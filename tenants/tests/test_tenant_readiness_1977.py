"""DRF-1977: ``manage.py tenant_readiness --tenant <slug>`` — готовность салона.

Раздел Q решений владельца (docs/OWNER_QUESTIONS_2026-09-12.md): после 13
шагов онбординга оператор проверяет салон одной командой. Команда — проекция
фактов каталога только на чтение, не сущность и не поле. Итог — из закрытого
словаря:

* ``READY`` — клиент может найти услугу, увидеть мастера и записаться;
* ``READY_WITHOUT_DISTANCE`` — записаться можно, расстояние не считается
  (нет подтверждённого места с координатами — DaData салон не блокирует);
* ``NOT_READY`` — записаться нельзя; причины названы.

Строка вывода — ``ключ=значение``; итог — ``RESULT: X``; причины —
``reasons: a,b``; сведения, которые не меняют итог, — ``info: a,b``.
"""
from __future__ import annotations

from datetime import time
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command, get_commands
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from ai.tests.factories import make_specialist, make_user
from appointments.models import SpecialistWorkingHours
from services.models import SalonService, ServiceCategory, ServiceTemplate, SpecialistService
from tenants.models import LocationStatus, ServiceLocation, Tenant
from tenants.tests.places import place_specialist_at
from users.models import SpecialistProfile, TenantUserRelationship

pytestmark = pytest.mark.django_db

COMMAND = "tenant_readiness"
SLUG = "ready-1977"


def _run(*args: str) -> str:
    assert COMMAND in get_commands(), "команды tenant_readiness нет"
    out = StringIO()
    call_command(COMMAND, *args, stdout=out)
    return out.getvalue()


def _lines(out: str) -> list[str]:
    return [line.strip() for line in out.splitlines() if line.strip()]


def _result(out: str) -> str:
    rows = [line for line in _lines(out) if line.startswith("RESULT:")]
    assert len(rows) == 1, out
    return rows[0].split(":", 1)[1].strip()


def _tokens(out: str, head: str) -> set[str]:
    rows = [line for line in _lines(out) if line.startswith(f"{head}:")]
    assert len(rows) <= 1, out
    if not rows:
        return set()
    return {t.strip() for t in rows[0].split(":", 1)[1].split(",") if t.strip()}


def _has(out: str, *pairs: str) -> bool:
    words = set(out.split())
    return all(pair in words for pair in pairs)


def _verified(**fields) -> dict:
    return {
        "mapping_status": "verified",
        "mapping_confirmed_rule": "fixture_rule_1977",
        "mapping_rule_version": "v1",
        "mapping_confirmed_at": timezone.now(),
        "mapping_source_ref": "фикстура DRF-1977",
        **fields,
    }


@pytest.fixture
def salon():
    """Готовый салон: каждый из 13 шагов раздела Q сделан, ровно по одному."""
    tenant = Tenant.objects.create(slug=SLUG, name="Салон готовности", city="Пенза")
    master = make_specialist(display_name="Мастер готовности")
    SpecialistProfile.objects.filter(pk=master.pk).update(tenant=tenant)
    master.refresh_from_db()
    place = place_specialist_at(master, 53.2, 45.0)
    for day in range(5):
        SpecialistWorkingHours.objects.create(
            specialist=master, day_of_week=day, is_working_day=True,
            start_time=time(10), end_time=time(19),
        )
    category = ServiceCategory.objects.create(name="Маникюр 1977", slug="manicure-1977")
    template = ServiceTemplate.objects.create(
        category=category, name="Маникюр классический 1977", name_short="Маникюр",
        duration_default=60, requires_health_check=False,
    )
    service = SalonService.objects.create(
        tenant=tenant, template=template, category=category, name="Маникюр",
        **_verified(),
    )
    edge = SpecialistService.objects.create(
        salon_service=service, specialist=master, price=Decimal("1500.00"),
    )
    admin = make_user(role="client")
    tur = TenantUserRelationship.objects.create(
        user=admin, tenant=tenant, role=TenantUserRelationship.Role.ADMIN, is_active=True,
    )
    return {
        "tenant": tenant, "master": master, "place": place, "category": category,
        "template": template, "service": service, "edge": edge, "tur": tur,
    }


