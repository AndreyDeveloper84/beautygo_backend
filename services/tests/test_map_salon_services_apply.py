"""MAP-AUTO-06 — транзакционный apply резолвера за закрытыми воротами.

Что стережётся:

* **ворота закрыты**: настоящая ``authorize_apply`` отказывает, apply и откат
  пишут **ноль** строк и ноль синонимов; команда ``--apply`` — ``CommandError``;
* с открытыми воротами (подмена в тесте) — пишется **ровно** то, что dry-run
  назвал ``AUTO_ELIGIBLE``: parity множества строк и канонов; провенанс
  ``AUTO_RULE`` (``confirmed_by=None``, ``map_salon_services:R1``, версия,
  ``source_ref`` c ``prev_template``); синоним с тем же провенансом;
  ``REVIEW_REQUIRED``/``UNRESOLVED``/правило выключено — не трогаются;
* **parity mismatch**: план посчитан, база изменилась (второй кандидат) — стоп,
  ноль записей;
* **идемпотентность**: второй apply — ноль записей, строки не тронуты;
* **откат при частичном провале**: по умолчанию всё или ничего; ``per_row`` —
  провал строки назван, соседи записаны;
* версии: ``rule_version``/``seed_version`` не те — стоп до записи;
* владелец никогда не трогается; откат снимает только записи правила данной
  версии и их синонимы, возвращает ``prev_template``;
* затронутые рёбра: вердикт ``None`` → ``False`` в отчёте;
* писатель — только форма §76 (AST: в модуле нет save/update/create/bulk_*,
  единственный delete — синонимы правила).
"""
from __future__ import annotations

import ast
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from services import mapping_apply
from services.mapping import ApplyNotAuthorized, RulesEnabled, resolve_tenant
from services.mapping.types import RULE_VERSION, Decision
from services.mapping_apply import ApplyStopped, ParityMismatch, apply_tenant, rollback_auto_rule
from services.models import (
    SalonService,
    ServiceCategory,
    ServiceTemplate,
    ServiceTemplateSynonym,
    SpecialistService,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[2]
SEED = "canonical_catalog_2026-07.json"


def _open(**_):
    """Открытые ворота — только в тесте. В бою authorize_apply всегда отказывает."""
    return None


def _cat(name):
    return ServiceCategory.objects.get_or_create(name=name)[0]


def _tpl(name, code, cat_name, *, rhc=False):
    return ServiceTemplate.objects.create(
        category=_cat(cat_name), name=name, name_short=name[:40], canonical_code=code,
        lifecycle="approved", requires_health_check=rhc, approved_rule="test", approval_rule_version="1",
        approved_at=timezone.now(), approval_source_ref="fixture",
    )


def _svc(tenant, name, cat_name, **kw):
    return SalonService.objects.create(tenant=tenant, category=_cat(cat_name), name=name, duration_minutes=60, **kw)


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="ft-apply-test", name="Формула тела (apply)")


@pytest.fixture
def world(tenant):
    _tpl("Массаж шейно-воротниковой зоны", "1.1.5", "Базовый ручной массаж")
    _tpl("Антицеллюлитный массаж", "1.4.5", "Лимфодренаж и коррекция фигуры")
    _tpl("Массаж стоп", "1.1.14", "Базовый ручной массаж")
    _tpl("Массаж стоп", "9.3.6", "Уход за стопами")
    _tpl("Массаж при болях в спине", "1.3.24", "Оздоровительные и восстановительные массажи", rhc=True)
    return {
        "shvz": _svc(tenant, "Массаж шейно-воротниковой зоны", "Базовый ручной массаж"),
        "anti": _svc(tenant, "Антицеллюлитный массаж", "Лимфодренаж и коррекция фигуры"),
        "stop": _svc(tenant, "Массаж стоп", "Массаж проблемных зон"),
        "boli": _svc(tenant, "Массаж при болях в спине", "Оздоровительные и восстановительные массажи"),
        "gap": _svc(tenant, "Пилинг PROBIO PEEL", "Уходы для лица"),
    }


def _snapshot():
    return (list(SalonService.objects.order_by("pk").values()), ServiceTemplateSynonym.objects.count())


def _apply(tenant, rules="R1", **kw):
    kw.setdefault("rule_version", RULE_VERSION)
    kw.setdefault("seed_version", SEED)
    kw.setdefault("authorize", _open)
    return apply_tenant(tenant, RulesEnabled.parse(rules), **kw)


