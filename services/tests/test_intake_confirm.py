"""S3C PR3 — confirm a DraftSalonService into bookable catalog rows.

confirm_draft materializes:
- SalonService (mid layer, source=yclients) — reused idempotently via
  ExternalSourceMapping(external_type='service').
- SpecialistService (bookable) for each YClients staff id that resolves to
  a SpecialistProfile (price/duration from the draft).
and marks the draft CONFIRMED (confirmed_salon_service / confirmed_at).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from services.integrations.intake.confirm import DraftNotConfirmable, confirm_draft
from services.models import (
    DraftSalonService,
    ExternalSourceMapping,
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
def category():
    return ServiceCategory.objects.create(name="Массаж S3C")


@pytest.fixture
def template(category):
    return ServiceTemplate.objects.create(
        category=category, name="Классический массаж", name_short="Массаж",
        duration_default=60, requires_health_check=False,
    )


def _staff(tenant, staff_id="10", name="Мастер"):
    user = User.objects.create_user(
        username=f"m{staff_id}", password="x", role="specialist",
        phone=f"+7999555{staff_id:0>4}",
    )
    sp, _ = SpecialistProfile.objects.get_or_create(
        user=user, defaults={"display_name": name},
    )
    sp.tenant = tenant
    sp.yclients_staff_id = staff_id
    sp.save(update_fields=["tenant", "yclients_staff_id"])
    return sp


def _draft(tenant, template=None, *, eid="101", name="Массаж",
           duration=60, price="1500"):
    return DraftSalonService.objects.create(
        tenant=tenant,
        external_source=DraftSalonService.ExternalSource.YCLIENTS,
        external_service_id=eid, external_name=name,
        suggested_template=template,
        suggested_duration=duration,
        suggested_price=Decimal(price) if price is not None else None,
        raw_payload={"id": eid},
    )


class TestSalonServiceMaterialization:
    def test_confirm_creates_salon_service_and_mapping(self, tenant, template):
        draft = _draft(tenant, template)
        result = confirm_draft(draft, staff_ids=[])
        salon = SalonService.objects.get(tenant=tenant, name="Массаж")
        assert salon.source == SalonService.Source.YCLIENTS
        assert salon.template_id == template.id
        assert salon.duration_minutes == 60
        assert salon.base_price == Decimal("1500")
        assert result.created_salon_service is True
        mapping = ExternalSourceMapping.objects.get(
            external_type=ExternalSourceMapping.ExternalType.SERVICE,
            external_id="101", tenant=tenant,
        )
        assert mapping.salon_service_id == salon.id
        draft.refresh_from_db()
        assert draft.status == DraftSalonService.Status.CONFIRMED
        assert draft.confirmed_salon_service_id == salon.id
        assert draft.confirmed_at is not None

    def test_confirm_offtaxonomy_needs_category(self, tenant):
        draft = _draft(tenant, template=None)
        with pytest.raises(DraftNotConfirmable):
            confirm_draft(draft, staff_ids=[])

    def test_confirm_offtaxonomy_with_fallback_category(self, tenant, category):
        draft = _draft(tenant, template=None)
        confirm_draft(draft, staff_ids=[], fallback_category=category)
        salon = SalonService.objects.get(tenant=tenant, name="Массаж")
        assert salon.template_id is None
        assert salon.category_id == category.id

    def test_reconfirm_is_idempotent(self, tenant, template):
        draft = _draft(tenant, template)
        confirm_draft(draft, staff_ids=[])
        confirm_draft(draft, staff_ids=[])
        assert SalonService.objects.filter(tenant=tenant).count() == 1
        assert ExternalSourceMapping.objects.filter(
            external_type=ExternalSourceMapping.ExternalType.SERVICE,
        ).count() == 1

    def test_rejected_draft_refused(self, tenant, template):
        draft = _draft(tenant, template)
        draft.status = DraftSalonService.Status.REJECTED
        draft.save(update_fields=["status"])
        with pytest.raises(DraftNotConfirmable):
            confirm_draft(draft, staff_ids=[])

    def test_salon_name_collision_is_graceful(self, tenant, template):
        # Two distinct YClients services sharing template+name collide on the
        # SalonService unique (tenant, template, name). Must surface as a
        # DraftNotConfirmable (seed-safe), never a raw IntegrityError crash.
        confirm_draft(_draft(tenant, template, eid="101", name="Массаж"), staff_ids=[])
        with pytest.raises(DraftNotConfirmable):
            confirm_draft(_draft(tenant, template, eid="102", name="Массаж"), staff_ids=[])


class TestGracefulDegradation:
    def test_offtaxonomy_without_duration_skips_specialist_not_crash(self, tenant, category):
        _staff(tenant, "10")
        # No template + no duration → SpecialistService has no resolvable
        # duration; must skip the bookable row (reported) and still confirm.
        draft = _draft(tenant, template=None, duration=None, price="1500")
        result = confirm_draft(draft, staff_ids=["10"], fallback_category=category)
        assert result.specialist_services_skipped_invalid == 1
        assert SpecialistService.objects.count() == 0
        draft.refresh_from_db()
        assert draft.status == DraftSalonService.Status.CONFIRMED


class TestSpecialistServiceBookability:
    def test_specialist_service_created_for_matched_staff(self, tenant, template):
        _staff(tenant, "10")
        draft = _draft(tenant, template)
        result = confirm_draft(draft, staff_ids=["10"])
        assert result.specialist_services_created == 1
        ss = SpecialistService.objects.get()
        assert ss.price == Decimal("1500")
        assert ss.duration_minutes == 60
        assert ss.is_active is True
        assert ss.tenant_id == tenant.id

    def test_unmatched_staff_reported(self, tenant, template):
        draft = _draft(tenant, template)
        result = confirm_draft(draft, staff_ids=["999"])
        assert result.unmatched_staff == ["999"]
        assert SpecialistService.objects.count() == 0

    def test_no_price_skips_bookable(self, tenant, template):
        _staff(tenant, "10")
        draft = _draft(tenant, template, price=None)
        result = confirm_draft(draft, staff_ids=["10"])
        assert result.specialist_services_skipped_no_price == 1
        assert SpecialistService.objects.count() == 0

    def test_specialist_service_idempotent(self, tenant, template):
        _staff(tenant, "10")
        draft = _draft(tenant, template)
        confirm_draft(draft, staff_ids=["10"])
        confirm_draft(draft, staff_ids=["10"])
        assert SpecialistService.objects.count() == 1

    def test_staff_ids_default_from_raw_payload(self, tenant, template):
        _staff(tenant, "10")
        draft = _draft(tenant, template)
        draft.raw_payload = {"id": "101", "staff_ids": ["10"]}
        draft.save(update_fields=["raw_payload"])
        result = confirm_draft(draft)  # no explicit staff_ids
        assert result.specialist_services_created == 1
