"""Tests for POST /api/v1/payments/internal/{id}/retry/ — task #85.

Service-to-service equivalent of POST /api/v1/payments/{id}/retry/, used
by the bot's W2 payment_failed skill. Auth via Authorization: Bearer +
X-External-User-ID. Defense-in-depth body.client_id cross-check.

The test file's §H.3 adversarial focus:

- The bearer token alone must not authorise impersonation. We pin the
  body.client_id cross-check by sending a forged header that *resolves*
  to a real user but naming a different victim in the body — 403.
- Missing / wrong bearer → 403 (PermissionDenied, not 401, since this
  view doesn't pair the permission with IsAuthenticated).
- Misconfigured deployment (empty AYLA_INTERNAL_API_TOKEN) → fail
  closed (403 even with a "correct" empty bearer).
- Malformed X-External-User-ID → 403, never a server error.
- Cross-tenant: a payment owned by a *different* Ayla user is invisible
  to the resolved actor, returns 404 (same as the mobile path's
  "wrong client" branch).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.value_objects import TimeInterval
from appointments.models import Appointment
from payments.models import Payment
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User


VALID_TOKEN = "test-ayla-internal-token-deadbeef"
WRONG_TOKEN = "test-ayla-internal-token-wrong"


# ---------------------------------------------------------------------------
# Fixtures — mirror the mobile-path test file with a single deviation:
# the "external user" is the appointment's client, identified via username
# (resolve_external_user uses username as the lookup key).
# ---------------------------------------------------------------------------


@pytest.fixture
def external_user_id() -> str:
    # Format <source>:<id> validated by users.services._EXTERNAL_USER_ID_RE.
    return "bot:42"


@pytest.fixture
def client_user(db, external_user_id):
    # resolve_external_user(external_user_id) looks up by username, so
    # the fixture's username MUST equal the X-External-User-ID we send.
    return User.objects.create_user(
        username=external_user_id, password="x", role="client",
        phone="+79991700100", is_proxy=True,
    )


@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username="internal_retry_spec", password="x", role="specialist",
        phone="+79991700101",
    )


@pytest.fixture
def specialist(specialist_user):
    p = SpecialistProfile.objects.get(user=specialist_user)
    p.display_name = "Internal Retry Spec"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(
        name="InternalRetry", slug="internal-retry-cat",
    )


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name="Internal Retry Service",
        price=Decimal("1500.00"),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
    )


def _future_utc(hours: int = 3) -> datetime:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)


@pytest.fixture
def appointment(client_user, specialist, service):
    from uuid import uuid4
    dto = CreateBookingDTO(
        client_id=client_user.id,
        specialist_id=specialist.id,
        service_id=service.id,
        start_at=_future_utc(3),
        idempotency_key=str(uuid4()),
    )
    appt, _ = CreateBookingService()._execute_atomic(
        dto, specialist, service,
        target_interval=TimeInterval(
            start_at=dto.start_at,
            end_at=dto.start_at + timedelta(hours=1),
        ),
    )
    return appt


@pytest.fixture
def failed_payment(appointment):
    return Payment.objects.create(
        appointment=appointment,
        amount=appointment.price,
        status=Payment.Status.FAILED,
        specialist_income=Decimal("1380.00"),
        platform_fee=Decimal("120.00"),
        provider="yookassa",
        provider_payment_id="failed-yk-internal-001",
        provider_client_secret="https://yookassa/old/expired",
    )


def _api(
    *, bearer: str | None = VALID_TOKEN,
    external_user_id: str | None = "bot:42",
) -> APIClient:
    c = APIClient()
    # No X-App-Type — internal endpoints are excluded from
    # AppTypeMiddleware (see users/middleware.py EXCLUDED_PATH_PREFIXES).
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if external_user_id is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


def _url(payment_id) -> str:
    return f"/api/v1/payments/internal/{payment_id}/retry/"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestInternalPaymentRetryHappyPath:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_bot_can_retry_failed_payment(
        self, client_user, failed_payment, appointment,
    ):
        mock_result = {
            'specialist_income': Decimal("1380.00"),
            'platform_fee': Decimal("120.00"),
            'provider_payment_id': "yk-internal-retry-001",
            'confirmation_url': "https://yookassa/new/internal-retry",
        }
        with patch("payments.views._get_yookassa") as mock_get_yk:
            mock_get_yk.return_value.create_payment.return_value = mock_result
            r = _api().post(
                _url(failed_payment.id),
                {
                    "client_id": str(client_user.id),
                    "return_url": "https://ayla.app/success",
                },
                format="json",
            )
        assert r.status_code == 201, r.data
        body = r.data.get("data") or r.data
        assert body["confirmation_url"] == "https://yookassa/new/internal-retry"
        assert body["amount"] == 1500.0
        # New PENDING Payment row exists.
        new_payment = Payment.objects.get(
            appointment=appointment,
            provider_payment_id="yk-internal-retry-001",
        )
        assert new_payment.status == Payment.Status.PENDING


# ---------------------------------------------------------------------------
# Auth boundary — Bearer token
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestInternalPaymentRetryBearerAuth:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_missing_authorization_header_denies(
        self, client_user, failed_payment,
    ):
        r = _api(bearer=None).post(
            _url(failed_payment.id),
            {"client_id": str(client_user.id),
             "return_url": "https://ayla.app/success"},
            format="json",
        )
        assert r.status_code == 403

    def test_wrong_bearer_token_denies(
        self, client_user, failed_payment,
    ):
        r = _api(bearer=WRONG_TOKEN).post(
            _url(failed_payment.id),
            {"client_id": str(client_user.id),
             "return_url": "https://ayla.app/success"},
            format="json",
        )
        assert r.status_code == 403

    def test_non_bearer_scheme_denies(
        self, client_user, failed_payment,
    ):
        c = APIClient()
        c.defaults["HTTP_AUTHORIZATION"] = f"Basic {VALID_TOKEN}"
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = "bot:42"
        r = c.post(
            _url(failed_payment.id),
            {"client_id": str(client_user.id),
             "return_url": "https://ayla.app/success"},
            format="json",
        )
        assert r.status_code == 403


@pytest.mark.django_db
class TestInternalPaymentRetryFailClosed:
    """Pin the misconfigured-deployment defense.

    When ``AYLA_INTERNAL_API_TOKEN`` is empty (factory default in
    ``settings.base``) the permission MUST refuse every request — even
    one whose Authorization header is empty/missing or coincidentally
    matches the empty string. We don't accept *any* bearer on a
    deployment that hasn't been provisioned with the secret.
    """

    @pytest.fixture(autouse=True)
    def _empty_token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = ""

    def test_empty_token_denies_even_with_empty_bearer(
        self, client_user, failed_payment,
    ):
        c = APIClient()
        c.defaults["HTTP_AUTHORIZATION"] = "Bearer "
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = "bot:42"
        r = c.post(
            _url(failed_payment.id),
            {"client_id": str(client_user.id),
             "return_url": "https://ayla.app/success"},
            format="json",
        )
        assert r.status_code == 403

    def test_empty_token_denies_with_any_bearer(
        self, client_user, failed_payment,
    ):
        r = _api(bearer="any-value-the-attacker-tries").post(
            _url(failed_payment.id),
            {"client_id": str(client_user.id),
             "return_url": "https://ayla.app/success"},
            format="json",
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Auth boundary — X-External-User-ID
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestInternalPaymentRetryExternalUserHeader:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_missing_external_user_id_denies(
        self, client_user, failed_payment,
    ):
        r = _api(external_user_id=None).post(
            _url(failed_payment.id),
            {"client_id": str(client_user.id),
             "return_url": "https://ayla.app/success"},
            format="json",
        )
        assert r.status_code == 403

    def test_malformed_external_user_id_denies(
        self, client_user, failed_payment,
    ):
        # Missing the `<source>:` prefix → InvalidExternalUserIDError →
        # permission returns False, not a 500.
        r = _api(external_user_id="not-a-valid-format").post(
            _url(failed_payment.id),
            {"client_id": str(client_user.id),
             "return_url": "https://ayla.app/success"},
            format="json",
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Defense-in-depth — body.client_id cross-check
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestInternalPaymentRetryClientIdDefense:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_body_client_id_mismatch_returns_403(
        self, client_user, failed_payment,
    ):
        """Token + correct X-External-User-ID, but the body names a
        *different* Ayla user — the request must be refused. This is
        the second factor that limits blast radius of a leaked token."""
        # Some other registered Ayla user — has nothing to do with the
        # appointment, but is a syntactically valid UUID.
        victim = User.objects.create_user(
            username="some_other_user", password="x", role="client",
            phone="+79991700199",
        )
        r = _api().post(
            _url(failed_payment.id),
            {
                "client_id": str(victim.id),
                "return_url": "https://ayla.app/success",
            },
            format="json",
        )
        assert r.status_code == 403
        assert r.data["error"]["code"] == "CLIENT_MISMATCH"

    def test_body_missing_client_id_validation_error(
        self, client_user, failed_payment,
    ):
        r = _api().post(
            _url(failed_payment.id),
            {"return_url": "https://ayla.app/success"},
            format="json",
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Resource visibility — same contract as mobile path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestInternalPaymentRetryVisibility:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_other_users_payment_returns_404(
        self, client_user, failed_payment,
    ):
        """A payment owned by a different Ayla user is invisible to the
        resolved actor — same NOT_FOUND envelope as the mobile path,
        no info disclosure (`appointment__client=request.user` filter
        in PaymentRetryService.execute)."""
        other_proxy = User.objects.create_user(
            username="bot:other-actor", password="x", role="client",
            phone="+79991700102", is_proxy=True,
        )
        # New client_user with username matching X-External-User-ID
        # other-actor would resolve to this user. But our request uses
        # external_user_id="bot:42" → resolves to client_user. The
        # payment we look up belongs to client_user already — so flip
        # the test: use a payment owned by other_proxy and try to fetch
        # it as bot:42. To do that we need a payment for other_proxy.
        # Simulate by reassigning the appointment's client.
        appt = failed_payment.appointment
        appt.client = other_proxy
        appt.save(update_fields=["client"])

        r = _api().post(
            _url(failed_payment.id),
            {
                "client_id": str(client_user.id),
                "return_url": "https://ayla.app/success",
            },
            format="json",
        )
        assert r.status_code == 404

    def test_409_when_payment_status_not_failed(
        self, client_user, failed_payment,
    ):
        failed_payment.status = Payment.Status.PAID
        failed_payment.save(update_fields=["status"])
        r = _api().post(
            _url(failed_payment.id),
            {"client_id": str(client_user.id),
             "return_url": "https://ayla.app/success"},
            format="json",
        )
        assert r.status_code == 409
        assert r.data["error"]["code"] == "INVALID_STATUS"

    def test_409_when_appointment_cancelled(
        self, client_user, failed_payment, appointment,
    ):
        appointment.status = Appointment.Status.CANCELLED
        appointment.save(update_fields=["status"])
        r = _api().post(
            _url(failed_payment.id),
            {"client_id": str(client_user.id),
             "return_url": "https://ayla.app/success"},
            format="json",
        )
        assert r.status_code == 409
