"""Level A резолвер (MAP-AUTO-04) — матрица исходов на фикстурах, dry-run only.

Каждый исход словаря приложения B плана — по одному тесту; плюс граничные
из HANDOFF §7: два кандидата → не auto; health-слово → не auto; пакет →
composition; «C+» — не состав; rename stability; alias только approved и
unique; Level A отдельно от Capability/Outcome/Goal (AST); никаких
выдуманных id; read-only (AST); ``--apply`` отказывает с причиной;
детерминизм двух прогонов; счётчики сводки = построчная сумма.
"""
from __future__ import annotations

import ast
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from services.mapping import RulesEnabled, resolve_tenant
from services.mapping.normalize import normalize
from services.mapping.types import Decision, Reason, Summary
from services.models import SalonService, ServiceCategory, ServiceTemplate, ServiceTemplateSynonym
from tenants.models import Tenant
from users.models import User

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "services" / "mapping"
COMMAND = ROOT / "services" / "management" / "commands" / "map_salon_services.py"


# ---------------------------------------------------------------- фикстуры

def _cat(name):
    return ServiceCategory.objects.get_or_create(name=name)[0]


def _tpl(name, code, cat_name, *, lifecycle="approved", rhc=False):
    prov = {}
    if lifecycle == "approved":
        prov = {"approved_rule": "test", "approval_rule_version": "1", "approved_at": timezone.now(),
                "approval_source_ref": "fixture"}
    return ServiceTemplate.objects.create(
        category=_cat(cat_name), name=name, name_short=name[:40], canonical_code=code,
        lifecycle=lifecycle, requires_health_check=rhc, **prov,
    )


def _svc(tenant, name, cat_name, **kw):
    return SalonService.objects.create(tenant=tenant, category=_cat(cat_name), name=name, duration_minutes=60, **kw)


def _syn(template, text, tenant=None):
    return ServiceTemplateSynonym.objects.create(
        template=template, text=text, source_tenant=tenant, confirmed_by=None,
        confirmed_rule="fixture", rule_version="1", confirmed_at=timezone.now(), source_ref="fixture",
    )


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="ft-map-test", name="Формула тела (тест)")


@pytest.fixture
def canon(db):
    """Минимальный канон с кодами — схема «готова»."""
    return {
        "spina": _tpl("Массаж спины", "1.1.4", "Базовый ручной массаж"),
        "shvz": _tpl("Массаж шейно-воротниковой зоны", "1.1.5", "Базовый ручной массаж"),
        "stop_massage": _tpl("Массаж стоп", "1.1.14", "Базовый ручной массаж"),
        "stop_pedicure": _tpl("Массаж стоп", "9.3.6", "Уход за стопами"),
        "podmyshki": _tpl("Лазерная эпиляция подмышек", "7.1.6", "Лазерная эпиляция"),
        "boli": _tpl("Массаж при болях в спине", "1.3.24", "Оздоровительные и восстановительные массажи", rhc=True),
    }


def _one(tenant, rules="") -> dict:
    rows = resolve_tenant(tenant, RulesEnabled.parse(rules))
    return {r.raw_name: r for r in rows}


# ---------------------------------------------------------------- матрица исходов

def test_skip_decided_verified_and_not_recommendable(tenant, canon):
    owner = User.objects.create_user(username="own-map", password="x", phone="+79990002300")
    prov = dict(mapping_confirmed_by=owner, mapping_confirmed_at=timezone.now(), mapping_source_ref="fixture")
    _svc(tenant, "Массаж спины", "Базовый ручной массаж", template=canon["spina"],
         mapping_status=SalonService.MappingStatus.VERIFIED, **prov)
    _svc(tenant, "Парный массаж", "Ручные массажи",
         mapping_status=SalonService.MappingStatus.NOT_RECOMMENDABLE, **prov)
    rows = _one(tenant, "R1,R2")
    spina = rows["Массаж спины"]
    assert (spina.decision, spina.reason) == (Decision.SKIP_DECIDED, Reason.ALREADY_VERIFIED)
    assert rows["Парный массаж"].reason is Reason.NOT_RECOMMENDABLE
    assert rows["Массаж спины"].provenance_plan is None       # решённое не переигрывается даже с правилами


