"""S3C — normalize YClients raw service dicts → RawServiceRecord.

Pure-Python (no DB). Encodes the verified-contract conversions from
docs/S3C_YCLIENTS_INTAKE_DESIGN.md §10:
- seance_length is in SECONDS → duration in minutes
- is_folder=true rows are category folders, NOT bookable services → drop
- prices become Decimal; zero/absent → None
"""
from __future__ import annotations

from decimal import Decimal

from services.integrations.intake.normalize import (
    normalize_service,
    normalize_services,
)
from services.integrations.yclients.dto import RawServiceRecord


class TestNormalizeService:
    def test_seance_length_seconds_to_minutes(self):
        rec = normalize_service({"id": 1, "title": "Массаж", "seance_length": 3600})
        assert rec.duration_min == 60

    def test_seance_length_rounds_to_nearest_minute(self):
        rec = normalize_service({"id": 1, "title": "X", "seance_length": 5430})
        assert rec.duration_min == 91  # 90.5 min → 91

    def test_missing_seance_length_gives_none_duration(self):
        rec = normalize_service({"id": 4, "title": "X"})
        assert rec.duration_min is None

    def test_folder_row_is_dropped(self):
        assert normalize_service({"id": 2, "title": "Категория", "is_folder": True}) is None

    def test_prices_become_decimal(self):
        rec = normalize_service({"id": 3, "title": "X", "price_min": 1500, "price_max": 2500})
        assert rec.price_min == Decimal("1500")
        assert rec.price_max == Decimal("2500")

    def test_zero_price_is_none(self):
        rec = normalize_service({"id": 3, "title": "X", "price_min": 0})
        assert rec.price_min is None

    def test_external_service_id_is_string(self):
        rec = normalize_service({"id": 12345, "title": "X"})
        assert rec.external_service_id == "12345"
        assert isinstance(rec, RawServiceRecord)

    def test_category_id_stringified(self):
        rec = normalize_service({"id": 1, "title": "X", "category_id": 88})
        assert rec.category_id == "88"

    def test_raw_payload_preserved(self):
        raw = {"id": 1, "title": "X", "weird_field": "keep"}
        rec = normalize_service(raw)
        assert rec.raw == raw


class TestNormalizeServices:
    def test_filters_folders_and_keeps_order(self):
        raw = [
            {"id": 1, "title": "A", "seance_length": 1800},
            {"id": 2, "title": "Folder", "is_folder": True},
            {"id": 3, "title": "B", "seance_length": 3600},
        ]
        out = normalize_services(raw)
        assert [r.external_service_id for r in out] == ["1", "3"]

    def test_empty_list(self):
        assert normalize_services([]) == []