# --- итог READY и строки фактов -------------------------------------------


def test_ready_salon_reads_ready_without_reasons(salon):
    out = _run("--tenant", SLUG)
    assert _result(out) == "READY"
    assert _tokens(out, "reasons") == set()


def test_ready_salon_prints_each_fact_line(salon):
    out = _run("--tenant", SLUG)
    assert _has(out, "is_active=true", "kind=salon")
    assert _has(out, "city_set=true")
    assert _has(out, "confirmed=1", "review_required=0", "inactive=0")
    assert _has(out, "participating_places=1", "masters_with_participating_place=1")
    assert _has(out, "sellable=1", "pool=1", "draft=0", "pending=0")
    assert _has(out, "with_working_day=1")
    assert _has(out, "active_services=1", "total_services=1", "with_template=1")
    assert _has(out, "verified=1", "of=1")
    assert _has(out, "bookable_edges=1", "health_unknown_edges=0")
    assert _has(out, "active_admins=1")


def test_schedule_line_names_its_source_and_what_it_cannot_see(salon):
    out = _run("--tenant", SLUG)
    assert _has(out, "source=catalog", "engine_read=not_checked_from_catalog")


def test_bot_binding_is_named_as_not_checked(salon):
    out = _run("--tenant", SLUG)
    assert _has(out, "bot=not_checked_from_catalog")
    assert "bot_not_checked" in _tokens(out, "info")


# --- отказы команды --------------------------------------------------------


def test_unknown_slug_is_refused(salon):
    assert COMMAND in get_commands(), "команды tenant_readiness нет"
    with pytest.raises(CommandError, match="нет-такого"):
        call_command(COMMAND, "--tenant", "нет-такого", stdout=StringIO())


def test_solo_workspace_is_refused(salon):
    Tenant.all_objects.filter(pk=salon["tenant"].pk).update(kind=Tenant.Kind.SOLO)
    assert COMMAND in get_commands(), "команды tenant_readiness нет"
    with pytest.raises(CommandError, match="solo"):
        call_command(COMMAND, "--tenant", SLUG, stdout=StringIO())


# --- NOT_READY: по одной причине на тест ------------------------------------


def _not_ready_with(out: str, reason: str) -> None:
    assert _result(out) == "NOT_READY", out
    assert reason in _tokens(out, "reasons"), out


def test_inactive_tenant_is_found_and_not_ready(salon):
    Tenant.all_objects.filter(pk=salon["tenant"].pk).update(is_active=False)
    out = _run("--tenant", SLUG)
    assert _has(out, "is_active=false")
    _not_ready_with(out, "tenant_inactive")


def test_missing_city(salon):
    Tenant.all_objects.filter(pk=salon["tenant"].pk).update(city="   ")
    out = _run("--tenant", SLUG)
    assert _has(out, "city_set=false")
    _not_ready_with(out, "city_missing")


def test_no_confirmed_location(salon):
    ServiceLocation.objects.filter(pk=salon["place"].pk).update(status=LocationStatus.REVIEW_REQUIRED)
    out = _run("--tenant", SLUG)
    assert _has(out, "confirmed=0", "review_required=1")
    _not_ready_with(out, "no_confirmed_location")


def test_master_paused_bookings_is_no_bookable_masters(salon):
    SpecialistProfile.objects.filter(pk=salon["master"].pk).update(is_booking_enabled=False)
    out = _run("--tenant", SLUG)
    assert _has(out, "sellable=0", "pool=1")
    _not_ready_with(out, "no_bookable_masters")


def test_master_with_inactive_user_is_no_bookable_masters(salon):
    type(salon["master"].user).objects.filter(pk=salon["master"].user_id).update(is_active=False)
    out = _run("--tenant", SLUG)
    assert _has(out, "sellable=0")
    _not_ready_with(out, "no_bookable_masters")