# ---------------------------------------------------------------- ворота

def test_closed_gate_writes_nothing_via_function_rollback_and_command(tenant, world):
    before = _snapshot()
    with pytest.raises(ApplyNotAuthorized, match="OD-NEW-7"):
        apply_tenant(tenant, RulesEnabled.parse("R1,R2"), rule_version=RULE_VERSION, seed_version=SEED)
    with pytest.raises(ApplyNotAuthorized):
        rollback_auto_rule(tenant, rule_version=RULE_VERSION)
    with pytest.raises(CommandError, match="OD-NEW-7"):
        call_command("map_salon_services", tenant=tenant.slug, rules="R1", apply=True,
                     rule_version=RULE_VERSION, seed_version=SEED, stdout=StringIO())
    assert _snapshot() == before


# ---------------------------------------------------------------- запись = план

def test_open_gate_writes_exactly_the_dry_run_auto_eligible_rows_with_rule_provenance(tenant, world):
    plan = resolve_tenant(tenant, RulesEnabled.parse("R1"))
    planned = {r.salon_service_id: next(c for c in r.candidates if c.is_auto_candidate).template_id
               for r in plan if r.decision is Decision.AUTO_ELIGIBLE}
    assert set(planned) == {world["shvz"].pk, world["anti"].pk}

    report = _apply(tenant)
    assert (report.planned, report.eligible, report.written, report.failed) == (5, 2, 2, 0)
    written = {s.pk: s.template_id for s in SalonService.objects.filter(mapping_status="verified")}
    assert written == planned                                           # parity: строки и каноны

    s = SalonService.objects.get(pk=world["shvz"].pk)
    assert s.mapping_confirmed_by_id is None and s.mapping_confirmed_rule == "map_salon_services:R1"
    assert s.mapping_rule_version == RULE_VERSION and s.mapping_confirmed_at is not None
    assert s.mapping_source_ref.startswith("auto:R1 seed=canonical_catalog_2026-07.json")
    assert "prev_template=none" in s.mapping_source_ref
    assert s.requires_health_check is None                               # unknown остаётся unknown
    syn = ServiceTemplateSynonym.objects.get(template=s.template, text=s.name)
    assert syn.confirmed_by_id is None and syn.confirmed_rule == "map_salon_services:R1"

    for key in ("stop", "boli", "gap"):                                  # review / health / no match
        other = SalonService.objects.get(pk=world[key].pk)
        assert other.mapping_status == "unmapped" and other.template_id is None


def test_rule_not_enabled_writes_nothing_even_with_an_open_gate(tenant, world):
    before = _snapshot()
    report = _apply(tenant, rules="")
    assert report.eligible == 0 and report.written == 0
    assert _snapshot() == before


def test_parity_mismatch_stops_and_writes_nothing(tenant, world):
    plan = resolve_tenant(tenant, RulesEnabled.parse("R1,R2"))
    # между планом и apply появился второй кандидат на то же слово
    ServiceTemplateSynonym.objects.create(
        template=ServiceTemplate.objects.get(canonical_code="1.4.5"), text="Массаж шейно-воротниковой зоны",
        source_tenant=tenant, confirmed_by=None, confirmed_rule="fixture", rule_version="1",
        confirmed_at=timezone.now(), source_ref="fixture",
    )
    before = _snapshot()
    with pytest.raises(ParityMismatch):
        _apply(tenant, rules="R1,R2", plan=plan)
    assert _snapshot() == before


def test_second_apply_is_a_no_op(tenant, world):
    _apply(tenant)
    before = _snapshot()
    report = _apply(tenant)
    assert report.written == 0 and report.eligible == 0                  # резолвер отдаёт SKIP_DECIDED
    assert _snapshot() == before


