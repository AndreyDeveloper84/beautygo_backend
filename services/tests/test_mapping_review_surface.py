"""Review-поверхность владельца (MAP-AUTO-05): колонки из отчёта, действия через форму §76.

Что стережётся:

* колонки/блок формы читают **файл отчёта** последнего dry-run; без отчёта —
  «нет прогона», это отдельное состояние; фильтр по исходу работает;
* «Пересчитать» пишет файл и **не трогает базу**;
* «Подтвердить связь с единственным кандидатом»: пишет ``VERIFIED`` +
  ``template`` + ``confirmed_by = кто нажал`` + ``source_ref`` «review …»
  через форму, оставляет синоним; отказывает без записи, когда кандидатов
  ≠ 1, стоят флаги состава/здоровья, строка уже решена, отчёта нет;
* «Разрыв канона» → ``NOT_RECOMMENDABLE`` с ``CANON_GAP`` в ``source_ref``,
  ``confirmed_by`` = кто нажал; решённое не переигрывается;
* ничего не предзаполняется: у строки в отчёте с планом apply поля
  провенанса в базе пусты, пока человек не нажал;
* из админки нельзя поставить ``VERIFIED`` без шаблона и без «кто/правило»
  (форма отказывает по полю) — сторож (4);
* единственный писатель review — ``SalonServiceAdminForm`` (AST по модулю).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from django.contrib import admin as django_admin
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from services.admin import SalonServiceAdminForm
from services.mapping.store import StoredReport, report_path
from services.mapping_review import confirm_single_candidate, mark_canon_gap
from services.models import SalonService, ServiceCategory, ServiceTemplate, ServiceTemplateSynonym
from tenants.models import Tenant
from users.models import User

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[2]
CHANGELIST = "admin:services_salonservice_changelist"


@pytest.fixture(autouse=True)
def _report_dir(settings, tmp_path):
    settings.MAPPING_REPORT_DIR = tmp_path / "reports"


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


@pytest.fixture
def owner(db):
    return User.objects.create_superuser(username="owner-review", password="pw", email="o@b.c", role="admin")


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="ft-review-test", name="Формула тела (review)")


@pytest.fixture
def world(tenant):
    """Канон + четыре услуги под разные исходы."""
    _tpl("Массаж шейно-воротниковой зоны", "1.1.5", "Базовый ручной массаж")
    _tpl("Массаж стоп", "1.1.14", "Базовый ручной массаж")
    _tpl("Массаж стоп", "9.3.6", "Уход за стопами")
    _tpl("Массаж при болях в спине", "1.3.24", "Оздоровительные и восстановительные массажи", rhc=True)
    return {
        "single": _svc(tenant, "Массаж шейно-воротниковой зоны", "Базовый ручной массаж"),
        "multi": _svc(tenant, "Массаж стоп", "Массаж проблемных зон"),
        "health": _svc(tenant, "Массаж при болях в спине", "Оздоровительные и восстановительные массажи"),
        "gap": _svc(tenant, "Пилинг PROBIO PEEL", "Уходы для лица"),
    }


def _client(owner):
    from django.test import Client

    c = Client()
    c.force_login(owner)
    return c


def _action(client, name, ids):
    return client.post(reverse(CHANGELIST), {"action": name, "_selected_action": [str(i) for i in ids]}, follow=True)


def _recompute(client, world):
    return _action(client, "recompute_resolver_dry_run", [s.pk for s in world.values()])


# ---------------------------------------------------------------- чтение

def test_without_a_report_the_column_says_no_run_and_the_form_block_says_so(owner, world):
    ma = django_admin.site._registry[SalonService]
    assert ma.resolver_outcome(world["single"]) == "— нет прогона"
    assert "нет прогона" in ma.resolver_evidence(world["single"])
    assert ma.resolver_candidates(world["single"]) == ""


def test_recompute_writes_the_report_file_and_touches_no_row(owner, tenant, world):
    before = list(SalonService.objects.order_by("pk").values())      # вся строка, не три поля
    resp = _recompute(_client(owner), world)
    assert resp.status_code == 200
    assert report_path(tenant.slug).exists()
    assert list(SalonService.objects.order_by("pk").values()) == before
    ma = django_admin.site._registry[SalonService]
    assert ma.resolver_outcome(world["single"]) == "AUTO_NOT_ENABLED / R1_EXACT_PAIR"
    assert ma.resolver_outcome(world["multi"]) == "REVIEW_REQUIRED / WEAK_EVIDENCE_NAME_ONLY"
    assert ma.resolver_outcome(world["health"]) == "REVIEW_REQUIRED / HEALTH_MARKER"
    assert ma.resolver_outcome(world["gap"]) == "UNRESOLVED / NO_MATCH"
    assert "1.1.5" in ma.resolver_candidates(world["single"]) and "←R1" in ma.resolver_candidates(world["single"])
    assert "health:" in ma.resolver_flags(world["health"])
    evidence = ma.resolver_evidence(world["single"])
    assert "AUTO_NOT_ENABLED / R1_EXACT_PAIR" in evidence and "кандидат 1.1.5" in evidence


def test_changelist_filter_by_resolver_decision(owner, tenant, world):
    client = _client(owner)
    _recompute(client, world)
    page = client.get(reverse(CHANGELIST) + "?resolver=UNRESOLVED").content.decode()
    assert "Пилинг PROBIO PEEL" in page and "Массаж шейно-воротниковой зоны" not in page
    page = client.get(reverse(CHANGELIST) + "?resolver=NONE").content.decode()
    assert "Пилинг PROBIO PEEL" not in page


def test_nothing_is_prefilled_after_a_run(owner, tenant, world):
    _recompute(_client(owner), world)
    s = SalonService.objects.get(pk=world["single"].pk)
    assert s.mapping_status == SalonService.MappingStatus.UNMAPPED and s.template_id is None
    assert s.mapping_confirmed_by_id is None and s.mapping_source_ref == ""


# ---------------------------------------------------------------- действие «подтвердить»

def test_confirm_single_candidate_writes_verified_with_the_clicker_as_confirmer(owner, tenant, world):
    client = _client(owner)
    _recompute(client, world)
    resp = _action(client, "confirm_single_resolver_candidate", [world["single"].pk])
    assert resp.status_code == 200
    s = SalonService.objects.get(pk=world["single"].pk)
    assert s.mapping_status == SalonService.MappingStatus.VERIFIED
    assert s.template.canonical_code == "1.1.5"
    assert s.mapping_confirmed_by == owner and s.mapping_confirmed_at is not None
    assert s.mapping_confirmed_rule == "" and s.mapping_rule_version == ""
    assert s.mapping_source_ref.startswith("review ") and "AUTO_NOT_ENABLED/R1_EXACT_PAIR" in s.mapping_source_ref
    assert ServiceTemplateSynonym.objects.filter(template=s.template, text=s.name, confirmed_by=owner).exists()


def test_confirm_refuses_multi_health_decided_and_missing_report(owner, tenant, world):
    client = _client(owner)
    # без отчёта — отказ
    out = confirm_single_candidate(world["single"], owner)
    assert not out.written and "нет строки в отчёте" in out.message
    _recompute(client, world)
    out = confirm_single_candidate(world["multi"], owner)
    assert not out.written and "кандидатов 2" in out.message
    out = confirm_single_candidate(world["health"], owner)
    assert not out.written and "health:" in out.message
    out = confirm_single_candidate(world["gap"], owner)
    assert not out.written and "кандидатов 0" in out.message
    # решённое не переигрывается
    confirm_single_candidate(world["single"], owner)
    out = confirm_single_candidate(SalonService.objects.get(pk=world["single"].pk), owner)
    assert not out.written and "уже решено" in out.message
    # ни одна из отказанных строк не тронута
    for key in ("multi", "health", "gap"):
        s = SalonService.objects.get(pk=world[key].pk)
        assert s.mapping_status == SalonService.MappingStatus.UNMAPPED and s.template_id is None


def test_selecting_everything_is_not_apply_all(owner, tenant, world):
    """Выделить все и нажать — пишется только то, что проходит правило единственного кандидата."""
    client = _client(owner)
    _recompute(client, world)
    _action(client, "confirm_single_resolver_candidate", [s.pk for s in world.values()])
    assert SalonService.objects.filter(mapping_status=SalonService.MappingStatus.VERIFIED).count() == 1


# ---------------------------------------------------------------- действие «разрыв канона»

def test_canon_gap_marks_not_recommendable_with_a_findable_tag(owner, tenant, world):
    client = _client(owner)
    _recompute(client, world)
    _action(client, "mark_canon_gap", [world["gap"].pk])
    s = SalonService.objects.get(pk=world["gap"].pk)
    assert s.mapping_status == SalonService.MappingStatus.NOT_RECOMMENDABLE
    assert s.mapping_confirmed_by == owner and s.mapping_source_ref.startswith("CANON_GAP: review ")
    assert "UNRESOLVED/NO_MATCH" in s.mapping_source_ref
    assert not ServiceTemplateSynonym.objects.exists()          # отказ синонима не оставляет
    out = mark_canon_gap(s, owner)
    assert not out.written and "уже решено" in out.message


# ---------------------------------------------------------------- сторож (4) и писатель

def test_admin_form_refuses_verified_without_template_and_without_who_or_rule(owner, tenant, world):
    ma = django_admin.site._registry[SalonService]
    request = RequestFactory().get("/")
    request.user = owner
    Form = ma.get_form(request, world["single"])
    base = {
        "tenant": str(tenant.pk), "template": "", "category": str(world["single"].category_id),
        "name": world["single"].name, "duration_minutes": "60", "base_price": "", "is_active": True,
        "source": SalonService.Source.MANUAL, "mapping_status": SalonService.MappingStatus.VERIFIED,
        "mapping_confirmed_by": "", "mapping_confirmed_rule": "", "mapping_rule_version": "",
        "mapping_confirmed_at_0": "2026-09-12", "mapping_confirmed_at_1": "12:00:00",
        "mapping_source_ref": "review",
    }
    form = Form(data=base, instance=world["single"])
    assert not form.is_valid()
    assert "template" in form.errors and "mapping_confirmed_by" in form.errors


def test_the_only_writer_in_review_is_the_admin_form():
    src = (ROOT / "services" / "mapping_review.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    writes = [
        ast.unparse(n.func) for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"save", "update", "create", "bulk_update", "bulk_create", "delete"}
    ]
    assert writes == ["form.save"], writes
    assert isinstance(SalonServiceAdminForm, type)


def test_report_store_roundtrip(owner, tenant, world):
    _recompute(_client(owner), world)
    report = StoredReport.load(tenant.slug)
    assert report is not None and report.generated_at is not None
    assert set(report.rows) == {str(s.pk) for s in world.values()}
    assert StoredReport.load("no-such-tenant") is None
