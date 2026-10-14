"""Очистка синтетического тестового набора Плана (``purge_synthetic_plan_fixture``).

Решение владельца 10.10.2026: команда безопасной очистки с пробным прогоном,
без автоматического исполнения. Узлы держат:

* после засева очистка убирает весь набор и ничего, кроме него; набор можно
  засеять снова;
* пробный прогон ничего не меняет; без названного режима команда отказывает;
* строка с именем из набора, но без пометки синтетики, — остановка без
  единого удаления;
* следы прогона (запись, выбор услуги в шаге плана) команда не удаляет: то,
  что они держат, остаётся, а тест-мастер выводится из продажи и остаётся
  скрытым из обычной выдачи.

Пределы: запись здесь создаётся прямой строкой, без события и оплаты — узел
проверяет запрет удаления, а не то, что запись порождает вокруг себя.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from appointments.models import Appointment, SpecialistWorkingHours
from services.management.commands._synthetic_plan_fixture import SERVICE_USERNAME, FixtureRefused, seed
from services.management.commands._synthetic_plan_purge import purge
from services.models import ProcedureCapability, SalonService, ServiceTemplate, SpecialistService
from services.tests.test_synthetic_plan_fixture_seed import NOTHING, SEEDED, _census, spec, world  # noqa: F401
from users.models import Profile, SpecialistProfile, TenantUserRelationship, User
from users.sellable import sellable_q, synthetic_offer_master_q

pytestmark = pytest.mark.django_db

MASTER = "synthetic-test-master:event"


def _everything() -> dict:
    """Счёт по всем таблицам, которых касается набор, — настоящее вместе с синтетикой."""
    return {
        "canons": ServiceTemplate.objects.count(), "capabilities": ProcedureCapability.objects.count(),
        "offers": SalonService.objects.count(), "edges": SpecialistService.objects.count(),
        "users": User.objects.count(), "profiles": Profile.objects.count(),
        "masters": SpecialistProfile.objects.count(), "relations": TenantUserRelationship.objects.count(),
        "hours": SpecialistWorkingHours.objects.count(),
    }


@pytest.fixture
def neighbours(world):  # noqa: F811
    """Настоящие строки рядом с набором: другой мастер того же салона со своей услугой и часами."""
    user = User(username="purge-real-master", role="specialist", tenant=world["salon"])
    user.set_unusable_password()
    user.save()
    master = SpecialistProfile.objects.get(user=user)
    canon = ServiceTemplate.objects.create(category=world["category"], name="Настоящий канон рядом")
    offer = SalonService.objects.create(
        tenant=world["salon"], category=world["category"], name="Настоящая услуга рядом", template=canon,
        duration_minutes=60, base_price=Decimal("1000.00"),
    )
    SpecialistWorkingHours.objects.create(
        specialist=master, day_of_week=0, is_working_day=True, start_time="10:00", end_time="18:00",
    )
    return {"master": master, "canon": canon, "offer": offer}


# ─── полный круг ─────────────────────────────────────────────────────────────


def test_the_purge_removes_the_whole_set_and_nothing_else(spec, neighbours) -> None:  # noqa: F811
    before = _everything()
    seed(spec)
    assert _census() == SEEDED

    report = purge(spec, dry_run=False)

    assert _census() == NOTHING
    assert _everything() == before
    assert not report.kept and not report.disabled and not report.run_traces, report
    assert not User.objects.filter(username__in=(MASTER, SERVICE_USERNAME)).exists()
    assert SalonService.objects.filter(pk=neighbours["offer"].pk).exists()
    assert SpecialistWorkingHours.objects.filter(specialist=neighbours["master"]).count() == 1


def test_the_set_can_be_seeded_again_after_the_purge(spec) -> None:  # noqa: F811
    seed(spec)
    purge(spec, dry_run=False)

    again = seed(spec)

    assert _census() == SEEDED
    assert not again.found


def test_the_dry_run_changes_nothing_and_reports_the_same(spec, neighbours) -> None:  # noqa: F811
    seed(spec)
    before = _everything()

    report = purge(spec, dry_run=True)

    assert _everything() == before and _census() == SEEDED
    assert len(report.removed) == 9, report.removed  # предложение, услуга, 3 способности, 2 канона, мастер, служебный


def test_a_purge_of_an_empty_base_finds_nothing_and_removes_nothing(spec, neighbours) -> None:  # noqa: F811
    before = _everything()

    report = purge(spec, dry_run=False)

    assert _everything() == before
    assert not report.removed and len(report.absent) == 8, report.absent


# ─── только помеченное ───────────────────────────────────────────────────────


@pytest.mark.parametrize("impostor", ["canon", "capability", "account"])
def test_a_row_named_like_the_set_but_not_of_it_stops_the_purge(spec, world, impostor) -> None:  # noqa: F811
    """Имя из набора — не пометка. Отказ до первого удаления: остальной набор остаётся целым."""
    seed(spec)
    victims = {}
    if impostor == "canon":
        # Канон набора нельзя «разметить обратно» — пометка неизменяема; берётся настоящий тёзка второго канона.
        name = spec["templates"][1]["name"]
        ProcedureCapability.objects.filter(key="synthetic_event_hands").delete()
        ServiceTemplate.objects.filter(name=name).delete()
        victims["canon"] = ServiceTemplate.objects.create(category=world["nails"], name=name)
    elif impostor == "capability":
        ProcedureCapability.objects.filter(key="synthetic_event_hands").delete()
        canon = ServiceTemplate.objects.create(category=world["nails"], name="Настоящий канон способности")
        victims["capability"] = ProcedureCapability.objects.create(
            key="synthetic_event_hands", templates=[canon], text_client="Настоящая способность-тёзка",
        )
    else:
        other = type(world["salon"]).objects.create(slug="purge-other-salon", name="Другой салон")
        User.objects.filter(username=MASTER).update(tenant=other)
        SpecialistProfile.objects.filter(user__username=MASTER).update(tenant=other)
    before = _everything()

    with pytest.raises(FixtureRefused) as refused:
        purge(spec, dry_run=False)

    assert refused.value.reason in ("not_a_synthetic_row", "not_the_seeded_master")
    assert _everything() == before
    assert SalonService.objects.filter(synthetic=True).count() == 1


# ─── следы прогона держат строки ─────────────────────────────────────────────


def test_a_booking_keeps_the_service_and_the_master_and_takes_him_off_sale(spec, world) -> None:  # noqa: F811
    seed(spec)
    offer = SalonService.objects.get(synthetic=True)
    master = SpecialistProfile.objects.get(user__username=MASTER)
    now = timezone.now()
    booking = Appointment.objects.create(
        client=world["persona"], specialist=master, salon_service=offer,
        start_datetime=now + timedelta(days=2), end_datetime=now + timedelta(days=2, hours=2),
        status=Appointment.Status.CONFIRMED, price=Decimal("3500.00"),
    )
    assert SpecialistProfile.objects.filter(sellable_q(), pk=master.pk).exists()  # контроль: до очистки продаётся

    report = purge(spec, dry_run=False)

    # След прогона на месте, и то, на что он ссылается, — тоже.
    assert Appointment.objects.filter(pk=booking.pk).exists()
    assert SalonService.objects.filter(pk=offer.pk).exists()
    assert SpecialistProfile.objects.filter(pk=master.pk).exists()
    assert ServiceTemplate.objects.filter(pk=offer.template_id).exists()
    assert report.run_traces == {"записи на синтетическую услугу": 1, "записи к тест-мастеру": 1}
    # Мастер не продаётся и не входит — записаться к нему больше нельзя.
    assert not SpecialistProfile.objects.filter(sellable_q(), pk=master.pk).exists()
    # И остаётся скрытым из обычной выдачи: признак, по которому его
    # исключают, — предложение по помеченной услуге — на месте.
    assert SpecialistService.objects.filter(salon_service=offer, specialist=master).exists()
    assert not SpecialistProfile.objects.exclude(synthetic_offer_master_q()).filter(pk=master.pk).exists()
    assert not SpecialistWorkingHours.objects.filter(specialist=master).exists()
    assert User.objects.get(username=MASTER).is_active is False
    assert len(report.disabled) == 1
    # Знание и канон без услуг ушли: их запись не держит.
    assert not ProcedureCapability.objects.filter(synthetic=True).exists()
    assert list(ServiceTemplate.objects.filter(synthetic=True).values_list("pk", flat=True)) == [offer.template_id]
    kept = " | ".join(report.kept)
    assert "услуга" in kept and "тест-мастер" in kept and "служебный пользователь" in kept, kept
    assert "оставлено намеренно" in kept, kept


def test_a_step_choice_keeps_the_service_but_not_the_master(spec, world, settings) -> None:  # noqa: F811
    """Выбор услуги в шаге плана держит услугу и её канон; мастера не держит — он удаляется."""
    from wellness.models import PlanStepResolution
    from wellness.tests.test_plan_engine_steps_2868 import _save, _step
    from goals.models import ClientGoal

    settings.PLAN_ENGINE_ENABLED = True
    # План на синтетической способности сохраняется только под разрешением тестовой персоны.
    settings.SYNTHETIC_TEST_DATA_ENABLED = True
    settings.SYNTHETIC_TEST_SUBJECT_IDS = [str(world["persona"].pk)]
    seed(spec)
    offer = SalonService.objects.get(synthetic=True)
    goal = ClientGoal.objects.create(client=world["persona"], goal_key="event", source_channel="bot")
    plan = _save(goal, steps=[_step("s1", capability_ref="synthetic_event_hair", outcome_ref="event")])
    PlanStepResolution.objects.create(
        plan_revision=plan.current_revision, step_id="s1", level=PlanStepResolution.Level.OFFER,
        tenant_offer=offer, canonical_service=offer.template, resolver_decision_id="purge-test",
        safety_state="NORMAL", safety_policy_version="purge-test", safety_evaluated_at_revision=1,
    )

    report = purge(spec, dry_run=False)

    assert SalonService.objects.filter(pk=offer.pk).exists()
    assert report.run_traces == {"выбор синтетической услуги в шаге плана": 1}
    assert not User.objects.filter(username=MASTER).exists()
    assert not report.disabled


# ─── команда ─────────────────────────────────────────────────────────────────


def _spec_file(tmp_path, spec) -> str:  # noqa: F811
    import json

    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_the_command_refuses_without_a_named_mode(spec, tmp_path) -> None:  # noqa: F811
    seed(spec)

    with pytest.raises(CommandError, match="Режим не назван"):
        call_command("purge_synthetic_plan_fixture", spec=_spec_file(tmp_path, spec), stdout=StringIO())

    assert _census() == SEEDED


def test_the_command_dry_run_prints_and_keeps_and_apply_removes(spec, tmp_path) -> None:  # noqa: F811
    seed(spec)
    path = _spec_file(tmp_path, spec)

    shown = StringIO()
    call_command("purge_synthetic_plan_fixture", spec=path, dry_run=True, stdout=shown)
    assert "ПРОБНЫЙ ПРОГОН" in shown.getvalue() and "Было бы удалено (9)" in shown.getvalue()
    assert "зеркале каталога у бота" in shown.getvalue()
    assert _census() == SEEDED

    done = StringIO()
    call_command("purge_synthetic_plan_fixture", spec=path, apply=True, stdout=done)
    assert "Очистка выполнена." in done.getvalue() and "Удалено (9)" in done.getvalue()
    assert _census() == NOTHING