def test_partial_failure_rolls_back_everything_by_default_and_names_the_row_per_row(tenant, world, monkeypatch):
    real = mapping_apply._write_through_form

    # Строки идут по имени: «Антицеллюлитный…» первой, «Массаж шейно-…» второй.
    # Падать обязана ВТОРАЯ: провал первой ничего не успевает записать, и тест
    # «откат» зеленел бы и без транзакции (проба это показала).
    def flaky(service, **changes):
        if service.name == "Массаж шейно-воротниковой зоны":
            raise RuntimeError("форма упала на второй строке")
        return real(service, **changes)

    monkeypatch.setattr(mapping_apply, "_write_through_form", flaky)
    before = _snapshot()
    with pytest.raises(RuntimeError, match="второй строке"):
        _apply(tenant)
    assert _snapshot() == before                                         # всё или ничего: первая откатана

    report = _apply(tenant, per_row=True)
    assert report.written == 1 and report.failed == 1
    failed = next(r for r in report.rows if r.outcome == "failed")
    assert failed.name == "Массаж шейно-воротниковой зоны" and "второй строке" in failed.detail
    assert SalonService.objects.get(pk=world["anti"].pk).mapping_status == "verified"
    assert SalonService.objects.get(pk=world["shvz"].pk).mapping_status == "unmapped"


def test_version_mismatch_stops_before_any_write(tenant, world):
    before = _snapshot()
    with pytest.raises(ApplyStopped, match="rule_version"):
        _apply(tenant, rule_version="map-salon-services/0.0.1")
    with pytest.raises(ApplyStopped, match="SEED_MISMATCH"):
        _apply(tenant, seed_version="canonical_catalog_2099-01.json")
    assert _snapshot() == before


def test_only_limits_the_write_to_named_rows(tenant, world):
    report = _apply(tenant, only={world["anti"].pk})
    assert report.written == 1
    verified = list(SalonService.objects.filter(mapping_status="verified").values_list("pk", flat=True))
    assert verified == [world["anti"].pk]


def test_affected_edges_verdict_moves_from_unknown_to_known(tenant, world):
    user = User.objects.create_user(username="m-apply", password="x", role="specialist", phone="+79990002400")
    master = SpecialistProfile.objects.get(user=user)
    SpecialistService.objects.create(salon_service=world["shvz"], specialist=master, price=1000, duration_minutes=60)
    report = _apply(tenant, only={world["shvz"].pk})
    assert report.edges_verdict_before == {"None": 1}
    assert report.edges_verdict_after == {"False": 1}


# ---------------------------------------------------------------- откат и владелец

def test_rollback_reverts_only_rule_rows_and_their_synonyms_never_the_owner(tenant, world):
    owner = User.objects.create_user(username="owner-apply", password="x", phone="+79990002401")
    boli = world["boli"]
    boli.template = ServiceTemplate.objects.get(canonical_code="1.3.24")
    boli.mapping_status = "verified"
    boli.mapping_confirmed_by = owner
    boli.mapping_confirmed_at = timezone.now()
    boli.mapping_source_ref = "owner"
    boli.save()
    ServiceTemplateSynonym.objects.create(
        template=boli.template, text=boli.name, source_tenant=tenant, confirmed_by=owner,
        confirmed_at=timezone.now(), source_ref="owner",
    )

    _apply(tenant)
    assert SalonService.objects.filter(mapping_confirmed_rule__startswith="map_salon_services:").count() == 2

    report = rollback_auto_rule(tenant, rule_version=RULE_VERSION, authorize=_open)
    assert sorted(report.reverted) == ["Антицеллюлитный массаж", "Массаж шейно-воротниковой зоны"]
    assert report.synonyms_removed == 2
    for key in ("shvz", "anti"):
        s = SalonService.objects.get(pk=world[key].pk)
        assert s.mapping_status == "unmapped" and s.template_id is None and s.mapping_confirmed_rule == ""
    kept = SalonService.objects.get(pk=boli.pk)
    assert kept.mapping_status == "verified" and kept.mapping_confirmed_by == owner
    assert ServiceTemplateSynonym.objects.filter(confirmed_by=owner).count() == 1


# ---------------------------------------------------------------- писатель

def test_the_apply_module_writes_only_through_the_form():
    tree = ast.parse((ROOT / "services" / "mapping_apply.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    forbidden = [ast.unparse(n.func) for n in calls
                 if n.func.attr in {"save", "update", "create", "get_or_create", "update_or_create",
                                    "bulk_create", "bulk_update"}]
    assert not forbidden, forbidden
    deletes = [ast.unparse(n) for n in calls if n.func.attr == "delete"]
    assert len(deletes) == 1 and "ServiceTemplateSynonym" in deletes[0]
    writers = [n for n in ast.walk(tree)
               if isinstance(n, ast.Call) and ast.unparse(n.func) == "_write_through_form"]
    assert len(writers) == 2                                             # apply и откат
