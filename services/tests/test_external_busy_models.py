"""S3-CAL ExternalBusyInterval model — TDD.

External (e.g. YClients) busy intervals that the slot busy-guard subtracts.
Source-abstracted: YClients is coupled only in the webhook ingress (S3-CAL.3).
Spec: docs/CATALOG_EXTERNAL_BUSY_S3CAL_DESIGN_2026-07.md.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.utils import timezone

from services.models import ExternalBusyInterval
from tenants.models import Tenant
from users.models import SpecialistProfile, User


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="cal-t", name="CAL Tenant")


@pytest.fixture
def specialist(db, tenant):
    u = User.objects.create_user(
        username="cal_spec", password="x", role="specialist",
        phone="+79995404100",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.yclients_staff_id = "9100"
    p.save()
    return p


@pytest.fixture
def window():
    start = timezone.now().replace(microsecond=0) + timedelta(days=1)
    return start, start + timedelta(hours=1)


@pytest.mark.django_db
class TestExternalBusyInterval:
    def test_create(self, tenant, specialist, window):
        start, end = window
        b = ExternalBusyInterval.objects.create(
            tenant=tenant, specialist=specialist,
            start_at=start, end_at=end, external_id="evt-1",
        )
        assert b.id is not None

    def test_source_defaults_yclients(self, tenant, specialist, window):
        start, end = window
        b = ExternalBusyInterval.objects.create(
            tenant=tenant, specialist=specialist, start_at=start, end_at=end,
        )
        assert b.source == "yclients"

    def test_raw_payload_defaults_dict(self, tenant, specialist, window):
        start, end = window
        b = ExternalBusyInterval.objects.create(
            tenant=tenant, specialist=specialist, start_at=start, end_at=end,
        )
        assert b.raw_payload == {}

    def test_end_must_be_after_start(self, tenant, specialist, window):
        start, end = window
        b = ExternalBusyInterval(
            tenant=tenant, specialist=specialist, start_at=end, end_at=start,
        )
        with pytest.raises(ValidationError):
            b.save()

    def test_idempotent_unique_on_external_id(self, tenant, specialist, window):
        start, end = window
        ExternalBusyInterval.objects.create(
            tenant=tenant, specialist=specialist,
            start_at=start, end_at=end, external_id="evt-dup",
        )
        with pytest.raises(IntegrityError):
            ExternalBusyInterval.objects.create(
                tenant=tenant, specialist=specialist,
                start_at=start, end_at=end, external_id="evt-dup",
            )

    def test_blank_external_id_allows_multiple(self, tenant, specialist, window):
        start, end = window
        ExternalBusyInterval.objects.create(
            tenant=tenant, specialist=specialist,
            start_at=start, end_at=end, external_id="",
        )
        ExternalBusyInterval.objects.create(
            tenant=tenant, specialist=specialist,
            start_at=start, end_at=end, external_id="",
        )
        assert ExternalBusyInterval.objects.filter(external_id="").count() == 2