def test_exact_pair_is_auto_not_enabled_without_the_flag_and_auto_eligible_with_it(tenant, canon):
    _svc(tenant, "Массаж шейно-воротниковой зоны", "Базовый ручной массаж")
    off = _one(tenant)["Массаж шейно-воротниковой зоны"]
    assert (off.decision, off.reason, off.rule_id) == (Decision.AUTO_NOT_ENABLED, Reason.R1_EXACT_PAIR, "R1")
    assert off.provenance_plan is None

    on = _one(tenant, "R1")["Массаж шейно-воротниковой зоны"]
    assert (on.decision, on.reason) == (Decision.AUTO_ELIGIBLE, Reason.R1_EXACT_PAIR)
    assert [c.canonical_code for c in on.candidates] == ["1.1.5"]
    p = on.provenance_plan
    assert p.confirmed_by is None and p.confirmed_rule == "map_salon_services:R1"
    assert "seed=canonical_catalog_2026-07.json" in p.source_ref and "prev_template=none" in p.source_ref
    assert on.safety_handoff.canonical_rhc is False and on.safety_handoff.local_rhc is None


def test_approved_unique_synonym_is_r2_and_survives_a_rename_of_the_canon(tenant, canon):
    _syn(canon["podmyshki"], "Подмышки", tenant)
    _svc(tenant, "Подмышки", "Лазерная эпиляция")
    row = _one(tenant, "R2")["Подмышки"]
    assert (row.decision, row.reason, row.rule_id) == (Decision.AUTO_ELIGIBLE, Reason.R2_APPROVED_SYNONYM, "R2")
    assert row.candidates[0].canonical_code == "7.1.6"

    # rename stability: канон переименован — идентичность (код) та же
    ServiceTemplate.objects.filter(pk=canon["podmyshki"].pk).update(name="Эпиляция подмышек (лазер)")
    row = _one(tenant, "R2")["Подмышки"]
    assert row.decision is Decision.AUTO_ELIGIBLE and row.candidates[0].canonical_code == "7.1.6"


def test_two_auto_candidates_are_never_auto(tenant, canon):
    """Пара даёт один канон, синоним — другой: два кандидата под правило → review."""
    _syn(canon["shvz"], "Массаж спины", tenant)           # чужой синоним на то же слово
    _svc(tenant, "Массаж спины", "Базовый ручной массаж")
    row = _one(tenant, "R1,R2")["Массаж спины"]
    assert (row.decision, row.reason) == (Decision.REVIEW_REQUIRED, Reason.MULTIPLE_CANDIDATES)
    assert sorted(c.canonical_code for c in row.candidates if c.is_auto_candidate) == ["1.1.4", "1.1.5"]
    assert row.provenance_plan is None


def test_same_canon_found_twice_is_one_candidate(tenant, canon):
    """Пара и синоним указывают на один канон — кандидат один, не дубль."""
    _syn(canon["shvz"], "Массаж шейно-воротниковой зоны", tenant)
    _svc(tenant, "Массаж шейно-воротниковой зоны", "Базовый ручной массаж")
    row = _one(tenant, "R1,R2")["Массаж шейно-воротниковой зоны"]
    assert row.decision is Decision.AUTO_ELIGIBLE and len(row.candidates) == 1


def test_name_only_match_in_another_category_is_weak_evidence(tenant, canon):
    _svc(tenant, "Массаж стоп", "Массаж проблемных зон")   # категория салона ≠ каноническая подкатегория
    row = _one(tenant, "R1,R2")["Массаж стоп"]
    assert (row.decision, row.reason) == (Decision.REVIEW_REQUIRED, Reason.WEAK_EVIDENCE_NAME_ONLY)
    assert sorted(c.canonical_code for c in row.candidates) == ["1.1.14", "9.3.6"]
    assert not any(c.is_auto_candidate for c in row.candidates)


def test_composition_markers_block_auto_and_plain_plus_does_not(tenant, canon):
    _svc(tenant, "Must Have (подмышки+ глубокое бикини)", "Лазерная эпиляция")
    _svc(tenant, "Спина без боли - комплекс массажа", "Массажные комплексы")
    _svc(tenant, "Пилинг FERULIC PEEL C+", "Уходы для лица")
    rows = _one(tenant, "R1,R2")
    bundle = rows["Must Have (подмышки+ глубокое бикини)"]
    assert (bundle.decision, bundle.reason) == (Decision.REVIEW_REQUIRED, Reason.COMPOSITION_MARKER)
    assert bundle.safety_handoff.is_composite and bundle.safety_handoff.components == ("подмышки", "глубокое бикини")
    complex_ = rows["Спина без боли - комплекс массажа"]
    assert complex_.reason is Reason.COMPOSITION_MARKER          # состав раньше health по порядку §6
    assert "health:бол" in complex_.flags                       # но маркер здоровья не потерян
    plus = rows["Пилинг FERULIC PEEL C+"]
    assert plus.reason is not Reason.COMPOSITION_MARKER and not plus.safety_handoff.is_composite


