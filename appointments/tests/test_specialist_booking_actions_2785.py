"""Мастер над своей клиентской записью из бота — DRF-2785 (вариант «в» владельца 05.10).

Что заперто:

- «Подтверждаю» (``acknowledge``) статус НЕ меняет: запись остаётся CONFIRMED,
  пишется факт (время + версия) и ровно одно событие ``booking.acknowledged``;
  повтор на той же версии — 200 без второго события; после переноса —
  подтверждение снова пишется, уже для нового времени; устаревшая версия —
  409 и ничего не записано; отменённую подтвердить нельзя — 422;
- «Не смогу» (``cancel``) — отмена мастером: ``cancelled_by=master``,
  ``reason_code=master_unavailable``, возврат 100 % (без вины клиента);
- закрыть визит / неявка / перенос — актор ``specialist``, те же функции,
  что у салона и мобильного пути;
- на ЧУЖОЙ записи (запись другого мастера) каждая ручка отвечает 404 и
  ничего не меняет;
- субъект: чужой профиль в URL, без заголовка, токен провижининга, прокси без
  связи — 403 на каждой из пяти ручек (на них ссылается перепись маршрутов
  ``users/tests/test_internal_subject_authorization.py``);
- в ответе нет людей: только идентификаторы, статус, версия, время.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from appointments.models import Appointment, AppointmentRevision, OutboxEvent
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token-2785"  # noqa: S105
PROVISIONING_TOKEN = "test-provisioning-only-token-2785"  # noqa: S105
MASTER_EXT = "bot:max:2785001"
OTHER_EXT = "bot:max:2785002"

ACTIONS = ("acknowledge", "cancel", "complete", "no-show", "reschedule")

#: Ровно эти ключи — и ни одного про человека (имя, телефон, клиент).
STATE_KEYS = {
    "appointment_id", "specialist_id", "status", "version", "start_at", "end_at",
    "master_acknowledged_at", "master_acknowledged_version", "acknowledged",
}


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN
    settings.AYLA_IDENTITY_PROVISIONING_TOKEN = PROVISIONING_TOKEN


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="sb2785", name="SB Salon")


def _master(salon, *, username, phone, external_id):
    user = User.objects.create_user(username=username, password="x", role="specialist", phone=phone)
    user.tenant = salon
    user.save(update_fields=["tenant"])
    profile = SpecialistProfile.objects.get(user=user)
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.tenant = salon
    profile.timezone = "Europe/Moscow"
    profile.save()
    if external_id:
        User.objects.create(
            username=external_id, role="client", is_proxy=True, is_guest=False, linked_user=user,
        )
    return profile


@pytest.fixture
def master(salon):
    return _master(salon, username="sb2785_m1", phone="+79995402001", external_id=MASTER_EXT)


@pytest.fixture
def other(salon):
    return _master(salon, username="sb2785_m2", phone="+79995402002", external_id=OTHER_EXT)


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username="sb2785_c", password="x", role="client", phone="+79995402009",
    )


def _booking(master, customer, *, hours_from_now=48, status=Appointment.Status.CONFIRMED):
    category, _ = ServiceCategory.objects.get_or_create(name="SB2785", defaults={"slug": "sb2785-cat"})
    service = Service.objects.create(
        specialist=master, category=category, name="Стрижка",
        price=Decimal("2000.00"), duration_minutes=60, is_active=True,
    )
    start = (timezone.now() + timedelta(hours=hours_from_now)).replace(minute=0, second=0, microsecond=0)
    return Appointment.objects.create(
        tenant=master.tenant, client=customer, specialist=master, service=service,
        salon_service=None, start_datetime=start, end_datetime=start + timedelta(hours=1),
        status=status, price=Decimal("2000.00"),
    )


def _api(external_id=MASTER_EXT, token=RUNTIME_TOKEN) -> APIClient:
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    if external_id:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_id
    return c


def _url(specialist_id, appointment_id, action):
    return f"/api/v1/internal/specialists/{specialist_id}/appointments/{appointment_id}/{action}/"


def _body(action, appointment):
    if action == "reschedule":
        return {
            "new_start_datetime": (appointment.start_datetime + timedelta(days=1)).isoformat(),
            "expected_version": appointment.version,
        }
    if action == "cancel":
        return {}
    return {"expected_version": appointment.version}


def _events(topic):
    return list(OutboxEvent.objects.filter(topic=topic))


class TestAcknowledge:
    def test_own_booking_is_acknowledged_without_changing_its_status(self, master, customer):
        appt = _booking(master, customer)

        resp = _api().post(_url(master.pk, appt.pk, "acknowledge"), {"expected_version": 1}, format="json")

        assert resp.status_code == 200, resp.content
        data = resp.data["data"]
        assert data["recorded"] is True
        assert data["acknowledged"] is True
        assert data["status"] == "confirmed"
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED
        assert appt.master_acknowledged_version == 1
        assert appt.master_acknowledged_at is not None
        [event] = _events(OutboxEvent.Topic.BOOKING_ACKNOWLEDGED)
        assert event.payload["event_name"] == "booking.acknowledged"
        assert event.payload["data"]["appointment_id"] == str(appt.pk)
        assert event.payload["data"]["version"] == 1
        assert event.payload["data"]["acknowledged_by"] == "specialist"
        # Адресат бота — клиент (event-contract §2.2), не нажавший мастер.
        assert event.payload["user_id"] == str(customer.pk)

    def test_a_repeat_on_the_same_version_writes_no_second_event(self, master, customer):
        appt = _booking(master, customer)
        api = _api()
        api.post(_url(master.pk, appt.pk, "acknowledge"), {"expected_version": 1}, format="json")

        resp = api.post(_url(master.pk, appt.pk, "acknowledge"), {"expected_version": 1}, format="json")

        assert resp.status_code == 200, resp.content
        assert resp.data["data"]["recorded"] is False
        assert len(_events(OutboxEvent.Topic.BOOKING_ACKNOWLEDGED)) == 1

    def test_after_a_reschedule_the_old_acknowledgement_is_stale_and_can_be_renewed(self, master, customer):
        appt = _booking(master, customer)
        api = _api()
        api.post(_url(master.pk, appt.pk, "acknowledge"), {"expected_version": 1}, format="json")
        moved = api.post(_url(master.pk, appt.pk, "reschedule"), _body("reschedule", appt), format="json")
        assert moved.status_code == 200, moved.content
        assert moved.data["data"]["version"] == 2
        assert moved.data["data"]["acknowledged"] is False, "подтверждение старого времени не переносится"

        resp = api.post(_url(master.pk, appt.pk, "acknowledge"), {"expected_version": 2}, format="json")

        assert resp.status_code == 200, resp.content
        assert resp.data["data"]["recorded"] is True
        assert resp.data["data"]["master_acknowledged_version"] == 2
        assert len(_events(OutboxEvent.Topic.BOOKING_ACKNOWLEDGED)) == 2

    def test_a_stale_version_is_409_and_nothing_is_written(self, master, customer):
        appt = _booking(master, customer)

        resp = _api().post(_url(master.pk, appt.pk, "acknowledge"), {"expected_version": 7}, format="json")

        assert resp.status_code == 409, resp.content
        assert resp.data["error"]["code"] == "STALE_VERSION"
        appt.refresh_from_db()
        assert appt.master_acknowledged_at is None
        assert _events(OutboxEvent.Topic.BOOKING_ACKNOWLEDGED) == []

    def test_a_cancelled_booking_cannot_be_acknowledged(self, master, customer):
        appt = _booking(master, customer, status=Appointment.Status.CANCELLED)

        resp = _api().post(_url(master.pk, appt.pk, "acknowledge"), {"expected_version": 1}, format="json")

        assert resp.status_code == 422, resp.content
        assert resp.data["error"]["code"] == "INVALID_STATUS"
        assert _events(OutboxEvent.Topic.BOOKING_ACKNOWLEDGED) == []


class TestCancel:
    def test_own_booking_is_cancelled_by_the_master_without_client_fault(self, master, customer):
        appt = _booking(master, customer)

        resp = _api().post(_url(master.pk, appt.pk, "cancel"), {"reason": "заболела"}, format="json")

        assert resp.status_code == 200, resp.content
        assert resp.data["data"]["status"] == "cancelled"
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CANCELLED
        assert appt.cancelled_by_id == master.user_id
        [event] = _events(OutboxEvent.Topic.BOOKING_CANCELLED)
        data = event.payload["data"]
        assert data["cancelled_by"] == "master"
        assert data["reason_code"] == "master_unavailable"
        assert data["initiator_role"] == "specialist"
        assert data["refund_percent"] == 100.0

    def test_a_stale_version_is_409_and_the_booking_stays(self, master, customer):
        appt = _booking(master, customer)

        resp = _api().post(_url(master.pk, appt.pk, "cancel"), {"expected_version": 5}, format="json")

        assert resp.status_code == 409, resp.content
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED


class TestCloseAndMove:
    def test_complete_closes_the_visit_as_the_specialist(self, master, customer):
        appt = _booking(master, customer, hours_from_now=-3)

        resp = _api().post(_url(master.pk, appt.pk, "complete"), {"expected_version": 1}, format="json")

        assert resp.status_code == 200, resp.content
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.COMPLETED
        assert appt.completed_by == "specialist"
        [event] = _events(OutboxEvent.Topic.BOOKING_COMPLETED)
        assert event.payload["data"]["completed_by"] == "specialist"

    def test_no_show_is_marked_by_the_specialist(self, master, customer):
        appt = _booking(master, customer, hours_from_now=-3)

        resp = _api().post(_url(master.pk, appt.pk, "no-show"), {"expected_version": 1}, format="json")

        assert resp.status_code == 200, resp.content
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.NO_SHOW
        assert appt.no_show_marked_by == "specialist"

    def test_reschedule_is_recorded_as_the_specialists_from_the_bot(self, master, customer):
        appt = _booking(master, customer)

        resp = _api().post(_url(master.pk, appt.pk, "reschedule"), _body("reschedule", appt), format="json")

        assert resp.status_code == 200, resp.content
        revision = AppointmentRevision.objects.get(appointment=appt)
        assert revision.actor_role == "specialist"
        assert revision.basis == "internal_bot"
        assert revision.actor_id == master.user_id


class TestForeignBooking:
    @pytest.mark.parametrize("action", ACTIONS)
    def test_another_masters_booking_is_404_and_untouched(self, action, master, other, customer):
        """Мастер ``other`` законно называет СЕБЯ, но запись — мастера ``master``."""
        appt = _booking(master, customer, hours_from_now=-3 if action in ("complete", "no-show") else 48)

        resp = _api(OTHER_EXT).post(_url(other.pk, appt.pk, action), _body(action, appt), format="json")

        assert resp.status_code == 404, resp.content
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED
        assert appt.version == 1
        assert appt.master_acknowledged_at is None
        assert OutboxEvent.objects.count() == 0

    @pytest.mark.parametrize("action", ACTIONS)
    def test_own_booking_positive_control(self, action, master, customer):
        """Положительная пара к 404 выше: тот же вызов от владельца записи проходит."""
        appt = _booking(master, customer, hours_from_now=-3 if action in ("complete", "no-show") else 48)

        resp = _api().post(_url(master.pk, appt.pk, action), _body(action, appt), format="json")

        assert resp.status_code == 200, resp.content
        assert STATE_KEYS <= set(resp.data["data"]), resp.data["data"]
        assert set(resp.data["data"]) - STATE_KEYS <= {"recorded"}, "в ответе лишнее — возможно, люди"


class TestSubject:
    @pytest.mark.parametrize("action", ACTIONS)
    def test_a_foreign_profile_in_the_url_is_403(self, action, master, other, customer):
        appt = _booking(master, customer)
        resp = _api(OTHER_EXT).post(_url(master.pk, appt.pk, action), _body(action, appt), format="json")
        assert resp.status_code == 403, resp.content

    @pytest.mark.parametrize("action", ACTIONS)
    def test_an_unnamed_caller_is_403(self, action, master, customer):
        appt = _booking(master, customer)
        resp = _api(external_id=None).post(_url(master.pk, appt.pk, action), _body(action, appt), format="json")
        assert resp.status_code == 403, resp.content

    @pytest.mark.parametrize("action", ACTIONS)
    def test_the_provisioning_credential_is_403(self, action, master, customer):
        appt = _booking(master, customer)
        resp = _api(token=PROVISIONING_TOKEN).post(
            _url(master.pk, appt.pk, action), _body(action, appt), format="json",
        )
        assert resp.status_code == 403, resp.content

    @pytest.mark.parametrize("action", ACTIONS)
    def test_an_unlinked_proxy_is_403(self, action, master, customer):
        appt = _booking(master, customer)
        User.objects.create(username="bot:max:2785999", role="client", is_proxy=True, is_guest=False)
        resp = _api("bot:max:2785999").post(_url(master.pk, appt.pk, action), _body(action, appt), format="json")
        assert resp.status_code == 403, resp.content
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED


class TestReviewFindings:
    """Три находки ревью (DRF-2785) — каждая своим узлом."""

    def test_cancel_version_is_checked_under_the_lock_not_on_the_views_read(
        self, master, customer, monkeypatch,
    ):
        """Перенос закоммичен между чтением ручки и блокировкой сервиса.

        Ручка видит снимок v1 (мастер видел 15:00), строка уже v2. Проверка на
        чтении ручки этого не заметит; заметить обязан сервис под блокировкой.
        """
        from appointments.internal_specialist_api import InternalSpecialistBookingCancelView

        appt = _booking(master, customer)
        stale = Appointment.objects.select_related("specialist", "specialist__user").get(pk=appt.pk)
        Appointment.objects.filter(pk=appt.pk).update(version=2)
        monkeypatch.setattr(
            InternalSpecialistBookingCancelView, "_own_booking",
            staticmethod(lambda specialist_id, appointment_id: stale),
        )

        resp = _api().post(_url(master.pk, appt.pk, "cancel"), {"expected_version": 1}, format="json")

        assert resp.status_code == 409, resp.content
        assert resp.data["error"]["code"] == "STALE_VERSION"
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED
        assert _events(OutboxEvent.Topic.BOOKING_CANCELLED) == []

    @pytest.mark.parametrize("action", ACTIONS)
    def test_the_draft_workspace_claim_opens_no_booking_write(self, action, salon, customer):
        """Профиль DRAFT с заявкой на кабинет и с НАСТОЯЩЕЙ записью (модерация
        вернула его в DRAFT). Заголовок-заявка без связи — 403 на каждой ручке."""
        claim = "bot:max:2785777"
        profile = _master(salon, username="sb2785_draft", phone="+79995402003", external_id=None)
        appt = _booking(profile, customer, hours_from_now=-3 if action in ("complete", "no-show") else 48)
        SpecialistProfile.objects.filter(pk=profile.pk).update(
            status=SpecialistProfile.ProfileStatus.DRAFT, provisioned_external_user_id=claim,
        )

        resp = _api(claim).post(_url(profile.pk, appt.pk, action), _body(action, appt), format="json")

        assert resp.status_code == 403, resp.content
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED
        assert OutboxEvent.objects.count() == 0

    def test_the_claim_itself_is_still_honoured_by_the_workspace_gate(self, salon):
        """Положительная пара: та же заявка проходит общий сторож кабинета —
        закрыта именно поверхность записей, а не заявка вообще."""
        from users.permissions import (
            IsInternalBearerForLinkedSpecialistSubject,
            IsInternalBearerForSpecialistSubject,
        )

        claim = "bot:max:2785778"
        profile = _master(salon, username="sb2785_draft2", phone="+79995402004", external_id=None)
        SpecialistProfile.objects.filter(pk=profile.pk).update(
            status=SpecialistProfile.ProfileStatus.DRAFT, provisioned_external_user_id=claim,
        )
        owner = IsInternalBearerForSpecialistSubject().provisioned_workspace_owner(claim, str(profile.pk), None)
        assert owner is not None and owner.pk == profile.user_id
        assert IsInternalBearerForLinkedSpecialistSubject().provisioned_workspace_owner(
            claim, str(profile.pk), None,
        ) is None

    @pytest.mark.parametrize("action", ACTIONS)
    def test_a_master_whose_staff_relationship_was_revoked_is_404(self, action, master, customer):
        """Узел, который проходит МИМО фильтра запроса: запись своя, мастер
        связан — отказывает только ``_authority``."""
        from users.models import TenantUserRelationship

        appt = _booking(master, customer, hours_from_now=-3 if action in ("complete", "no-show") else 48)
        revoked = TenantUserRelationship.objects.filter(
            user=master.user, tenant=master.tenant, is_active=True,
        ).update(is_active=False)
        assert revoked >= 1, "положительный контроль: связь мастера с салоном была"

        resp = _api().post(_url(master.pk, appt.pk, action), _body(action, appt), format="json")

        assert resp.status_code == 404, resp.content
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED
        assert OutboxEvent.objects.count() == 0

    def test_a_deactivated_salon_is_404(self, master, customer):
        appt = _booking(master, customer)
        Tenant.all_objects.filter(pk=master.tenant_id).update(is_active=False)

        resp = _api().post(_url(master.pk, appt.pk, "cancel"), {}, format="json")

        assert resp.status_code == 404, resp.content
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED
