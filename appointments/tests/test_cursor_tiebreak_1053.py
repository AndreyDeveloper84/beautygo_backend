"""DRF-1053 — cursor pagination must not drop rows that share a timestamp.

``_CursorPageMixin`` sorts with a ``(start_datetime, id)`` tie-break but
filters the next page on ``start_datetime`` alone. When two or more
appointments land on the exact same ``start_datetime`` and the page
boundary falls inside that group, the rows after the boundary are
filtered out by ``start_datetime__gt`` / ``__lt`` and never returned.

The defect is silent: the caller sees a shorter list, not an error.

Test shape (per the contour rule "a negative assertion needs a positive
guard on the same data"):

1. Build N appointments, deliberately forcing duplicate timestamps.
2. **Positive guard** — assert the fixture really contains ties. Without
   it the "nothing was lost" assertion would pass on any data set where
   no boundary ever falls inside a tie group, i.e. it would prove
   nothing.
3. Page through to exhaustion and assert exactly N unique ids came back
   — no losses, no repeats.

No literal dates anywhere: every timestamp is an offset from ``now()``.
"""
from __future__ import annotations

from collections import Counter
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
from users.models import SpecialistProfile, TenantUserRelationship, User


VALID_TOKEN = "test-ayla-internal-token-tiebreak"
EXTERNAL_USER_ID = "bot:tiebreak"
LIST_URL = "/api/v1/internal/me/bookings/"

# Two tie groups of three. With limit=2 the page boundary lands inside
# BOTH groups, so a start_datetime-only cursor drops one row per group.
TIE_GROUP_SIZE = 3
TOTAL = TIE_GROUP_SIZE * 2
PAGE_LIMIT = 2


# ---------------------------------------------------------------------------
# Fixtures / helpers (self-contained — no cross-test-module imports)
# ---------------------------------------------------------------------------


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79997100001", is_proxy=True,
    )


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="tb-a", name="Tiebreak Tenant")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="TbCat", slug="tb-cat")


@pytest.fixture
def specialist(db, tenant):
    user = User.objects.create_user(
        username="tb_spec_1", password="x", role="specialist",
        phone="+79993100001",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.display_name = "Tiebreak Spec"
    profile.tenant = tenant
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.timezone = "Europe/Moscow"
    profile.save()
    return profile


@pytest.fixture
def service(db, specialist, category):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name="Tiebreak Service",
        price=Decimal("1000.00"),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
    )


def _api() -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
    return c


def _now() -> datetime:
    return datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)


def _book(customer, specialist, service, start_at: datetime) -> Appointment:
    """Create one appointment through the booking service core.

    Distinct, non-overlapping slots keep the double-booking guard happy;
    the ties are forced afterwards with a queryset ``update()`` which
    bypasses ``save()``/``clean()``.
    """
    dto = CreateBookingDTO(
        client_id=customer.id,
        specialist_id=specialist.id,
        service_id=service.id,
        start_at=start_at,
        idempotency_key=str(uuid4()),
    )
    appt, _ = CreateBookingService()._execute_atomic(
        dto, specialist, service,
        target_interval=TimeInterval(
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
        ),
    )
    TenantUserRelationship.objects.get_or_create(
        user=customer, tenant=specialist.tenant,
        defaults={
            "role": TenantUserRelationship.Role.CUSTOMER,
            "is_active": True,
        },
    )
    return appt


def _build_with_ties(customer, specialist, service, *, past: bool):
    """N appointments in two groups, each group sharing one timestamp.

    ``past=False`` -> all in the future (section=upcoming).
    ``past=True``  -> all backdated (section=history).
    Returns the list of appointment ids.
    """
    appts = [
        _book(customer, specialist, service,
              _now() + timedelta(hours=3 + 2 * i))
        for i in range(TOTAL)
    ]

    if past:
        group_starts = [
            _now() - timedelta(days=3),
            _now() - timedelta(days=5),
        ]
    else:
        group_starts = [
            _now() + timedelta(days=3),
            _now() + timedelta(days=5),
        ]

    for group_index, start in enumerate(group_starts):
        chunk = appts[
            group_index * TIE_GROUP_SIZE:(group_index + 1) * TIE_GROUP_SIZE
        ]
        Appointment.objects.filter(id__in=[a.id for a in chunk]).update(
            start_datetime=start,
            end_datetime=start + timedelta(hours=1),
        )

    return [a.id for a in appts]


def _assert_ties_present(appointment_ids) -> None:
    """Positive guard for the negative assertion below.

    'Nothing was lost' is only meaningful if the data actually contains
    a page boundary that can lose something. Assert the ties exist.
    """
    stamps = list(
        Appointment.objects
        .filter(id__in=appointment_ids)
        .values_list("start_datetime", flat=True)
    )
    counts = Counter(stamps)
    duplicated = {ts: n for ts, n in counts.items() if n > 1}
    assert duplicated, (
        "fixture built no duplicate start_datetime values — this test "
        "would be green on any implementation and prove nothing"
    )
    assert max(duplicated.values()) > PAGE_LIMIT, (
        "no tie group is larger than the page size, so no page boundary "
        f"falls inside a tie: groups={duplicated}"
    )