def test_no_working_day(salon):
    SpecialistWorkingHours.objects.filter(specialist=salon["master"]).update(is_working_day=False)
    out = _run("--tenant", SLUG)
    assert _has(out, "with_working_day=0")
    _not_ready_with(out, "no_schedule")


def test_working_day_without_hours_is_not_a_working_day(salon):
    SpecialistWorkingHours.objects.filter(specialist=salon["master"]).update(end_time=None)
    out = _run("--tenant", SLUG)
    _not_ready_with(out, "no_schedule")


def test_no_active_services(salon):
    SalonService.objects.filter(pk=salon["service"].pk).update(is_active=False)
    out = _run("--tenant", SLUG)
    assert _has(out, "active_services=0", "total_services=1")
    _not_ready_with(out, "no_active_services")


def test_no_verified_services(salon):
    SalonService.objects.filter(pk=salon["service"].pk).update(mapping_status="review_required")
    out = _run("--tenant", SLUG)
    assert _has(out, "verified=0")
    _not_ready_with(out, "no_verified_services")


def test_inactive_edge_is_no_bookable_edges(salon):
    SpecialistService.objects.filter(pk=salon["edge"].pk).update(is_active=False)
    out = _run("--tenant", SLUG)
    assert _has(out, "bookable_edges=0")
    _not_ready_with(out, "no_bookable_edges")


def test_edge_below_one_rouble_is_no_bookable_edges(salon):
    """DRF-1962: ребро дешевле 1 ₽ не продаётся (``sellable_offer_q``)."""
    SpecialistService.objects.filter(pk=salon["edge"].pk).update(price=Decimal("0.00"))
    out = _run("--tenant", SLUG)
    assert _has(out, "bookable_edges=0")
    _not_ready_with(out, "no_bookable_edges")


def test_edge_at_unsellable_master_is_no_bookable_edges(salon):
    """Ребро продаётся, но его мастер скрыт; продаётся другой мастер, у которого рёбер нет."""
    second = make_specialist(display_name="Второй мастер")
    SpecialistProfile.objects.filter(pk=second.pk).update(tenant=salon["tenant"])
    SpecialistWorkingHours.objects.create(
        specialist=second, day_of_week=0, is_working_day=True, start_time=time(10), end_time=time(19),
    )
    SpecialistProfile.objects.filter(pk=salon["master"].pk).update(is_available=False)
    out = _run("--tenant", SLUG)
    assert _has(out, "sellable=1", "bookable_edges=0")
    _not_ready_with(out, "no_bookable_edges")


def test_revoked_admin_is_no_admin(salon):
    TenantUserRelationship.objects.filter(pk=salon["tur"].pk).update(is_active=False)
    out = _run("--tenant", SLUG)
    assert _has(out, "active_admins=0")
    _not_ready_with(out, "no_admin")


def test_staff_role_is_not_an_admin(salon):
    TenantUserRelationship.objects.filter(pk=salon["tur"].pk).update(role=TenantUserRelationship.Role.STAFF)
    out = _run("--tenant", SLUG)
    _not_ready_with(out, "no_admin")


# --- READY_WITHOUT_DISTANCE ------------------------------------------------


def test_confirmed_place_without_coordinates_is_ready_without_distance(salon):
    """Раздел Q: отсутствие DaData салон не блокирует — место подтверждено, координат нет."""
    ServiceLocation.objects.filter(pk=salon["place"].pk).update(latitude=None, longitude=None)
    out = _run("--tenant", SLUG)
    assert _has(out, "confirmed=1", "participating_places=0")
    assert _result(out) == "READY_WITHOUT_DISTANCE"
    assert "no_coordinates" in _tokens(out, "reasons")


def test_sellable_master_without_participating_place(salon):
    second = make_specialist(display_name="Мастер без места")
    SpecialistProfile.objects.filter(pk=second.pk).update(tenant=salon["tenant"])
    out = _run("--tenant", SLUG)
    assert _has(out, "sellable=2", "masters_with_participating_place=1")
    assert _result(out) == "READY_WITHOUT_DISTANCE"
    assert "masters_without_place" in _tokens(out, "reasons")


