"""DRF-1128 — offset-paginated lists must have a TOTAL sort order.

Sibling class of DRF-1053 (#266). That one was cursor pagination
comparing half of its sort key; this one is ``PageNumberPagination``
(LIMIT/OFFSET) over an order that is not total to begin with.

Three list endpoints sort on timestamps alone::

    GET /api/v1/specialists/{id}/reviews/   -created_at | -rating,-created_at
    GET /api/v1/notifications/              -created_at
    GET /api/v1/ai/conversations/           -last_message_at,-created_at

When several rows carry the same value for every key in the ORDER BY,
SQL leaves their relative order undefined, and every page of an offset
paginator is a separate execution of that query. Postgres plans each one
against its own ``LIMIT + OFFSET`` bound: a small bound gets a top-N
heapsort, a large one a full sort, and the two disagree about which tied
rows belong where. A row that sat on page 7 for one request sits on page
3 for the next — so a client paging to the end collects it twice and
never sees some other row at all.

Ties are not exotic here. ``created_at`` is ``auto_now_add``, so every
row written inside one transaction — a notification fan-out, a review
backfill, a bulk import — carries the identical microsecond.

The defect is silent. No error, no 500: the caller just sees one entry
fewer, or the same entry twice.

Test shape (contour rule "a negative assertion needs a positive guard on
the same data"):

1. Build N rows with a deliberate tie group.
2. **Positive guards** — the fixture really contains a tie, and the tie
   group is LARGER than the page size, so a page boundary is guaranteed
   to fall inside it. Without both, the sweep never crosses a boundary
   where the order is undefined and the test would stay green on the
   broken code. That is exactly why the existing endpoint tests miss
   this class: they build a handful of rows with distinct timestamps.
3. Sweep every page to exhaustion; assert exactly N unique ids came
   back — nothing lost, nothing repeated.
4. Sweep again and assert the two sweeps agree.

Nothing writes between the pages and no planner setting is touched —
these are plain reads against a default Postgres.

**Postgres only.** SQLite breaks ties by rowid, which is stable by
accident, so this whole class is invisible there and a green run would
be measuring the wrong database.

No literal dates anywhere: every timestamp is an offset from ``now()``.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal

import pytest
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from ai.models import Conversation
from appointments.models import Appointment
from notifications.models import Notification
from reviews.models import Review
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import User


pytestmark = pytest.mark.django_db


# TOTAL rows at PAGE_SIZE per page. The newest DISTINCT_HEAD carry
# distinct timestamps; the remaining TIE_GROUP share one. The tie group
# is dozens of pages wide, so most page boundaries fall inside it — and
# the sweep is long enough for Postgres to switch sort strategies partway
# through, which is what makes the pages disagree.
TOTAL = 400
PAGE_SIZE = 10
DISTINCT_HEAD = 40
TIE_GROUP = TOTAL - DISTINCT_HEAD


# ---------------------------------------------------------------------------
# Guards and helpers
# ---------------------------------------------------------------------------


def _require_postgres() -> None:
    assert connection.vendor == "postgresql", (
        "This test only means something on Postgres. SQLite returns tied "
        "rows in rowid order, which hides the defect entirely. Set "
        "POSTGRES_DB *and* POSTGRES_HOST — without the host Django "
        "silently falls back to SQLite and checks the wrong thing."
    )


def _assert_tie_group(values: list, expected: int) -> None:
    """Positive guard: the data really contains the boundary case.

    ``values`` holds the full sort key of every row in the listing, in
    any order. Both halves matter — a tie must exist, and it must be
    wider than one page, or no page boundary lands inside it.
    """
    biggest = max(Counter(values).values())
    assert biggest == expected, (
        f"fixture built no tie group of {expected}: the largest group of "
        f"rows sharing a sort key is {biggest}. With no tie there is no "
        f"undefined order to expose, and the assertions below would pass "
        f"on any implementation."
    )
    assert biggest > PAGE_SIZE, (
        f"the tie group ({biggest}) must be wider than the page size "
        f"({PAGE_SIZE}) or no page boundary falls inside it."
    )


def _sweep(api: APIClient, url: str, extract) -> list:
    """Page to exhaustion, returning every id in the order served."""
    seen: list = []
    for page in range(1, TOTAL // PAGE_SIZE + 3):
        resp = api.get(url, {"page": page, "page_size": PAGE_SIZE})
        if resp.status_code == 404:
            break
        assert resp.status_code == 200, resp.data
        rows = extract(resp)
        if not rows:
            break
        seen.extend(str(r["id"]) for r in rows)
    return seen


def _assert_no_loss(seen: list, expected_ids: set, label: str) -> None:
    counts = Counter(seen)
    repeated = {i: n for i, n in counts.items() if n > 1}
    lost = expected_ids - set(seen)
    assert not lost and not repeated and len(seen) == TOTAL, (
        f"{label}: paging {TOTAL} rows at page_size={PAGE_SIZE} returned "
        f"{len(seen)} rows but only {len(counts)} distinct — "
        f"{len(lost)} row(s) never appeared and {len(repeated)} row(s) "
        f"appeared more than once "
        f"(repeat counts: {sorted(repeated.values(), reverse=True)})."
    )


def _stamps() -> list:
    """TOTAL timestamps, the last TIE_GROUP of them identical.

    Newest first, every one an offset from now — never a literal date.
    """
    now = timezone.now()
    tied = now - timezone.timedelta(days=1)
    head = [
        now - timezone.timedelta(minutes=i + 1) for i in range(DISTINCT_HEAD)
    ]
    return head + [tied] * TIE_GROUP


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="tiebreak", name="Tiebreak Tenant")


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="tie-client", password="x", role="client",
        phone="+79995200001",
    )


@pytest.fixture
def specialist_profile(db, tenant):
    user = User.objects.create_user(
        username="tie-spec", password="x", role="specialist",
        phone="+79995200002",
    )
    profile = user.specialist_profile
    profile.display_name = "Мастер Тай"
    profile.status = "active"
    profile.is_available = True
    profile.tenant = tenant
    profile.save()
    return profile


@pytest.fixture
def service(db, specialist_profile):
    category = ServiceCategory.objects.create(name="Тай-брейк")
    return Service.objects.create(
        specialist=specialist_profile,
        category=category,
        name="Услуга",
        price=Decimal("1500"),
        duration_minutes=60,
    )


@pytest.fixture
def auth_client(client_user):
    api = APIClient()
    api.defaults["HTTP_X_APP_TYPE"] = "client"
    api.force_authenticate(user=client_user)
    return api


# ---------------------------------------------------------------------------
# GET /api/v1/specialists/{id}/reviews/
# ---------------------------------------------------------------------------


@pytest.fixture
def reviews_fixture(client_user, specialist_profile, service, tenant) -> set:
    """TOTAL reviews for one specialist, TIE_GROUP of them co-stamped.

    A moderation backfill or an import writes a batch inside a single
    transaction and ``auto_now_add`` gives every row the same value.
    """
    now = timezone.now()
    appointments = [
        Appointment(
            client=client_user,
            specialist=specialist_profile,
            service=service,
            tenant=tenant,
            # Distinct, non-overlapping slots: the ties we care about are
            # on Review.created_at, not on the appointment calendar.
            start_datetime=now - timezone.timedelta(hours=2 * (n + 1)),
            end_datetime=now - timezone.timedelta(hours=2 * (n + 1) - 1),
            status="completed",
            price=service.price,
        )
        for n in range(TOTAL)
    ]
    Appointment.objects.bulk_create(appointments)

    reviews = [
        Review(
            appointment=appt,
            client=client_user,
            specialist=specialist_profile,
            service=service,
            tenant=tenant,
            rating=5,
            text=f"r{n}",
        )
        for n, appt in enumerate(appointments)
    ]
    Review.objects.bulk_create(reviews)

    # created_at is auto_now_add — assignment is ignored by save() and by
    # bulk_create, so the tie is stamped in with queryset update()s.
    for review, stamp in zip(reviews, _stamps()):
        Review.objects.filter(pk=review.pk).update(created_at=stamp)

    return {str(r.id) for r in reviews}


def _review_rows(resp):
    return resp.data["data"]


class TestSpecialistReviewsOrdering:

    def test_recent_sort_loses_no_review_on_tied_created_at(
        self, auth_client, specialist_profile, reviews_fixture,
    ):
        _require_postgres()
        _assert_tie_group(
            list(
                Review.objects.filter(specialist=specialist_profile)
                .values_list("created_at", flat=True)
            ),
            TIE_GROUP,
        )

        url = f"/api/v1/specialists/{specialist_profile.id}/reviews/"

        first = _sweep(auth_client, url, _review_rows)
        _assert_no_loss(first, reviews_fixture, "reviews ?sort=recent")

        second = _sweep(auth_client, url, _review_rows)
        assert first == second, (
            "two identical sweeps disagreed on order — the listing is not "
            "reproducible between requests."
        )

    def test_rating_sort_loses_no_review_on_tied_key(
        self, auth_client, specialist_profile, reviews_fixture,
    ):
        _require_postgres()
        # ?sort=rating orders on (-rating, -created_at). Every review here
        # carries rating=5, so the tie group is the co-stamped block.
        _assert_tie_group(
            list(
                Review.objects.filter(specialist=specialist_profile)
                .values_list("rating", "created_at")
            ),
            TIE_GROUP,
        )

        url = f"/api/v1/specialists/{specialist_profile.id}/reviews/?sort=rating"

        first = _sweep(auth_client, url, _review_rows)
        _assert_no_loss(first, reviews_fixture, "reviews ?sort=rating")

        second = _sweep(auth_client, url, _review_rows)
        assert first == second, (
            "two identical sweeps disagreed on order — the listing is not "
            "reproducible between requests."
        )


# ---------------------------------------------------------------------------
# GET /api/v1/notifications/
# ---------------------------------------------------------------------------


class TestNotificationsOrdering:

    def test_feed_loses_no_notification_on_tied_created_at(
        self, auth_client, client_user,
    ):
        _require_postgres()

        notes = [
            Notification(
                user=client_user,
                template_id="appointment_reminder_1h",
                channel=Notification.Channel.PUSH,
                title=f"t{n}", body=f"b{n}",
                status=Notification.Status.SENT,
            )
            for n in range(TOTAL)
        ]
        Notification.objects.bulk_create(notes)
        for note, stamp in zip(notes, _stamps()):
            Notification.objects.filter(pk=note.pk).update(created_at=stamp)
        expected = {str(n.id) for n in notes}

        # A reminder fan-out writes the whole batch in one transaction, so
        # auto_now_add stamps every row identically. That is the tie.
        _assert_tie_group(
            list(
                Notification.objects.filter(user=client_user)
                .values_list("created_at", flat=True)
            ),
            TIE_GROUP,
        )

        url = "/api/v1/notifications/"

        def rows(resp):
            return resp.data["data"]["results"]

        first = _sweep(auth_client, url, rows)
        _assert_no_loss(first, expected, "notifications feed")

        second = _sweep(auth_client, url, rows)
        assert first == second, (
            "two identical sweeps disagreed on order — the feed is not "
            "reproducible between requests."
        )


# ---------------------------------------------------------------------------
# GET /api/v1/ai/conversations/
# ---------------------------------------------------------------------------


class TestConversationsOrdering:

    def test_history_loses_no_conversation_on_tied_keys(
        self, auth_client, client_user,
    ):
        _require_postgres()

        # The partial unique index allows one active conversation per
        # (user, tenant), but tenant is NULL here and Postgres treats
        # every NULL as distinct, so the constraint does not bite.
        convs = [
            Conversation(user=client_user, is_active=True, tenant=None)
            for _ in range(TOTAL)
        ]
        Conversation.objects.bulk_create(convs)
        for conv, stamp in zip(convs, _stamps()):
            Conversation.objects.filter(pk=conv.pk).update(
                created_at=stamp, last_message_at=stamp,
            )
        expected = {str(c.id) for c in convs}

        _assert_tie_group(
            list(
                Conversation.objects.filter(user=client_user, is_active=True)
                .values_list("last_message_at", "created_at")
            ),
            TIE_GROUP,
        )

        url = "/api/v1/ai/conversations/"

        def rows(resp):
            return resp.data["data"]["results"]

        first = _sweep(auth_client, url, rows)
        _assert_no_loss(first, expected, "ai conversations")

        second = _sweep(auth_client, url, rows)
        assert first == second, (
            "two identical sweeps disagreed on order — the history is not "
            "reproducible between requests."
        )
