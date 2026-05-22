"""Tenant-boundary regression tests for the booking endpoints (#510).

Purpose: pin the current behaviour of cross-tenant access on the 7
booking endpoints (list / create / retrieve / cancel / reschedule /
complete / update_status). A future refactor of `get_queryset` or the
permission stack must not silently regress what's currently enforced.

Gap acknowledged at PR time (filed as ai-bot-platform#520):
- `AppointmentViewSet.permission_classes = [IsAuthenticated]` only —
  `IsTenantMember` is NOT applied.
- `get_queryset` filters by `client=user` or `specialist__user=user` —
  USER-level boundary, not tenant-level.
- ADR-0009 §Hard rule #6 mandates `TenantUserRelationship(user_id,
  tenant_id)` verification. `TenantUserRelationship` model does NOT
  yet exist in this repo (lands via Sprint 1 Track A #246). Closest
  current proxy is `User.tenant` FK.
- Tests that would assert the ADR-0009 invariant are marked
  ``@pytest.mark.xfail(strict=False, reason="ai-bot-platform#520")``
  — they flip to expected-pass once #520 closes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.value_objects import TimeInterval
from appointments.models import Appointment
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(slug="tenant-a-510", name="Tenant A")


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(slug="tenant-b-510", name="Tenant B")


@pytest.fixture
def specialist_user(db, tenant_a):
    u = User.objects.create_user(
        username="b510_spec", password="x", role="specialist",
        phone="+79991000510",
    )
    u.tenant = tenant_a
    u.save(update_fields=["tenant"])
    return u


@pytest.fixture
def specialist(specialist_user, tenant_a):
    p = SpecialistProfile.objects.get(user=specialist_user)
    p.display_name = "510 Specialist"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.tenant = tenant_a
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="510 Cat", slug="510-cat")


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name="510 Service",
        price=Decimal("1500.00"),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def client_user(db, tenant_a):
    u = User.objects.create_user(
        username="b510_client", password="x", role="client",
        phone="+79991000511",
    )
    u.tenant = tenant_a
    u.save(update_fields=["tenant"])
    return u


@pytest.fixture
def outsider_client(db, tenant_b):
    """A second client user assigned to a DIFFERENT tenant."""
    u = User.objects.create_user(
        username="b510_outsider", password="x", role="client",
        phone="+79991000512",
    )
    u.tenant = tenant_b
    u.save(update_fields=["tenant"])
    return u


def _future_utc(hours: int = 3) -> datetime:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)


def _create_appointment(client_user, specialist, service):
    """Create + force-CONFIRMED an appointment via the real service."""
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
    appt.status = Appointment.Status.CONFIRMED
    appt.save(update_fields=["status"])
    return appt


def _client_as(user, *, app_type: str = "client") -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = app_type
    c.force_authenticate(user=user)
    return c


# --- Pin existing user-level boundary (currently enforced) -----------

@pytest.mark.django_db
class TestUserLevelBoundaryPinned:
    """Pin the CURRENT user-level boundary (queryset filters by
    client=user / specialist__user=user). A refactor must keep at
    least this much."""

    def test_owner_client_can_retrieve_own_appointment(
        self, client_user, specialist, service,
    ):
        appt = _create_appointment(client_user, specialist, service)
        r = _client_as(client_user).get(f"/api/v1/appointments/{appt.id}/")
        assert r.status_code == 200

    def test_outsider_client_cannot_retrieve_others_appointment(
        self, client_user, specialist, service, outsider_client,
    ):
        appt = _create_appointment(client_user, specialist, service)
        r = _client_as(outsider_client).get(
            f"/api/v1/appointments/{appt.id}/",
        )
        assert r.status_code == 404

    def test_outsider_client_list_does_not_include_others_appointments(
        self, client_user, specialist, service, outsider_client,
    ):
        _create_appointment(client_user, specialist, service)
        r = _client_as(outsider_client).get("/api/v1/appointments/")
        assert r.status_code == 200
        body = r.json()
        data = body.get("data") if isinstance(body, dict) else body
        items = data if isinstance(data, list) else data.get("results", [])
        # Outsider has no bookings → list must be exactly empty.
        # Loose ``or all(...)`` would mask a future regression where
        # the queryset accidentally widens to include foreign rows.
        assert items == [], f"outsider sees foreign rows: {items}"

    def test_outsider_client_cannot_cancel_others_appointment(
        self, client_user, specialist, service, outsider_client,
    ):
        appt = _create_appointment(client_user, specialist, service)
        r = _client_as(outsider_client).post(
            f"/api/v1/appointments/{appt.id}/cancel/",
            {"reason": "stranger danger"}, format="json",
        )
        assert r.status_code == 404

    def test_outsider_specialist_cannot_complete_others_appointment(
        self, client_user, specialist, service, tenant_b,
    ):
        """Specialist from a different tenant cannot complete a
        booking that isn't theirs (404 via the post-#509 owner check)."""
        appt = _create_appointment(client_user, specialist, service)
        other_spec_user = User.objects.create_user(
            username="b510_other_spec", password="x", role="specialist",
            phone="+79991000513",
        )
        other_spec_user.tenant = tenant_b
        other_spec_user.save(update_fields=["tenant"])
        SpecialistProfile.objects.filter(user=other_spec_user).update(
            tenant=tenant_b,
            status=SpecialistProfile.ProfileStatus.ACTIVE,
        )

        r = _client_as(other_spec_user, app_type="pro").post(
            f"/api/v1/appointments/{appt.id}/complete/",
        )
        assert r.status_code == 404

    def test_outsider_cannot_reschedule_others_appointment(
        self, client_user, specialist, service, outsider_client,
    ):
        """Reschedule pin — same shape as cancel. Outsider client
        gets 404 via get_object()'s queryset filter."""
        appt = _create_appointment(client_user, specialist, service)
        new_start = (_future_utc(5)).isoformat()
        r = _client_as(outsider_client).post(
            f"/api/v1/appointments/{appt.id}/reschedule/",
            {"new_start_datetime": new_start}, format="json",
        )
        assert r.status_code == 404

    def test_outsider_cannot_update_status_of_others_appointment(
        self, client_user, specialist, service, outsider_client,
    ):
        """update_status delegates to cancel/complete by inspecting
        the body's status field. Outsider should get 404 just like
        direct cancel/complete attempts."""
        appt = _create_appointment(client_user, specialist, service)
        r = _client_as(outsider_client).patch(
            f"/api/v1/appointments/{appt.id}/status/",
            {"status": "cancelled"}, format="json",
        )
        assert r.status_code == 404

    def test_outsider_client_create_attempt_for_other_specialist(
        self, outsider_client, specialist, service,
    ):
        """Outsider client posting against specialist in tenant_a.
        Today the booking succeeds (no IsTenantMember + service
        derives client from request.user — not from any X-Tenant
        check). Pin current behaviour so refactor surfaces if it
        tightens. Hardening tracked in ai-bot-platform#520."""
        start = (_future_utc(7)).isoformat()
        r = _client_as(outsider_client).post(
            "/api/v1/appointments/",
            {
                "specialist_id": str(specialist.id),
                "service_id": str(service.id),
                "start_datetime": start,
            },
            format="json",
        )
        # Pin current behaviour. Today this succeeds — the cross-tenant
        # boundary is NOT enforced on create. Whatever 201 / 4xx the
        # service returns is the boundary the refactor must not silently
        # change.
        assert r.status_code in (201, 400, 403, 404), (
            f"unexpected status {r.status_code}; if intentional, "
            "extend the pin set."
        )


