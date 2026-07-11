"""S3-CAL ExternalBusyIntervalProvider + read-path registration — TDD.

The provider implements the appointments BusyIntervalProvider Protocol and is
composed into make_read_provider() behind EXTERNAL_BUSY_ENABLED. Zero change to
SlotBuilder / AvailabilityQueryService.
Spec: docs/CATALOG_EXTERNAL_BUSY_S3CAL_DESIGN_2026-07.md.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from appointments.infrastructure.availability.providers import make_read_provider
from services.availability import ExternalBusyIntervalProvider
from services.models import ExternalBusyInterval
from tenants.models import Tenant
from users.models import SpecialistProfile, User

DAY_START = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
DAY_END = datetime(2026, 8, 1, 23, 59, 59, tzinfo=timezone.utc)


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="calp-t", name="CALP Tenant")


@pytest.fixture
def specialist(db, tenant):
    u = User.objects.create_user(
        username="calp_spec", password="x", role="specialist",
        phone="+79995505100",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.save()
    return p


def _busy(tenant, specialist, start, end, external_id=""):
    return ExternalBusyInterval.objects.create(
        tenant=tenant, specialist=specialist,
        start_at=start, end_at=end, external_id=external_id,
    )


@pytest.mark.django_db
class TestExternalBusyIntervalProvider:
    def test_returns_overlapping(self, tenant, specialist):
        start = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 8, 1, 11, 0, tzinfo=timezone.utc)
        _busy(tenant, specialist, start, end, "e1")
        out = ExternalBusyIntervalProvider().get_busy_intervals(
            specialist.id, DAY_START, DAY_END,
        )
        assert len(out) == 1
        assert out[0].start_at == start and out[0].end_at == end

    def test_excludes_non_overlapping(self, tenant, specialist):
        # entirely on the previous day
        _busy(
            tenant, specialist,
            datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 31, 11, 0, tzinfo=timezone.utc), "e2",
        )
        out = ExternalBusyIntervalProvider().get_busy_intervals(
            specialist.id, DAY_START, DAY_END,
        )
        assert out == []

    def test_clips_to_window(self, tenant, specialist):
        # spans from before the window start to inside it
        _busy(
            tenant, specialist,
            datetime(2026, 7, 31, 23, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc), "e3",
        )
        out = ExternalBusyIntervalProvider().get_busy_intervals(
            specialist.id, DAY_START, DAY_END,
        )
        assert len(out) == 1
        assert out[0].start_at == DAY_START  # clipped to window start
        assert out[0].end_at == datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc)

    def test_filters_by_specialist(self, tenant, specialist, db):
        other_u = User.objects.create_user(
            username="calp_other", password="x", role="specialist",
            phone="+79995505200",
        )
        other = SpecialistProfile.objects.get(user=other_u)
        other.tenant = tenant
        other.save()
        _busy(
            tenant, other,
            datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 1, 11, 0, tzinfo=timezone.utc), "e4",
        )
        out = ExternalBusyIntervalProvider().get_busy_intervals(
            specialist.id, DAY_START, DAY_END,
        )
        assert out == []


@pytest.mark.django_db
class TestReadProviderRegistration:
    def test_flag_off_excludes_external_provider(self, settings):
        settings.EXTERNAL_BUSY_ENABLED = False
        providers = make_read_provider()._providers
        assert not any(
            isinstance(p, ExternalBusyIntervalProvider) for p in providers
        )

    def test_flag_on_includes_external_provider(self, settings):
        settings.EXTERNAL_BUSY_ENABLED = True
        providers = make_read_provider()._providers
        assert any(
            isinstance(p, ExternalBusyIntervalProvider) for p in providers
        )