def test_health_wording_blocks_auto_even_with_an_exact_pair(tenant, canon):
    _svc(tenant, "Массаж при болях в спине", "Оздоровительные и восстановительные массажи")
    row = _one(tenant, "R1")["Массаж при болях в спине"]
    assert (row.decision, row.reason) == (Decision.REVIEW_REQUIRED, Reason.HEALTH_MARKER)
    assert row.candidates and row.candidates[0].canonical_code == "1.3.24"   # кандидат виден, но не auto
    assert row.safety_handoff.canonical_rhc is True and row.safety_handoff.health_markers


def test_provisional_canon_is_review_not_auto(tenant, canon):
    _tpl("Биоэнергетический массаж", "1.2.99", "Расслабляющие массажи", lifecycle="provisional")
    _svc(tenant, "Биоэнергетический массаж", "Расслабляющие массажи")
    row = _one(tenant, "R1")["Биоэнергетический массаж"]
    assert (row.decision, row.reason) == (Decision.REVIEW_REQUIRED, Reason.CANDIDATE_PROVISIONAL)


def test_synonym_pointing_to_two_canons_is_ambiguous(tenant, canon):
    _syn(canon["stop_massage"], "Стопы", tenant)
    _syn(canon["stop_pedicure"], "Стопы", tenant)
    _svc(tenant, "Стопы", "Массаж проблемных зон")
    row = _one(tenant, "R2")["Стопы"]
    assert (row.decision, row.reason) == (Decision.REVIEW_REQUIRED, Reason.SYNONYM_AMBIGUOUS)
    assert "SYNONYM_AMBIGUOUS" in row.flags and row.provenance_plan is None


def test_no_match_is_unresolved_with_a_hint_not_a_verdict(tenant, canon):
    _svc(tenant, "Пилинг PROBIO PEEL", "Уходы для лица")
    row = _one(tenant, "R1,R2")["Пилинг PROBIO PEEL"]
    assert (row.decision, row.reason, row.hint) == (Decision.UNRESOLVED, Reason.NO_MATCH, "POSSIBLE_CANON_GAP")
    assert row.candidates == () and row.safety_handoff.canonical_rhc == "unknown"


def test_canon_without_code_blocks_the_row(tenant, canon):
    _tpl("Массаж головы", None, "Базовый ручной массаж")
    _svc(tenant, "Массаж головы", "Базовый ручной массаж")
    row = _one(tenant, "R1")["Массаж головы"]
    assert (row.decision, row.reason) == (Decision.BLOCKED, Reason.TEMPLATE_WITHOUT_CODE)


def test_marketing_vocabulary_is_empty_until_the_owner_names_it(tenant, canon, monkeypatch):
    from services.mapping import normalize as nz

    assert nz.MARKETING_WORDS == frozenset()
    _svc(tenant, "Премиум массаж спины", "Базовый ручной массаж")
    assert _one(tenant)["Премиум массаж спины"].reason is not Reason.MARKETING_WORDING
    monkeypatch.setattr(nz, "MARKETING_WORDS", frozenset({"премиум"}))
    row = _one(tenant)["Премиум массаж спины"]
    assert (row.decision, row.reason) == (Decision.REVIEW_REQUIRED, Reason.MARKETING_WORDING)


# ---------------------------------------------------------------- инварианты

def test_every_row_has_one_decision_one_reason_and_summary_equals_rows(tenant, canon):
    for name, cat in [("Массаж спины", "Базовый ручной массаж"), ("Подмышки", "Лазерная эпиляция"),
                      ("Комплекс «Гладкая кожа»", "Массажные комплексы"), ("Массаж стоп", "Массаж проблемных зон")]:
        _svc(tenant, name, cat)
    rows = resolve_tenant(tenant, RulesEnabled.parse("R1"))
    assert len(rows) == SalonService.objects.filter(tenant=tenant).count()
    s = Summary.of(rows)
    assert s.total == len(rows) == sum(s.by_decision.values()) == sum(s.by_reason.values())


def test_two_runs_on_the_same_input_are_identical(tenant, canon):
    _svc(tenant, "Массаж спины", "Базовый ручной массаж")
    _svc(tenant, "Стопы", "Массаж проблемных зон")
    a = resolve_tenant(tenant, RulesEnabled.parse("R1"))
    b = resolve_tenant(tenant, RulesEnabled.parse("R1"))
    assert a == b