# --- сведения (info), итог не меняют ---------------------------------------


def test_pending_specialists_are_info_not_a_reason(salon):
    draft = make_specialist(display_name="Черновик", status=SpecialistProfile.ProfileStatus.DRAFT)
    pending = make_specialist(display_name="На проверке", status=SpecialistProfile.ProfileStatus.PENDING)
    SpecialistProfile.objects.filter(pk__in=[draft.pk, pending.pk]).update(tenant=salon["tenant"])
    out = _run("--tenant", SLUG)
    assert _has(out, "draft=1", "pending=1")
    assert _result(out) == "READY"
    assert "specialists_pending" in _tokens(out, "info")


def test_edge_with_unknown_health_check_is_counted_apart(salon):
    """Без шаблона и без явного ответа признак здоровья неизвестен — такое ребро не считается записываемым."""
    loose = SalonService.objects.create(
        tenant=salon["tenant"], category=salon["category"], name="Услуга без шаблона",
        duration_minutes=45,
    )
    SpecialistService.objects.create(salon_service=loose, specialist=salon["master"], price=Decimal("900.00"))
    out = _run("--tenant", SLUG)
    assert _has(out, "bookable_edges=1", "health_unknown_edges=1")
    assert _has(out, "active_services=2", "with_template=1", "verified=1", "of=2")
    assert _result(out) == "READY"
    assert {"health_check_unknown_edges", "verified_partial"} <= _tokens(out, "info")


def test_unconfigured_geocoder_is_info(salon, settings):
    settings.GEOCODING_PROVIDER = "нет-такого-провайдера"
    out = _run("--tenant", SLUG)
    assert _result(out) == "READY"
    assert "geocoder_not_configured" in _tokens(out, "info")


# --- --require-ready ---------------------------------------------------------


def test_require_ready_fails_on_not_ready_after_printing(salon):
    TenantUserRelationship.objects.filter(pk=salon["tur"].pk).update(is_active=False)
    assert COMMAND in get_commands(), "команды tenant_readiness нет"
    out = StringIO()
    with pytest.raises(CommandError, match="NOT_READY"):
        call_command(COMMAND, "--tenant", SLUG, "--require-ready", stdout=out)
    assert _result(out.getvalue()) == "NOT_READY"


def test_require_ready_passes_ready_without_distance(salon):
    ServiceLocation.objects.filter(pk=salon["place"].pk).update(latitude=None, longitude=None)
    out = _run("--tenant", SLUG, "--require-ready")
    assert _result(out) == "READY_WITHOUT_DISTANCE"


def test_without_require_ready_not_ready_is_not_an_error(salon):
    TenantUserRelationship.objects.filter(pk=salon["tur"].pk).update(is_active=False)
    out = _run("--tenant", SLUG)
    assert _result(out) == "NOT_READY"


# --- только чтение ---------------------------------------------------------


def test_command_writes_nothing(salon):
    """Проекция, не сущность: ни строки, ни записи в SQL."""
    models = (
        Tenant.all_objects, ServiceLocation.objects, SpecialistProfile.objects,
        SpecialistWorkingHours.objects, SalonService.objects, SpecialistService.objects,
        TenantUserRelationship.objects,
    )
    before = tuple(m.count() for m in models)
    assert COMMAND in get_commands(), "команды tenant_readiness нет"
    with CaptureQueriesContext(connection) as ctx:
        call_command(COMMAND, "--tenant", SLUG, stdout=StringIO())
    after = tuple(m.count() for m in models)
    assert before == after
    statements = [q["sql"].lstrip().split(None, 1)[0].upper() for q in ctx.captured_queries]
    assert "SELECT" in statements
    writes = [s for s in statements if s in {"INSERT", "UPDATE", "DELETE", "TRUNCATE"}]
    assert writes == []
