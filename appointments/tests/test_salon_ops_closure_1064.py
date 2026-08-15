"""DRF-1064 — a salon employee can close a visit, and the closure says who.

Three things ship together here and each has its own failure mode:

1. **Actor.** ``complete`` / ``no-show`` accepted exactly one actor — the
   assigned specialist — so the salon could not close a visit at all.
   Owner decision OD-V1 makes the salon a first-class closer.
2. **Attribution.** The closure records WHO closed it
   (``Appointment.completed_by`` + the ``completed_by`` payload field),
   because retrofitting that later means a migration plus an already
   emitted stream of events without it.
3. **Optimistic concurrency.** ``expected_version`` → 409
   ``STALE_VERSION``, checked after the row lock and before any state
   change, so a stale caller changes nothing and emits nothing.

The negative cases matter as much as the positive ones: a cross-tenant
administrator must get 404 (not 403 — that would confirm the id exists),
and a ``staff`` grant must not be silently promoted into closing rights.
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
from appointments.models import Appointment, OutboxEvent
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="salon-1064", name="Formula 1064")


@pytest.fixture
def other_salon(db):
    return Tenant.objects.create(slug="other-1064", name="Other 1064")


@pytest.fixture
def specialist_user(db, salon):
    u = User.objects.create_user(
        username="ops1064_spec", password="x", role="specialist",
        phone="+79991010641",
    )
    u.tenant = salon
    u.save(update_fields=["tenant"])
    return u


@pytest.fixture
def specialist(specialist_user, salon):
    p = SpecialistProfile.objects.get(user=specialist_user)
    p.display_name = "1064 Master"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.tenant = salon
    p.save()
    return p


@pytest.fixture
def other_specialist(db, salon):
    u = User.objects.create_user(
        username="ops1064_spec2", password="x", role="specialist",
        phone="+79991010642",
    )
    u.tenant = salon
    u.save(update_fields=["tenant"])
    p = SpecialistProfile.objects.get(user=u)
    p.display_name = "1064 Colleague"
    p.tenant = salon
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="1064 Cat", slug="1064-cat")


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name="1064 Service",
        price=Decimal("1000.00"),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="ops1064_client", password="x", role="client",
        phone="+79991010643",
    )


def _grant(user, tenant, role):
    return TenantUserRelationship.objects.create(
        user=user, tenant=tenant, role=role, is_active=True,
    )


@pytest.fixture
def salon_admin(db, salon):
    """A front-desk administrator: an ordinary account plus an admin grant.

    Deliberately NOT ``role="specialist"`` — the whole point is that the
    capacity comes from the tenant grant, not from the account's global
    role. ``provision_salon_admin`` (DRF-1062) creates exactly this shape.
    """
    u = User.objects.create_user(
        username="ops1064_admin", password="x", role="admin",
        phone="+79991010644",
    )
    _grant(u, salon, TenantUserRelationship.Role.ADMIN)
    return u


@pytest.fixture
def foreign_admin(db, salon, other_salon):
    u = User.objects.create_user(
        username="ops1064_admin2", password="x", role="admin",
        phone="+79991010645",
    )
    _grant(u, other_salon, TenantUserRelationship.Role.ADMIN)
    # Also a plain customer of the salon under test — so the request
    # passes IsTenantMember and the 404 below is produced by the actor
    # resolver, not by the membership guard.
    _grant(u, salon, TenantUserRelationship.Role.CUSTOMER)
    return u


@pytest.fixture
def salon_staff(db, salon):
    u = User.objects.create_user(
        username="ops1064_staff", password="x", role="client",
        phone="+79991010646",
    )
    _grant(u, salon, TenantUserRelationship.Role.STAFF)
    return u


@pytest.fixture
def other_salon_booking(db, other_salon, category):
    """A confirmed booking that belongs to a DIFFERENT salon."""
    u = User.objects.create_user(
        username="ops1064_spec_b", password="x", role="specialist",
        phone="+79991010647",
    )
    u.tenant = other_salon
    u.save(update_fields=["tenant"])
    p = SpecialistProfile.objects.get(user=u)
    p.display_name = "1064 Foreign Master"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.tenant = other_salon
    p.save()
    svc = Service.objects.create(
        specialist=p, category=category, name="1064 Foreign Service",
        price=Decimal("900.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )
    other_client = User.objects.create_user(
        username="ops1064_client_b", password="x", role="client",
        phone="+79991010648",
    )
    return _confirmed(other_client, p, svc)


def _future_utc(hours: int = 3) -> datetime:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)


def _confirmed(client_user, specialist, service) -> Appointment:
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
    # Creation-time events are noise for these assertions.
    OutboxEvent.objects.all().delete()
    return appt


def _api(user, *, tenant_slug: str | None = None) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "pro"
    if tenant_slug:
        c.defaults["HTTP_X_TENANT"] = tenant_slug
    c.force_authenticate(user=user)
    return c


def _event(topic):
    return OutboxEvent.objects.filter(topic=topic).first()


# ---------------------------------------------------------------------------
# The salon can close a visit — the point of the task
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestSalonClosesVisit:

    def test_admin_completes_booking_of_own_tenant(
        self, client_user, specialist, service, salon, salon_admin,
    ):
        appt = _confirmed(client_user, specialist, service)

        resp = _api(salon_admin, tenant_slug=salon.slug).post(
            f"/api/v1/appointments/{appt.id}/complete/", {}, format="json",
        )

        assert resp.status_code == 200, resp.data
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.COMPLETED
        assert appt.completed_at is not None
        assert appt.completed_by == "salon"

    def test_completed_event_carries_the_closer_and_stays_at_version_1(
        self, client_user, specialist, service, salon, salon_admin,
    ):
        appt = _confirmed(client_user, specialist, service)

        _api(salon_admin, tenant_slug=salon.slug).post(
            f"/api/v1/appointments/{appt.id}/complete/", {}, format="json",
        )

        evt = _event(OutboxEvent.Topic.BOOKING_COMPLETED)
        assert evt is not None
        payload = evt.payload
        # New OPTIONAL data field → non-breaking per event-contract §4.1,
        # so the wire version must NOT move.
        assert payload["event_version"] == 1
        assert payload["data"]["completed_by"] == "salon"
        assert payload["data"]["completed_at"]
        # Envelope actor stays inside the pinned three-value enum; salon
        # and specialist both map to "admin" and are told apart by the
        # payload field above (event-contract §2.2).
        assert payload["actor"] == "admin"
        # §2.2: for an admin-actor event user_id is the AFFECTED user.
        # It used to be the operator, which pointed the bot's post-visit
        # review skill at the wrong person.
        assert payload["user_id"] == str(appt.client_id)

    def test_admin_marks_no_show_and_cancellation_says_admin(
        self, client_user, specialist, service, salon, salon_admin,
    ):
        appt = _confirmed(client_user, specialist, service)

        resp = _api(salon_admin, tenant_slug=salon.slug).post(
            f"/api/v1/appointments/{appt.id}/no-show/", {}, format="json",
        )

        assert resp.status_code == 200, resp.data
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.NO_SHOW
        assert appt.no_show_marked_by == "salon"

        internal = _event(OutboxEvent.Topic.BOOKING_NO_SHOW)
        assert internal.payload["data"]["no_show_marked_by"] == "salon"

        # Cross-service representation: §3.2 declares `admin` in the
        # cancelled_by enum and nothing in Ayla had ever produced it.
        mirror = _event(OutboxEvent.Topic.BOOKING_CANCELLED)
        assert mirror.payload["data"]["cancelled_by"] == "admin"
        assert mirror.payload["data"]["reason_code"] == "user_no_show"


# ---------------------------------------------------------------------------
# The master's existing path is untouched
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestSpecialistPathUnchanged:

    def test_specialist_completes_own_booking(
        self, client_user, specialist, specialist_user, service,
    ):
        appt = _confirmed(client_user, specialist, service)

        resp = _api(specialist_user).post(
            f"/api/v1/appointments/{appt.id}/complete/", {}, format="json",
        )

        assert resp.status_code == 200, resp.data
        appt.refresh_from_db()
        assert appt.completed_by == "specialist"
        evt = _event(OutboxEvent.Topic.BOOKING_COMPLETED)
        assert evt.payload["data"]["completed_by"] == "specialist"
        assert evt.payload["actor"] == "admin"

    def test_specialist_no_show_still_reads_as_master(
        self, client_user, specialist, specialist_user, service,
    ):
        appt = _confirmed(client_user, specialist, service)

        _api(specialist_user).post(
            f"/api/v1/appointments/{appt.id}/no-show/", {}, format="json",
        )

        appt.refresh_from_db()
        assert appt.no_show_marked_by == "specialist"
        mirror = _event(OutboxEvent.Topic.BOOKING_CANCELLED)
        assert mirror.payload["data"]["cancelled_by"] == "master"

    def test_specialist_cannot_close_a_colleagues_booking(
        self, client_user, specialist, service, other_specialist,
    ):
        appt = _confirmed(client_user, specialist, service)

        resp = _api(other_specialist.user).post(
            f"/api/v1/appointments/{appt.id}/complete/", {}, format="json",
        )

        # 404, not 403 — a specialist must not learn that the id exists.
        assert resp.status_code == 404
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED


# ---------------------------------------------------------------------------
# Who is NOT a closer
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestClosingRightsAreNarrow:

    def test_client_is_rejected(
        self, client_user, specialist, service,
    ):
        appt = _confirmed(client_user, specialist, service)
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        c.force_authenticate(user=client_user)

        resp = c.post(
            f"/api/v1/appointments/{appt.id}/complete/", {}, format="json",
        )

        assert resp.status_code == 403
        assert resp.data["error"]["code"] == "FORBIDDEN"

    def test_no_standing_in_the_addressed_salon_is_403(
        self, client_user, specialist, service, salon, foreign_admin,
    ):
        """403, and it leaks nothing.

        This caller administers a different salon and is a mere customer
        of this one. The refusal happens at the role gate, before the row
        is ever fetched, so the answer is about the caller — not about
        whether the id exists. The 404 rule (next test) applies one level
        down: a caller WITH standing who reaches for a row outside it.
        """
        appt = _confirmed(client_user, specialist, service)

        resp = _api(foreign_admin, tenant_slug=salon.slug).post(
            f"/api/v1/appointments/{appt.id}/complete/", {}, format="json",
        )

        assert resp.status_code == 403
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED
        assert appt.completed_by == ""

    def test_admin_cannot_reach_a_booking_of_another_salon(
        self, salon, salon_admin, other_salon_booking,
    ):
        """The tenant boundary at row level — 404, never 403.

        ``complete`` deliberately bypasses ``get_queryset`` (it needs
        ``select_for_update`` on the raw manager), so nothing else stands
        between a valid admin and any appointment id in the system. This
        is the test that pins the guard that does.
        """
        resp = _api(salon_admin, tenant_slug=salon.slug).post(
            f"/api/v1/appointments/{other_salon_booking.id}/complete/",
            {}, format="json",
        )

        assert resp.status_code == 404
        other_salon_booking.refresh_from_db()
        assert other_salon_booking.status == Appointment.Status.CONFIRMED
        assert other_salon_booking.completed_by == ""

    def test_admin_without_addressed_tenant_is_rejected(
        self, client_user, specialist, service, salon_admin,
    ):
        appt = _confirmed(client_user, specialist, service)

        # No X-Tenant: nothing to authorise against. Fail closed — the
        # tenant is never taken from the body or inferred from the grant.
        resp = _api(salon_admin).post(
            f"/api/v1/appointments/{appt.id}/complete/", {}, format="json",
        )

        assert resp.status_code == 403
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED

    def test_staff_grant_is_not_a_closing_right(
        self, client_user, specialist, service, salon, salon_staff,
    ):
        appt = _confirmed(client_user, specialist, service)

        resp = _api(salon_staff, tenant_slug=salon.slug).post(
            f"/api/v1/appointments/{appt.id}/complete/", {}, format="json",
        )

        # Only role=admin was granted closing rights. Widening to `staff`
        # is an owner decision, not an implementation detail.
        assert resp.status_code == 403
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED


# ---------------------------------------------------------------------------
# Optimistic concurrency
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestExpectedVersion:

    def test_matching_version_completes(
        self, client_user, specialist, service, salon, salon_admin,
    ):
        appt = _confirmed(client_user, specialist, service)

        resp = _api(salon_admin, tenant_slug=salon.slug).post(
            f"/api/v1/appointments/{appt.id}/complete/",
            {"expected_version": appt.version}, format="json",
        )

        assert resp.status_code == 200, resp.data
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.COMPLETED

    def test_stale_version_changes_nothing_and_emits_nothing(
        self, client_user, specialist, service, salon, salon_admin,
    ):
        appt = _confirmed(client_user, specialist, service)

        resp = _api(salon_admin, tenant_slug=salon.slug).post(
            f"/api/v1/appointments/{appt.id}/complete/",
            {"expected_version": appt.version + 7}, format="json",
        )

        assert resp.status_code == 409
        assert resp.data["error"]["code"] == "STALE_VERSION"
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED
        assert appt.completed_at is None
        assert appt.completed_by == ""
        assert not OutboxEvent.objects.exists()

    def test_stale_version_on_no_show_changes_nothing(
        self, client_user, specialist, service, salon, salon_admin,
    ):
        appt = _confirmed(client_user, specialist, service)

        resp = _api(salon_admin, tenant_slug=salon.slug).post(
            f"/api/v1/appointments/{appt.id}/no-show/",
            {"expected_version": appt.version + 7}, format="json",
        )

        assert resp.status_code == 409
        assert resp.data["error"]["code"] == "STALE_VERSION"
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED
        assert not OutboxEvent.objects.exists()

    def test_omitting_the_version_still_works(
        self, client_user, specialist, specialist_user, service,
    ):
        """Backward compatibility: existing mobile builds and the
        ``PATCH /status/`` alias never send ``expected_version``, and
        this task does not turn a working call into a 400."""
        appt = _confirmed(client_user, specialist, service)

        resp = _api(specialist_user).post(
            f"/api/v1/appointments/{appt.id}/complete/", {}, format="json",
        )

        assert resp.status_code == 200, resp.data

    def test_completion_does_not_bump_version(
        self, client_user, specialist, service, salon, salon_admin,
    ):
        """Pinning a found architectural fact, not a wish.

        ``Appointment.version`` counts reschedules — it is bumped in
        exactly one place (``RescheduleBookingService``) and every bump
        writes an ``AppointmentRevision`` under
        ``UNIQUE(appointment, version)``. Incrementing it on completion
        would punch a hole in that append-only history, and no canonical
        source asks for it. If the owner ever decides completion must
        move the version, this test is the thing that should fail first.
        """
        appt = _confirmed(client_user, specialist, service)
        before = appt.version

        _api(salon_admin, tenant_slug=salon.slug).post(
            f"/api/v1/appointments/{appt.id}/complete/",
            {"expected_version": before}, format="json",
        )

        appt.refresh_from_db()
        assert appt.version == before

    def test_replay_after_success_is_invalid_status_not_stale_version(
        self, client_user, specialist, service, salon, salon_admin,
    ):
        """The consequence of the fact above, spelled out for clients.

        Because the version does not move, a replayed close passes the
        version check and fails on state. Two different codes, both
        meaning "re-read"; only ``STALE_VERSION`` means "someone moved
        it under you".
        """
        appt = _confirmed(client_user, specialist, service)
        api = _api(salon_admin, tenant_slug=salon.slug)
        url = f"/api/v1/appointments/{appt.id}/complete/"

        first = api.post(url, {"expected_version": appt.version}, format="json")
        assert first.status_code == 200

        second = api.post(url, {"expected_version": appt.version}, format="json")
        assert second.status_code == 422
        assert second.data["error"]["code"] == "INVALID_STATUS"


# ---------------------------------------------------------------------------
# Domain-level guards
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestAttributionVocabulary:

    def test_unknown_actor_is_refused_by_the_model(
        self, client_user, specialist, service,
    ):
        appt = _confirmed(client_user, specialist, service)

        with pytest.raises(ValueError):
            appt.complete(completed_by="receptionist")

        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED

    def test_unattributed_closure_is_allowed_but_marked_as_such(
        self, client_user, specialist, service,
    ):
        """Empty means "we do not know who", which is the truth about
        every row closed before this field existed. It must not be
        confused with a real actor value."""
        appt = _confirmed(client_user, specialist, service)

        appt.complete()

        appt.refresh_from_db()
        assert appt.status == Appointment.Status.COMPLETED
        assert appt.completed_by == ""
