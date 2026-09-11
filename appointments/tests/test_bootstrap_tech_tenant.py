"""Сторожа фикстуры технического тенанта (§13, OD-PILOT-6).

Каждый тест здесь стоит против конкретного требования брифа, и все
шесть — про то, чем фикстура может ТИХО испортиться:

1. настоящих ПДн нет  → телефоны NULL, слаг/имя вымышленные;
2. отличим от боевого → слаг, имя, запрет на чужой слаг;
3. идемпотентность    → второй запуск не плодит и не затирает записи;
4. «неизвестно» = NULL → а не сочинённое False (дефект PR #307);
5. выходной пустой    → is_working_day=False ⇒ все четыре времени None;
6. по умолчанию сухо  → без --apply не появляется ни одной строки.
"""
from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from appointments.management.commands.bootstrap_tech_tenant import IDS, TENANT_SLUG
from appointments.models import Appointment, SpecialistWorkingHours
from services.models import SalonService, SpecialistService
from tenants.models import Tenant
from users.models import SpecialistProfile, User


def _run(*args) -> str:
    out = StringIO()
    call_command("bootstrap_tech_tenant", *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def seeded(db):
    return _run("--apply")


# ---------------------------------------------------------------------------
# 6. Сухой прогон по умолчанию
# ---------------------------------------------------------------------------
def test_without_apply_nothing_is_written(db):
    """Умолчание не пишет. Иначе случайный запуск на чужом стенде — запись."""
    text = _run()

    assert Tenant.all_objects.filter(slug=TENANT_SLUG).count() == 0
    assert SalonService.objects.count() == 0
    assert Appointment.objects.count() == 0
    assert "СУХОЙ ПРОГОН" in text
    # Положительная стража: сухой прогон обязан ПОСЧИТАТЬ строки,
    # а не промолчать. Отчёт «created=0» означал бы, что предпросмотр
    # ничего не строил и числа взяты из воздуха.
    assert "created=26" in text


def test_apply_creates_the_whole_declared_shape(seeded):
    tenant = Tenant.all_objects.get(slug=TENANT_SLUG)
    assert tenant.id == IDS["tenant"]
    assert SalonService.objects.filter(tenant=tenant).count() == 4
    assert SpecialistService.objects.filter(tenant=tenant).count() == 4
    assert SpecialistWorkingHours.objects.filter(specialist__tenant=tenant).count() == 7
    assert Appointment.objects.filter(tenant=tenant).count() == 4


# ---------------------------------------------------------------------------
# 1. Настоящих персональных данных нет
# ---------------------------------------------------------------------------
def test_no_invented_phone_numbers(seeded):
    """Правдоподобный номер — это чей-то настоящий номер. NULL честнее."""
    users = User.objects.filter(username__startswith="tech-probe-")
    assert users.count() == 2
    assert {u.phone for u in users} == {None}


def test_tenant_carries_no_address_and_no_city(seeded):
    tenant = Tenant.all_objects.get(slug=TENANT_SLUG)
    assert tenant.address == ""
    assert tenant.city == ""


# ---------------------------------------------------------------------------
# 2. Отличим от боевого
# ---------------------------------------------------------------------------
def test_tenant_announces_itself_as_a_stand(seeded):
    tenant = Tenant.all_objects.get(slug=TENANT_SLUG)
    assert tenant.slug == "tech-probe"
    assert "НЕ БОЕВОЙ САЛОН" in tenant.name
    assert tenant.is_active is False  # замок 1
    master = SpecialistProfile.objects.get(tenant=tenant)
    assert "Техстенд" in master.display_name
    assert master.status == SpecialistProfile.ProfileStatus.PENDING  # замок 2
    assert master.is_booking_enabled is False  # замок 3
    assert all("Техстенд" in o.name for o in SalonService.objects.filter(tenant=tenant))


def test_foreign_tenant_on_the_same_slug_is_refused(db):
    """Слаг занят не нами — отказ, а не запись поверх чужих строк."""
    Tenant.all_objects.create(slug=TENANT_SLUG, name="Чей-то настоящий салон")

    with pytest.raises(CommandError, match="уже занят"):
        _run("--apply")


# ---------------------------------------------------------------------------
# 3. Идемпотентность
# ---------------------------------------------------------------------------
def test_second_run_does_not_duplicate(seeded):
    before = {
        "tenants": Tenant.all_objects.count(),
        "users": User.objects.count(),
        "offers": SalonService.objects.count(),
        "edges": SpecialistService.objects.count(),
        "hours": SpecialistWorkingHours.objects.count(),
        "appointments": Appointment.objects.count(),
    }

    text = _run("--apply")

    assert Tenant.all_objects.count() == before["tenants"]
    assert User.objects.count() == before["users"]
    assert SalonService.objects.count() == before["offers"]
    assert SpecialistService.objects.count() == before["edges"]
    assert SpecialistWorkingHours.objects.count() == before["hours"]
    assert Appointment.objects.count() == before["appointments"]
    assert "created=0" in text


def test_second_run_does_not_silently_wipe_a_probe_result(seeded):
    """Проверка отменила запись — второй запуск обязан её СОХРАНИТЬ.

    Молчаливый сброс уничтожил бы доказательство прогона. Вернуть
    объявленное состояние можно только явным ``--reset``.
    """
    appt = Appointment.objects.get(pk=IDS["appt_cancel_target"])
    assert appt.status == Appointment.Status.CONFIRMED
    Appointment.objects.filter(pk=appt.pk).update(
        status=Appointment.Status.CANCELLED, cancellation_reason="probe run", version=2,
    )

    text = _run("--apply")

    survived = Appointment.objects.get(pk=IDS["appt_cancel_target"])
    assert survived.status == Appointment.Status.CANCELLED
    assert survived.cancellation_reason == "probe run"
    assert survived.version == 2
    assert "kept=4" in text


def test_reset_returns_the_probe_appointments_to_the_declared_shape(seeded):
    Appointment.objects.filter(pk=IDS["appt_cancel_target"]).update(
        status=Appointment.Status.CANCELLED, version=2,
    )

    text = _run("--apply", "--reset")

    restored = Appointment.objects.get(pk=IDS["appt_cancel_target"])
    assert restored.status == Appointment.Status.CONFIRMED
    assert restored.version == 1
    assert "reset=4" in text
    assert "kept=0" in text


def test_drift_in_the_skeleton_is_restored_and_named(seeded):
    """Затирание каркаса допустимо, но обязано быть НАЗВАНО построчно."""
    SalonService.objects.filter(pk=IDS["offer_safe"]).update(name="кто-то переименовал")

    text = _run("--apply")

    assert SalonService.objects.get(pk=IDS["offer_safe"]).name.startswith("Техстенд")
    assert "restored ← offer_safe: name" in text


def test_activate_does_not_switch_the_master_back_off(seeded):
    """Владелец включил стенд — повторный сид не вправе это отменить."""
    _run("--activate", "--apply")
    assert Tenant.all_objects.get(pk=IDS["tenant"]).is_active is True

    _run("--apply")

    master = SpecialistProfile.objects.get(tenant_id=IDS["tenant"])
    assert master.status == SpecialistProfile.ProfileStatus.ACTIVE
    assert master.is_booking_enabled is True


# ---------------------------------------------------------------------------
# 4. Три состояния здоровья: неизвестное — это NULL, а не False
# ---------------------------------------------------------------------------
def test_health_unknown_offer_stores_absence_not_an_invented_no(seeded):
    """Дефект PR #307 в фикстурном исполнении: `False` вместо `None`.

    Салон на вопрос НЕ ОТВЕЧАЛ. Записанное `False` было бы
    положительным утверждением о безопасности, которого никто не делал.
    """
    offer = SalonService.objects.get(pk=IDS["offer_health_unknown"])
    assert offer.requires_health_check is None
    assert offer.template_id is None

    edge = SpecialistService.objects.get(pk=IDS["edge_health_unknown"])
    resolved = edge.resolved_requires_health_check()
    assert resolved is None
    assert resolved is not False


@pytest.mark.parametrize(
    "edge_key, expected",
    [
        ("edge_safe", False),
        ("edge_health_required", True),
        ("edge_health_unknown", None),
        ("edge_unmapped", False),
    ],
)
def test_all_three_health_states_are_present(seeded, edge_key, expected):
    edge = SpecialistService.objects.get(pk=IDS[edge_key])
    assert edge.resolved_requires_health_check() is expected


def test_safe_service_says_no_rather_than_staying_silent(seeded):
    """У безопасной услуги `False` — сказанное «нет», а не молчание."""
    offer = SalonService.objects.get(pk=IDS["offer_safe"])
    assert offer.requires_health_check is False


def test_verified_and_unmapped_offers_both_exist(seeded):
    statuses = {
        key: SalonService.objects.get(pk=IDS[key]).mapping_status
        for key in ("offer_safe", "offer_health_required", "offer_health_unknown", "offer_unmapped")
    }
    assert statuses["offer_safe"] == SalonService.MappingStatus.VERIFIED
    assert statuses["offer_health_required"] == SalonService.MappingStatus.VERIFIED
    assert statuses["offer_unmapped"] == SalonService.MappingStatus.UNMAPPED
    assert statuses["offer_health_unknown"] == SalonService.MappingStatus.UNMAPPED

    verified = SalonService.objects.get(pk=IDS["offer_safe"])
    # VERIFIED без провенанса невозможен на уровне схемы; фикстура
    # обязана нести настоящее основание, а не выдуманного человека.
    assert verified.mapping_confirmed_by_id is None
    assert verified.mapping_confirmed_rule
    assert verified.mapping_rule_version
    assert verified.mapping_source_ref


def test_unmapped_offer_still_has_a_known_health_answer(seeded):
    """Оси разведены: проверка про разметку не спотыкается о здоровье."""
    offer = SalonService.objects.get(pk=IDS["offer_unmapped"])
    assert offer.mapping_status == SalonService.MappingStatus.UNMAPPED
    assert offer.requires_health_check is False


# ---------------------------------------------------------------------------
# 5. Выходной с пустыми временами
# ---------------------------------------------------------------------------
def test_day_off_carries_empty_times(seeded):
    """На боевом лежат пять строк «выходной с 09:00 до 21:00». Не здесь."""
    rows = SpecialistWorkingHours.objects.filter(specialist__tenant_id=IDS["tenant"])
    assert rows.count() == 7

    days_off = [r for r in rows if not r.is_working_day]
    assert len(days_off) == 1
    for row in days_off:
        assert row.start_time is None
        assert row.end_time is None
        assert row.break_start is None
        assert row.break_end is None

    working = [r for r in rows if r.is_working_day]
    assert len(working) == 6
    assert all(r.start_time is not None and r.end_time is not None for r in working)


# ---------------------------------------------------------------------------
# Манифест — то, чем проверка адресует строки
# ---------------------------------------------------------------------------
def test_manifest_reports_the_three_health_states(seeded):
    payload = seeded[seeded.index("{"):]
    manifest = json.loads(payload)

    assert manifest["fixture"] == "tech-probe"
    assert manifest["tenant_slug"] == TENANT_SLUG
    assert manifest["tenant_is_active"] is False
    assert manifest["resolved_requires_health_check"] == {
        "safe": False,
        "health_required": True,
        "health_unknown": None,
        "unmapped": False,
    }
    assert manifest["working_hours"]["day_off_times_are_null"] is True
    assert set(manifest["appointments"]) == {
        "cancel_target", "reschedule_target", "occupied", "already_cancelled",
    }
    assert manifest["candidate_slots"]["occupied"] == manifest["appointments"]["occupied"]["starts_at"]
