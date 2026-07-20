"""C7 — Client Payments contract tests (§7.5 v1.7.0, mock YooKassa).

Pins:
- C7.1: internal payment create — two-stage hold, amount ONLY from the
  Booking snapshot (client-sent amounts ignored), idempotent repeat,
  404/409/403/422 matrix;
- C7.2: card binding — separate voluntary action with consent_version,
  save_payment_method: true at setup, method persisted ONLY on
  provider-confirmed payment_method.saved == true, list active,
  revoke → charge forbidden;
- C7.6: ownership boundary — X-External-User-ID scope + client_id
  cross-check, foreign ids → 403/404.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment
from payments.models import Payment, UserPaymentMethod
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User

VALID_TOKEN = "test-ayla-internal-token-c7"
EXTERNAL_USER_ID = "bot:c7"
WEBHOOK_URL = "/api/v1/payments/webhook/"


def _payment_url(appointment_id) -> str:
    return f"/api/v1/internal/appointments/{appointment_id}/payment/"


def _cards_url(user_id) -> str:
    return f"/api/v1/internal/users/{user_id}/cards/"


def _card_setup_url(user_id) -> str:
    return f"/api/v1/internal/users/{user_id}/cards/setup/"


def _card_delete_url(user_id, card_id) -> str:
    return f"/api/v1/internal/users/{user_id}/cards/{card_id}/"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def customer(db):
    # resolve_external_user resolves bot:c7 by username (proxy actor).
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79993007001", is_proxy=True,
    )


@pytest.fixture
def other_user(db):
    return User.objects.create_user(
        username="c7_other_client", password="x", role="client",
        phone="+79993007002",
    )


@pytest.fixture
def specialist(db):
    u = User.objects.create_user(
        username="c7_spec", password="x", role="specialist",
        phone="+79993007003",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.display_name = "C7 Spec"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.yookassa_account_id = "yk-subacc-c7"
    p.save()
    return p


@pytest.fixture
def service(db, specialist):
    cat = ServiceCategory.objects.create(name="C7 Cat", slug="c7-cat")
    return Service.objects.create(
        specialist=specialist, category=cat, name="C7 Service",
        price=Decimal("2000.00"), duration_minutes=60, is_active=True,
    )


@pytest.fixture
def appointment(db, customer, specialist, service):
    now = datetime.now(tz=timezone.utc)
    return Appointment.objects.create(
        client=customer, specialist=specialist, service=service,
        start_datetime=now + timedelta(hours=3),
        end_datetime=now + timedelta(hours=4),
        status=Appointment.Status.AWAITING_PAYMENT,
        price=service.price,
        snapshot_service_name=service.name,
        snapshot_price=service.price,
    )


def _api(*, bearer=VALID_TOKEN, external_user_id=EXTERNAL_USER_ID):
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if external_user_id is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


def _mock_create_result():
    return {
        "provider_payment_id": "yk_c7_hold_1",
        "confirmation_url": "https://yookassa.ru/confirm/c7",
        "status": "pending",
        "platform_fee": Decimal("90.00"),
        "specialist_income": Decimal("1910.00"),
    }


# ---------------------------------------------------------------------------
# C7.1 — internal payment create
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestInternalPaymentCreate:
    def _post(self, appointment, client_id, **overrides):
        body = {
            "client_id": str(client_id),
            "return_url": "https://miniapp.example/done",
        }
        body.update(overrides)
        return _api().post(_payment_url(appointment.id), body, format="json")

    def test_creates_two_stage_hold_from_snapshot_amount(
        self, customer, appointment,
    ):
        svc = MagicMock()
        svc.create_payment.return_value = _mock_create_result()
        with patch("payments.views._get_yookassa", return_value=svc):
            # A client-sent amount must be IGNORED (C7.1/C7.6) — the
            # snapshot price is the only source.
            r = self._post(appointment, customer.id, amount="1.00")
        assert r.status_code == 200, r.data
        data = r.data["data"]
        assert data["amount"] == "2000.00"
        assert data["currency"] == "RUB"
        # AMD-016: actual state at response time — pending on creation
        # (the hold lands later, via the waiting_for_capture webhook).
        assert data["capture_state"] == "pending"
        assert data["confirmation_url"] == "https://yookassa.ru/confirm/c7"
        call = svc.create_payment.call_args
        assert call.kwargs["amount"] == Decimal("2000.00")  # snapshot!
        assert call.kwargs["capture"] is False  # two-stage (D9)
        assert call.kwargs["specialist_account_id"] == "yk-subacc-c7"

    def test_repeat_returns_same_payment_no_duplicate(
        self, customer, appointment,
    ):
        svc = MagicMock()
        svc.create_payment.return_value = _mock_create_result()
        with patch("payments.views._get_yookassa", return_value=svc):
            r1 = self._post(appointment, customer.id)
            r2 = self._post(appointment, customer.id)
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.data["data"]["payment_id"] == r2.data["data"]["payment_id"]
        assert svc.create_payment.call_count == 1
        assert Payment.objects.filter(appointment=appointment).count() == 1

    def test_reuses_engine_pending_row(
        self, customer, appointment,
    ):
        """payment_required=true bookings carry a pending row WITHOUT a
        provider session — C7.1 fills that row, never adds a second."""
        pending = Payment.objects.create(
            appointment=appointment, amount=appointment.price,
            status=Payment.Status.PENDING,
        )
        svc = MagicMock()
        svc.create_payment.return_value = _mock_create_result()
        with patch("payments.views._get_yookassa", return_value=svc):
            r = self._post(appointment, customer.id)
        assert r.status_code == 200
        assert r.data["data"]["payment_id"] == str(pending.id)
        assert Payment.objects.filter(appointment=appointment).count() == 1

    def test_404_unknown_appointment(self, customer):
        r = _api().post(_payment_url(uuid.uuid4()), {
            "client_id": str(customer.id),
            "return_url": "https://miniapp.example/done",
        }, format="json")
        assert r.status_code == 404
        assert r.data["error"]["code"] == "APPOINTMENT_NOT_FOUND"

    def test_404_foreign_appointment_info_hidden(
        self, customer, other_user, appointment,
    ):
        """C7.6: another customer's appointment is indistinguishable
        from a nonexistent one."""
        appointment.client = other_user
        appointment.save(update_fields=["client"])
        r = self._post(appointment, customer.id)
        assert r.status_code == 404
        assert r.data["error"]["code"] == "APPOINTMENT_NOT_FOUND"

    def test_403_client_mismatch(self, customer, appointment):
        r = self._post(customer, uuid.uuid4())  # body names someone else
        assert r.status_code == 403
        assert r.data["error"]["code"] == "CLIENT_MISMATCH"

    def test_403_missing_bearer(self, customer, appointment):
        r = _api(bearer=None).post(_payment_url(appointment.id), {
            "client_id": str(customer.id),
            "return_url": "https://miniapp.example/done",
        }, format="json")
        assert r.status_code == 403

    @pytest.mark.parametrize("status", [
        Appointment.Status.CANCELLED,
        Appointment.Status.COMPLETED,
        Appointment.Status.NO_SHOW,
    ])
    def test_409_non_payable_status(self, customer, appointment, status):
        appointment.status = status
        appointment.save(update_fields=["status"])
        r = self._post(appointment, customer.id)
        assert r.status_code == 409
        assert r.data["error"]["code"] == "INVALID_STATUS"

    def test_409_already_paid(self, customer, appointment):
        Payment.objects.create(
            appointment=appointment, amount=appointment.price,
            status=Payment.Status.PAID, provider="yookassa",
            provider_payment_id="yk_paid_1",
        )
        r = self._post(appointment, customer.id)
        assert r.status_code == 409
        assert r.data["error"]["code"] == "INVALID_STATUS"

    def test_422_without_specialist_subaccount(
        self, customer, specialist, appointment,
    ):
        """D8 boundary preserved: no sub-account → unavailable, but the
        no-prepayment booking itself is untouched."""
        from payments.exceptions import SpecialistPayoutNotConfiguredError
        svc = MagicMock()
        svc.create_payment.side_effect = SpecialistPayoutNotConfiguredError(
            "no sub-account",
        )
        with patch("payments.views._get_yookassa", return_value=svc):
            r = self._post(appointment, customer.id)
        assert r.status_code == 422
        assert r.data["error"]["code"] == "ONLINE_PAYMENT_UNAVAILABLE"
        appointment.refresh_from_db()
        assert appointment.status == Appointment.Status.AWAITING_PAYMENT


# ---------------------------------------------------------------------------
# C7.2 — card binding + consent boundary
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCardSetup:
    def test_setup_returns_confirmation_url_with_save_flag(
        self, customer,
    ):
        svc = MagicMock()
        svc.create_card_binding.return_value = {
            "provider_payment_id": "yk_bind_1",
            "confirmation_url": "https://yookassa.ru/bind/c7",
            "status": "pending",
        }
        with patch("payments.views._get_yookassa", return_value=svc):
            r = _api().post(_card_setup_url(customer.id), {
                "consent_version": "card-consent-v1",
                "return_url": "https://miniapp.example/cards/done",
            }, format="json")
        assert r.status_code == 200, r.data
        assert r.data["data"]["confirmation_url"] == (
            "https://yookassa.ru/bind/c7"
        )
        call = svc.create_card_binding.call_args
        assert call.kwargs["user_id"] == customer.id
        assert call.kwargs["consent_version"] == "card-consent-v1"

    def test_consent_version_required(self, customer):
        """Consent boundary: binding without an explicit consent version
        is a 400, not a silent default."""
        r = _api().post(_card_setup_url(customer.id), {
            "return_url": "https://miniapp.example/cards/done",
        }, format="json")
        assert r.status_code == 400

    def test_403_foreign_user_id(self, customer):
        """C7.6: arbitrary ayla_user_id without verified linkage → 403."""
        r = _api().post(_card_setup_url(uuid.uuid4()), {
            "consent_version": "card-consent-v1",
            "return_url": "https://miniapp.example/cards/done",
        }, format="json")
        assert r.status_code == 403
        assert r.data["error"]["code"] == "CLIENT_MISMATCH"


@pytest.mark.django_db
class TestCardBindingWebhook:
    def _fire(self, customer, *, saved, event="payment.succeeded",
              status="succeeded", purpose="card_binding"):
        info = {
            "provider_payment_id": "yk_bind_1",
            "status": status,
            "paid": True,
            "expires_at": None,
            "refunded_amount": Decimal("0"),
            "metadata": {
                "purpose": purpose,
                "ayla_user_id": str(customer.id),
                "consent_version": "card-consent-v1",
                "consented_at": "2026-07-19T12:00:00+00:00",
            },
            "payment_method": {
                "id": "pm_c7_card_1",
                "saved": saved,
                "last4": "4242",
                "brand": "Visa",
            },
        }
        svc = MagicMock()
        svc.get_payment_info.return_value = info
        api = APIClient()
        api.defaults["HTTP_X_APP_TYPE"] = "client"
        with patch("payments.views._get_yookassa", return_value=svc):
            return api.post(WEBHOOK_URL, {
                "event": event, "object": {"id": "yk_bind_1"},
            }, format="json")

    def test_saved_method_persisted_with_consent(self, customer):
        r = self._fire(customer, saved=True)
        assert r.status_code == 200
        card = UserPaymentMethod.objects.get()
        assert card.user_id == customer.id
        assert card.payment_method_id == "pm_c7_card_1"
        assert card.last4 == "4242"
        assert card.brand == "Visa"
        assert card.consent_version == "card-consent-v1"
        assert card.consented_at == datetime(
            2026, 7, 19, 12, 0, tzinfo=timezone.utc,
        )
        assert card.revoked_at is None

    def test_succeeded_without_saved_flag_stores_nothing(self, customer):
        """Consent boundary: a plain payment.succeeded NEVER creates a
        saved method — only provider-confirmed saved==true does."""
        r = self._fire(customer, saved=False)
        assert r.status_code == 200
        assert UserPaymentMethod.objects.count() == 0

    def test_non_binding_payment_ignored(self, customer):
        r = self._fire(customer, saved=True, purpose="booking_payment")
        assert r.status_code == 200
        assert UserPaymentMethod.objects.count() == 0

    def test_repeat_webhook_idempotent(self, customer):
        self._fire(customer, saved=True)
        self._fire(customer, saved=True)
        assert UserPaymentMethod.objects.count() == 1


@pytest.mark.django_db
class TestCardListDelete:
    def _card(self, customer, **kw):
        defaults = dict(
            user=customer, payment_method_id=f"pm_{uuid.uuid4().hex[:8]}",
            last4="4242", brand="Visa",
            consent_version="card-consent-v1",
            consented_at=datetime.now(tz=timezone.utc),
        )
        defaults.update(kw)
        return UserPaymentMethod.objects.create(**defaults)

    def test_list_active_only(self, customer):
        active = self._card(customer)
        revoked = self._card(customer, last4="5555")
        revoked.revoke()
        r = _api().get(_cards_url(customer.id))
        assert r.status_code == 200, r.data
        cards = r.data["data"]
        assert cards == [{
            "id": str(active.id), "last4": "4242", "brand": "Visa",
        }]

    def test_list_403_foreign_user(self, customer):
        r = _api().get(_cards_url(uuid.uuid4()))
        assert r.status_code == 403

    def test_delete_revokes_and_blocks_charges(self, customer):
        card = self._card(customer)
        assert card.chargeable()
        r = _api().delete(_card_delete_url(customer.id, card.id))
        assert r.status_code == 200, r.data
        card.refresh_from_db()
        assert card.revoked_at is not None
        # C7.2 enforcement: a revoked method is not chargeable anymore.
        assert not card.chargeable()
        # …and no longer listed.
        r = _api().get(_cards_url(customer.id))
        assert r.data["data"] == []

    def test_delete_idempotent_200(self, customer):
        card = self._card(customer)
        card.revoke()
        r = _api().delete(_card_delete_url(customer.id, card.id))
        assert r.status_code == 200

    def test_delete_foreign_card_404(self, customer, other_user):
        foreign = self._card(other_user)
        r = _api().delete(_card_delete_url(customer.id, foreign.id))
        assert r.status_code == 404
        foreign.refresh_from_db()
        assert foreign.revoked_at is None  # untouched
