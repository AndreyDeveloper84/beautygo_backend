"""S3C PR4 — CSV bootstrap adapter + loader command.

CsvSource feeds the SAME IntakePipeline as the YClients API adapter
(unified-pipeline design), so the pilot catalog can be seeded from a
`mysite` last-sync export without waiting on the YClients licence.

CSV columns:
    external_service_id,title,duration_min,price_min,price_max,category,staff_ids
`staff_ids` is `;`-separated (comma is the CSV delimiter).
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from services.integrations.intake.sources import CsvSource
from services.integrations.yclients.dto import RawServiceRecord
from services.models import (
    DraftSalonService,
    ServiceCategory,
    SpecialistService,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, User

_CSV = (
    "external_service_id,title,duration_min,price_min,price_max,category,staff_ids\n"
    "101,Массаж,60,1500,2500,Массаж,10;11\n"
    "102,Маникюр,90,2000,,Ногти,\n"
)


@pytest.fixture
def csv_path(tmp_path):
    p = tmp_path / "pilot_catalog.csv"
    p.write_text(_CSV, encoding="utf-8")
    return str(p)


class TestCsvSourceParsing:
    def test_parses_rows_to_records(self, csv_path):
        recs = CsvSource(csv_path).fetch_services()
        assert len(recs) == 2
        assert all(isinstance(r, RawServiceRecord) for r in recs)
        r0 = recs[0]
        assert r0.external_service_id == "101"
        assert r0.name == "Массаж"
        assert r0.duration_min == 60  # already minutes — no seconds conversion
        assert r0.price_min == Decimal("1500")
        assert r0.price_max == Decimal("2500")
        assert r0.category_id == "Массаж"
        assert r0.external_staff_ids == ("10", "11")
        assert r0.raw["staff_ids"] == ["10", "11"]

    def test_empty_optional_fields(self, csv_path):
        r1 = CsvSource(csv_path).fetch_services()[1]
        assert r1.price_max is None
        assert r1.external_staff_ids == ()

    def test_fetch_staff_is_empty(self, csv_path):
        # Staff arrive embedded in each service row's staff_ids, not a sheet.
        assert CsvSource(csv_path).fetch_staff() == []


@pytest.mark.django_db
class TestLoaderCommand:
    @pytest.fixture
    def tenant(self):
        return Tenant.objects.create(slug="penza-salon", name="Penza Salon")

    def test_loads_csv_into_drafts(self, tenant, csv_path):
        call_command("intake_csv", "--tenant", "penza-salon", "--file", csv_path)
        assert DraftSalonService.objects.filter(tenant=tenant).count() == 2
        d = DraftSalonService.objects.get(tenant=tenant, external_service_id="101")
        assert d.external_source == DraftSalonService.ExternalSource.CSV
        assert d.raw_payload["staff_ids"] == ["10", "11"]

    def test_dry_run_writes_nothing(self, tenant, csv_path):
        call_command("intake_csv", "--tenant", "penza-salon", "--file", csv_path, "--dry-run")
        assert DraftSalonService.objects.count() == 0

    def test_unknown_tenant_raises(self, csv_path, db):
        with pytest.raises(CommandError):
            call_command("intake_csv", "--tenant", "nope", "--file", csv_path)

    def test_missing_file_raises(self, tenant):
        with pytest.raises(CommandError):
            call_command("intake_csv", "--tenant", "penza-salon", "--file", "/no/such.csv")

    def test_full_pilot_path_csv_to_bookable(self, tenant, csv_path):
        # CSV → drafts → intake_confirm → bookable SpecialistService, using
        # staff_ids carried in raw_payload. staff 10 exists, 11 does not.
        user = User.objects.create_user(
            username="m10", password="x", role="specialist", phone="+79995550010",
        )
        sp, _ = SpecialistProfile.objects.get_or_create(
            user=user, defaults={"display_name": "Мастер"},
        )
        sp.tenant = tenant
        sp.yclients_staff_id = "10"
        sp.save(update_fields=["tenant", "yclients_staff_id"])
        category = ServiceCategory.objects.create(name="Массаж cat")

        call_command("intake_csv", "--tenant", "penza-salon", "--file", csv_path)
        call_command("intake_confirm", "--tenant", "penza-salon", "--category", str(category.pk))

        # service 101 had staff 10 (matched) + 11 (unmatched) → 1 bookable row.
        ss = SpecialistService.objects.filter(specialist=sp)
        assert ss.count() == 1
