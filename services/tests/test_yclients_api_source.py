"""S3C — YClientsApiSource: client pull → normalized RawServiceRecord[].

The source adapter is the intake pipeline's fetch stage: it calls the
(mocked) client and runs the raw dicts through ``normalize_services``.
PR1 stops here — writing DraftSalonService is a later chunk.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from services.integrations.intake.sources import YClientsApiSource
from services.integrations.yclients.dto import RawServiceRecord


def test_fetch_services_normalizes_and_drops_folders():
    client = MagicMock()
    client.list_services.return_value = [
        {"id": 1, "title": "A", "seance_length": 3600, "price_min": 1500},
        {"id": 2, "title": "Folder", "is_folder": True},
        {"id": 3, "title": "B", "seance_length": 1800},
    ]
    recs = YClientsApiSource(client).fetch_services()
    assert [r.external_service_id for r in recs] == ["1", "3"]
    assert all(isinstance(r, RawServiceRecord) for r in recs)
    assert recs[0].duration_min == 60
    assert recs[0].price_min == Decimal("1500")


def test_fetch_services_empty_when_client_returns_nothing():
    # Mirrors the expired-licence reality: management endpoint yields [].
    client = MagicMock()
    client.list_services.return_value = []
    assert YClientsApiSource(client).fetch_services() == []


def test_fetch_staff_normalizes_records():
    client = MagicMock()
    client.list_staff.return_value = [
        {"id": 10, "name": "Мастер", "specialization": "массаж", "bookable": True},
    ]
    staff = YClientsApiSource(client).fetch_staff()
    assert staff[0].external_staff_id == "10"
    assert staff[0].bookable is True