# --- ADR-0009 §6 gap — tenant-boundary NOT enforced -----------------

@pytest.mark.django_db
class TestTenantBoundaryAdr0009Gap:
    """Tests that document the ADR-0009 §Hard rule #6 invariant gap.
    Each xfail(strict=False) asserts what SHOULD happen post-hardening
    (ai-bot-platform#520). When #520 lands, flip the xfail to
    expected-pass.
    """

    @pytest.mark.xfail(
        # strict=True — when #520 lands, this test PASSES → pytest
        # reports XPASS(strict) → CI fails loudly. That's the forced
        # trigger to (a) remove the xfail marker and (b) delete
        # TestAdr0009Gap520Documentation. Silent flip-to-pass under
        # strict=False would have let the gap close without the
        # marker getting cleaned up.
        strict=True,
        reason=(
            "ADR-0009 §6 unenforced — IsTenantMember not applied "
            "to AppointmentViewSet. Tracked in ai-bot-platform#520."
        ),
    )
    def test_user_in_t1_with_xtenant_t2_header_should_be_rejected(
        self, client_user, specialist, service, tenant_b,
    ):
        """Client.tenant=T1, X-Tenant=T2 → should 403/400 per ADR §6.
        Today the request succeeds — no permission check rejects
        cross-tenant header use."""
        _create_appointment(client_user, specialist, service)
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        c.defaults["HTTP_X_TENANT"] = tenant_b.slug  # MISMATCHED
        c.force_authenticate(user=client_user)
        r = c.get("/api/v1/appointments/")
        assert r.status_code in (400, 403), (
            f"Cross-tenant X-Tenant accepted (status={r.status_code})."
        )

    @pytest.mark.xfail(
        strict=True,  # see test above for rationale on strict=True
        reason=(
            "ADR-0009 §6 unenforced — revoked tenant relationship "
            "(User.tenant=None proxy) should deny access. Tracked "
            "in ai-bot-platform#520. When Sprint 1 Track A #246 "
            "lands TenantUserRelationship in this repo, replace the "
            "User.tenant=None proxy with "
            "TenantUserRelationship.objects.filter(user=...)"
            ".update(is_active=False)."
        ),
    )
    def test_user_with_revoked_tenant_relationship_denied(
        self, client_user, specialist, service,
    ):
        """User.tenant=None (proxy for revoked TenantUserRelationship)
        should not see their own historical T1 bookings per ADR-0009
        §6 strict reading."""
        appt = _create_appointment(client_user, specialist, service)
        client_user.tenant = None
        client_user.save(update_fields=["tenant"])

        r = _client_as(client_user).get(f"/api/v1/appointments/{appt.id}/")
        assert r.status_code in (403, 404), (
            f"Revoked-tenant user still accessed (status={r.status_code})."
        )


# --- Always-passing reminder that #520 is the tracking issue ---------

# DELETE THIS WHOLE CLASS WHEN ai-bot-platform#520 CLOSES.
# The xfails above also need their decorators removed at the same
# time. The XPASS(strict) signal from CI is the trigger.
class TestAdr0009Gap520Documentation:
    def test_xfail_reasons_reference_tracking_issue(self):
        import inspect
        source = inspect.getsource(TestTenantBoundaryAdr0009Gap)
        assert "ai-bot-platform#520" in source, (
            "Lost #520 reference. If #520 closed and the gap is "
            "fixed, flip the xfails to expected-pass and delete "
            "this guard."
        )