def test_candidates_never_carry_invented_ids(tenant, canon):
    _syn(canon["podmyshki"], "Подмышки", tenant)
    for name, cat in [("Массаж спины", "Базовый ручной массаж"), ("Подмышки", "Лазерная эпиляция"),
                      ("Массаж стоп", "Массаж проблемных зон")]:
        _svc(tenant, name, cat)
    known = dict(ServiceTemplate.objects.values_list("pk", "canonical_code"))
    for row in resolve_tenant(tenant, RulesEnabled.parse("R1,R2")):
        for c in row.candidates:
            assert c.template_id in known and known[c.template_id] == c.canonical_code


def test_schema_not_ready_when_no_canon_carries_a_code(tenant):
    """Стоп до чтения корпуса: без кодов резолвер сравнивал бы с пустотой."""
    _tpl("Массаж головы", None, "Базовый ручной массаж")
    _svc(tenant, "Массаж головы", "Базовый ручной массаж")
    with pytest.raises(SystemExit) as exc:
        call_command("map_salon_services", tenant=tenant.slug, stdout=StringIO(), stderr=StringIO())
    assert exc.value.code == 2


def test_command_prints_subject_first_writes_nothing_and_refuses_apply(tenant, canon, tmp_path):
    _svc(tenant, "Массаж спины", "Базовый ручной массаж")
    before = list(SalonService.objects.values_list("mapping_status", "template_id"))
    out = StringIO()
    call_command("map_salon_services", tenant=tenant.slug, rules="R1", out=str(tmp_path / "r.json"), stdout=out)
    text = out.getvalue()
    assert text.startswith("предмет: база ")
    assert "AUTO_ELIGIBLE" in text and "строк: 1" in text
    assert (tmp_path / "r.json").exists()
    assert list(SalonService.objects.values_list("mapping_status", "template_id")) == before
    assert not ServiceTemplateSynonym.objects.exists()

    with pytest.raises(CommandError, match="OD-NEW-7"):
        call_command("map_salon_services", tenant=tenant.slug, apply=True, stdout=StringIO())
    with pytest.raises(CommandError, match="неизвестные правила"):
        call_command("map_salon_services", tenant=tenant.slug, rules="R9", stdout=StringIO())


# ---------------------------------------------------------------- структурные сторожа

def _package_trees():
    for path in [*sorted(PACKAGE.glob("*.py")), COMMAND]:
        yield path.relative_to(ROOT).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def test_package_is_read_only():
    """Ни .save/.update/.create/bulk_*/.delete, ни импорта формы §76 — записи нет по построению."""
    writes = {"save", "update", "create", "get_or_create", "update_or_create", "bulk_create", "bulk_update", "delete"}
    offenders = []
    for rel, tree in _package_trees():
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in writes:
                offenders.append(f"{rel}:{n.lineno} {ast.unparse(n.func)}")
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                mod = (n.module or "") if isinstance(n, ast.ImportFrom) else ",".join(a.name for a in n.names)
                if "services.admin" in mod or "forms" in mod:
                    offenders.append(f"{rel}:{n.lineno} import {mod}")
    assert not offenders, offenders


def test_level_a_does_not_import_capability_outcome_goal_or_recommendation():
    forbidden = ("goals", "recommendation", "ai.", "ai", "users.recommendation_source")
    offenders = []
    for rel, tree in _package_trees():
        for n in ast.walk(tree):
            top = {"goals", "recommendation", "ai"}
            if isinstance(n, ast.ImportFrom) and n.module and n.module.split(".")[0] in top:
                offenders.append(f"{rel}:{n.lineno} from {n.module}")
            if isinstance(n, ast.Import) and any(a.name.split(".")[0] in top for a in n.names):
                offenders.append(f"{rel}:{n.lineno} import")
    assert not offenders, (forbidden, offenders)


def test_no_numeric_score_anywhere_in_the_package():
    names = set()
    for _, tree in _package_trees():
        for n in ast.walk(tree):
            if isinstance(n, ast.Name):
                names.add(n.id.lower())
            elif isinstance(n, ast.Attribute):
                names.add(n.attr.lower())
    assert not {"score", "confidence", "similarity", "ratio"} & names


def test_mapping_status_is_read_only_for_skip_decided():
    """Второй легитимный читатель статуса — и только чтобы отступить перед решением человека."""
    src = (PACKAGE / "decide.py").read_text(encoding="utf-8")
    assert 'VERIFIED = "verified"' in src and 'NOT_RECOMMENDABLE = "not_recommendable"' in src
    tree = ast.parse(src)
    compares = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Compare)
                and "mapping_status" in ast.unparse(n)]
    assert compares and all("SKIP" not in c and "=" in c for c in compares)


def test_normalize_uses_the_shared_key_and_keeps_raw():
    ev = normalize("Массаж  «Спины»", "Базовый ручной массаж")
    assert ev.raw_name == "Массаж  «Спины»" and ev.norm_name == "массаж спины"