def _drain(section: str) -> list[str]:
    """Page through the endpoint to exhaustion; return ids in order."""
    collected: list[str] = []
    cursor = None
    # Hard stop well above the number of pages a correct implementation
    # needs — protects against a cursor that fails to advance.
    for _ in range(TOTAL + 5):
        params = {"section": section, "limit": str(PAGE_LIMIT)}
        if cursor is not None:
            params["cursor"] = cursor
        r = _api().get(LIST_URL, params)
        assert r.status_code == 200, r.content
        body = r.json()["data"]
        collected.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("cursor never terminated")
    return collected


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCursorTieBreak:
    """DRF-1053 — paging over equal timestamps must be lossless."""

    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_upcoming_paging_loses_no_row_on_a_timestamp_tie(
        self, customer, specialist, service,
    ):
        ids = _build_with_ties(customer, specialist, service, past=False)
        _assert_ties_present(ids)

        collected = _drain("upcoming")

        assert len(collected) == len(set(collected)), (
            f"duplicate ids across pages: {collected}"
        )
        assert set(collected) == {str(i) for i in ids}, (
            f"lost {len(ids) - len(set(collected))} of {len(ids)} bookings "
            f"paging with limit={PAGE_LIMIT}"
        )

    def test_history_paging_loses_no_row_on_a_timestamp_tie(
        self, customer, specialist, service,
    ):
        ids = _build_with_ties(customer, specialist, service, past=True)
        _assert_ties_present(ids)

        collected = _drain("history")

        assert len(collected) == len(set(collected)), (
            f"duplicate ids across pages: {collected}"
        )
        assert set(collected) == {str(i) for i in ids}, (
            f"lost {len(ids) - len(set(collected))} of {len(ids)} bookings "
            f"paging with limit={PAGE_LIMIT}"
        )

    def test_ordering_within_a_tie_group_is_by_id(
        self, customer, specialist, service,
    ):
        """The tie-break itself must be observable and stable.

        Guards against a 'fix' that merely widens the filter without
        matching the sort key — the two must agree or paging desyncs.
        """
        ids = _build_with_ties(customer, specialist, service, past=False)
        _assert_ties_present(ids)

        # One shot, no paging: the full list in sort order.
        r = _api().get(LIST_URL, {"section": "upcoming", "limit": str(TOTAL)})
        assert r.status_code == 200, r.content
        items = r.json()["data"]["items"]
        assert len(items) == TOTAL

        expected = [
            str(pk) for pk in (
                Appointment.objects
                .filter(id__in=ids)
                .order_by("start_datetime", "id")
                .values_list("id", flat=True)
            )
        ]
        assert [it["id"] for it in items] == expected


@pytest.mark.django_db
class TestCursorFormatCompatibility:
    """The cursor gained an id half — old ones must still be honoured.

    A bot can hold a cursor issued by the previous build across a
    deploy. Rejecting it would turn a fixed bug into a visible 400 in
    the middle of someone's scroll, so a timestamp-only cursor keeps
    the old (lossy) comparison instead.
    """

    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_legacy_timestamp_only_cursor_still_paginates(
        self, customer, specialist, service,
    ):
        ids = _build_with_ties(customer, specialist, service, past=False)
        first = Appointment.objects.filter(id__in=ids).order_by(
            "start_datetime", "id",
        ).first()

        # Exactly what the previous build emitted: no id half.
        legacy = first.start_datetime.isoformat()
        r = _api().get(
            LIST_URL,
            {"section": "upcoming", "limit": str(PAGE_LIMIT),
             "cursor": legacy},
        )
        assert r.status_code == 200, r.content
        # Old semantics: strictly-later timestamps only.
        returned = {it["id"] for it in r.json()["data"]["items"]}
        assert returned
        assert str(first.id) not in returned

    def test_cursor_with_a_broken_id_half_is_rejected(
        self, customer, specialist, service,
    ):
        ids = _build_with_ties(customer, specialist, service, past=False)
        first = Appointment.objects.filter(id__in=ids).first()
        bad = f"{first.start_datetime.isoformat()}|not-a-uuid"
        r = _api().get(
            LIST_URL, {"section": "upcoming", "cursor": bad},
        )
        assert r.status_code == 400

    def test_next_cursor_round_trips_through_the_client(
        self, customer, specialist, service,
    ):
        """The separator must survive URL encoding both ways."""
        _build_with_ties(customer, specialist, service, past=False)
        r = _api().get(
            LIST_URL, {"section": "upcoming", "limit": str(PAGE_LIMIT)},
        )
        cursor = r.json()["data"]["next_cursor"]
        assert cursor is not None and "|" in cursor

        r2 = _api().get(
            LIST_URL,
            {"section": "upcoming", "limit": str(PAGE_LIMIT),
             "cursor": cursor},
        )
        assert r2.status_code == 200, r2.content
        assert r2.json()["data"]["items"]
