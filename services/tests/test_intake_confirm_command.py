"""S3C PR3 — `manage.py intake_confirm` mgmt command.

Thin CLI over confirm_draft: resolve tenant + (optional) draft/category,
confirm pending drafts, report a summary. --dry-run lists without writing.
"""
from __future__ import annotations

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from services.models import (
    DraftSalonService,
    SalonService,
    ServiceCategory,
    ServiceTemplate,
    SpecialistService,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant():
    return Tenant.objects.create(slug="penza-salon", name="Penza Salon")


@pytest.fixture
def template():
    cat = ServiceCategory.objects.create(name="Массаж S3C")
    return ServiceTemplate.objects.create(
        category=cat, name="Классический массаж", name_short="Массаж",
        duration_default=60, requires_health_check=False,
    )


@pytest.fixture
def specialist(tenant):
    user = User.objects.create_user(
        username="m10", password="x", role="specialist", phone="+79995550010",
    )
    sp, _ = SpecialistProfile.objects.get_or_create(
        user=user, defaults={"display_name": "Мастер"},
    )
    sp.tenant = tenant
    sp.yclients_staff_id = "10"
    sp.save(update_fields=["tenant", "yclients_staff_id"])
    return sp


def _draft(tenant, template, eid="101"):
    return DraftSalonService.objects.create(
        tenant=tenant,
        external_source=DraftSalonService.ExternalSource.YCLIENTS,
        external_service_id=eid, external_name="Массаж",
        suggested_template=template, suggested_duration=60,
        suggested_price=Decimal("1500"), raw_payload={"id": eid},
    )


def test_confirms_pending_drafts_and_makes_bookable(tenant, template, specialist):
    draft = _draft(tenant, template)
    out = StringIO()
    call_command("intake_confirm", "--tenant", "penza-salon", "--staff", "10", stdout=out)
    draft.refresh_from_db()
    assert draft.status == DraftSalonService.Status.CONFIRMED
    assert SalonService.objects.filter(tenant=tenant).count() == 1
    assert SpecialistService.objects.count() == 1


def test_unknown_tenant_raises(db):
    with pytest.raises(CommandError):
        call_command("intake_confirm", "--tenant", "nope")


def test_dry_run_confirms_nothing(tenant, template, specialist):
    draft = _draft(tenant, template)
    call_command("intake_confirm", "--tenant", "penza-salon", "--staff", "10", "--dry-run")
    draft.refresh_from_db()
    assert draft.status == DraftSalonService.Status.PENDING
    assert SalonService.objects.count() == 0


def test_single_draft_by_id(tenant, template, specialist):
    d1 = _draft(tenant, template, eid="101")
    d2 = _draft(tenant, template, eid="102")
    call_command("intake_confirm", "--tenant", "penza-salon", "--draft-id", str(d1.pk), "--staff", "10")
    d1.refresh_from_db()
    d2.refresh_from_db()
    assert d1.status == DraftSalonService.Status.CONFIRMED
    assert d2.status == DraftSalonService.Status.PENDING
