"""Intake source adapters — one normalized shape, many origins.

A source yields normalized DTOs; the pipeline (later chunk) maps them
onto ``DraftSalonService`` / ``ExternalSourceMapping``. Keeping the
adapter behind a ``Protocol`` lets the CSV bootstrap (PR4) drop in
without touching pipeline logic — the founder-approved "unified
pipeline + 2 adapters" design.

Two adapters: ``YClientsApiSource`` (API-pull) and ``CsvSource`` (bootstrap
seed). Both emit ``RawServiceRecord[]`` so the pipeline is source-agnostic.
"""
from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from typing import Protocol

from services.integrations.intake.normalize import (
    normalize_services,
    normalize_staff_list,
)
from services.integrations.yclients.dto import RawServiceRecord, RawStaffRecord


class CsvIntakeError(Exception):
    """The bootstrap CSV is unreadable or malformed."""


class ServiceSource(Protocol):
    """A source of normalized catalog records for intake."""

    def fetch_services(self) -> list[RawServiceRecord]: ...

    def fetch_staff(self) -> list[RawStaffRecord]: ...


class YClientsApiSource:
    """API-pull adapter — wraps a ``YClientsClient`` and normalizes.

    Injected client keeps this testable without network and lets callers
    build the client with their own creds / company id.
    """

    def __init__(self, client):
        self.client = client

    def fetch_services(self) -> list[RawServiceRecord]:
        return normalize_services(self.client.list_services())

    def fetch_staff(self) -> list[RawStaffRecord]:
        return normalize_staff_list(self.client.list_staff())


# --------------------------------------------------------------------------- #
# CSV bootstrap adapter (PR4)
# --------------------------------------------------------------------------- #

_CSV_STAFF_SEP = ";"  # comma is the CSV delimiter, so staff ids use ';'


def _parse_int(value, line_no: int, field_name: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise CsvIntakeError(
            f"line {line_no}: {field_name}={value!r} is not an integer"
        ) from exc


def _parse_decimal(value, line_no: int, field_name: str) -> Decimal | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise CsvIntakeError(
            f"line {line_no}: {field_name}={value!r} is not a number"
        ) from exc


class CsvSource:
    """CSV bootstrap adapter — pilot seed without the YClients licence.

    Emits the SAME ``RawServiceRecord`` shape as the API adapter, so the
    pipeline is source-agnostic. Columns::

        external_service_id,title,duration_min,price_min,price_max,category,staff_ids

    ``duration_min`` is already minutes (no seconds conversion). ``staff_ids``
    is ``;``-separated and carried into ``raw['staff_ids']`` so intake_confirm
    can make the service bookable for those specialists.
    """

    def __init__(self, path: str):
        self.path = path

    def fetch_services(self) -> list[RawServiceRecord]:
        try:
            handle = open(self.path, newline="", encoding="utf-8-sig")
        except OSError as exc:
            raise CsvIntakeError(f"cannot open CSV {self.path!r}: {exc}") from exc
        records: list[RawServiceRecord] = []
        with handle:
            reader = csv.DictReader(handle)
            for line_no, row in enumerate(reader, start=2):  # header = line 1
                records.append(self._row_to_record(row, line_no))
        return records

    def fetch_staff(self) -> list[RawStaffRecord]:
        # Staff arrive embedded in each service row's staff_ids, not a sheet.
        return []

    @staticmethod
    def _row_to_record(row: dict, line_no: int) -> RawServiceRecord:
        staff_ids = [
            s.strip()
            for s in (row.get("staff_ids") or "").split(_CSV_STAFF_SEP)
            if s.strip()
        ]
        category = (row.get("category") or "").strip()
        raw = {**row, "staff_ids": staff_ids}
        return RawServiceRecord(
            external_service_id=(row.get("external_service_id") or "").strip(),
            name=(row.get("title") or "").strip(),
            duration_min=_parse_int(row.get("duration_min"), line_no, "duration_min"),
            price_min=_parse_decimal(row.get("price_min"), line_no, "price_min"),
            price_max=_parse_decimal(row.get("price_max"), line_no, "price_max"),
            category_id=category or None,
            external_staff_ids=tuple(staff_ids),
            raw=raw,
        )
