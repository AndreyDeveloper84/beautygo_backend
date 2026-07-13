"""S3C PR2 — intake pipeline wires normalized DTOs into S3A models.

- Services → ``DraftSalonService`` (idempotent upsert by external_service_id,
  preserves human confirm/reject status on re-import).
- Staff → ``ExternalSourceMapping`` (staff→SpecialistProfile via
  yclients_staff_id; idempotent by the unique key).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from services.integrations.intake.pipeline import import_catalog
from services.integrations.yclients.dto import RawServiceRecord, RawStaffRecord
from services.models import DraftSalonService, ExternalSourceMapping
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db


class FakeSource:
    def __init__(self, services=(), staff=()):
        self._services = list(services)
        self._staff = list(staff)

    def fetch_services(self):
        return list(self._services)

    def fetch_staff(self):
        return list(self._staff)


def _svc(external_id, name="Массаж", duration=60, price="1500"):
    return RawServiceRecord(
        external_service_id=external_id,
        name=name,
        duration_min=duration,
        price_min=Decimal(price) if price is not None else None,
        raw={"id": external_id},
    )


@pytest.fixture
def tenant():
    return Tenant.objects.create(slug="penza-salon", name="Penza Salon")


@pytest.fixture
def specialist(tenant):
    user = User.objects.create_user(
        username="master1", password="x", role="specialist", phone="+79995551010",
    )
    # A specialist-role user auto-gets a SpecialistProfile via signal — reuse it.
    sp, _ = SpecialistProfile.objects.get_or_create(
        user=user, defaults={"display_name": "Мастер"},
    )
    sp.tenant = tenant
    sp.yclients_staff_id = "10"
    sp.save(update_fields=["tenant", "yclients_staff_id"])
    return sp


class TestServiceDrafts:
    def test_creates_drafts(self, tenant):
        summary = import_catalog(FakeSource(services=[_svc("101")]), tenant)
        assert summary.services_created == 1
        d = DraftSalonService.objects.get(tenant=tenant, external_service_id="101")
        assert d.external_name == "Массаж"
        assert d.suggested_duration == 60
        assert d.suggested_price == Decimal("1500")
        assert d.external_source == DraftSalonService.ExternalSource.YCLIENTS
        assert d.status == DraftSalonService.Status.PENDING
        assert d.raw_payload == {"id": "101"}

    def test_reimport_is_idempotent(self, tenant):
        src = FakeSource(services=[_svc("101")])
        import_catalog(src, tenant)
        summary2 = import_catalog(src, tenant)
        assert summary2.services_created == 0
        assert summary2.services_updated == 1
        assert DraftSalonService.objects.filter(tenant=tenant).count() == 1

    def test_reimport_updates_fields_but_preserves_confirmed_status(self, tenant):
        import_catalog(FakeSource(services=[_svc("101", name="Старое")]), tenant)
        d = DraftSalonService.objects.get(external_service_id="101")
        d.status = DraftSalonService.Status.CONFIRMED
        d.save(update_fields=["status"])

        import_catalog(FakeSource(services=[_svc("101", name="Новое", price="2000")]), tenant)
        d.refresh_from_db()
        assert d.status == DraftSalonService.Status.CONFIRMED  # not reset
        assert d.external_name == "Новое"
        assert d.suggested_price == Decimal("2000")

    def test_empty_external_id_is_skipped(self, tenant):
        summary = import_catalog(FakeSource(services=[_svc("")]), tenant)
        assert summary.services_skipped == 1
        assert DraftSalonService.objects.count() == 0

    def test_long_name_truncated_to_200(self, tenant):
        import_catalog(FakeSource(services=[_svc("101", name="x" * 250)]), tenant)
        d = DraftSalonService.objects.get(external_service_id="101")
        assert len(d.external_name) == 200

    def test_drafts_scoped_per_tenant(self, tenant):
        other = Tenant.objects.create(slug="other", name="Other")
        import_catalog(FakeSource(services=[_svc("101")]), tenant)
        import_catalog(FakeSource(services=[_svc("101")]), other)
        # same external id, two tenants → two independent drafts
        assert DraftSalonService.objects.filter(external_service_id="101").count() == 2


class TestStaffMapping:
    def test_maps_staff_to_specialist(self, tenant, specialist):
        summary = import_catalog(
            FakeSource(staff=[RawStaffRecord("10", "Мастер")]), tenant,
        )
        assert summary.staff_mapped == 1
        m = ExternalSourceMapping.objects.get(
            external_type=ExternalSourceMapping.ExternalType.STAFF,
            external_id="10", tenant=tenant,
        )
        assert m.specialist_id == specialist.id
        assert m.salon_service_id is None

    def test_unmatched_staff_counted_and_not_mapped(self, tenant):
        summary = import_catalog(
            FakeSource(staff=[RawStaffRecord("999", "Никто")]), tenant,
        )
        assert summary.staff_unmatched == 1
        assert not ExternalSourceMapping.objects.filter(external_id="999").exists()

    def test_staff_mapping_idempotent(self, tenant, specialist):
        src = FakeSource(staff=[RawStaffRecord("10", "Мастер")])
        import_catalog(src, tenant)
        import_catalog(src, tenant)
        assert ExternalSourceMapping.objects.filter(
            external_type=ExternalSourceMapping.ExternalType.STAFF,
            external_id="10", tenant=tenant,
        ).count() == 1
