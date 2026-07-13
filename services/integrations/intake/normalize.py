"""Normalize raw YClients service dicts → ``RawServiceRecord``.

Encodes the verified-contract conversions (design §10):
- ``seance_length`` is SECONDS → ``duration_min`` in minutes (rounded).
- ``is_folder`` rows are category folders, NOT bookable services → dropped.
- ``price_min`` / ``price_max`` become ``Decimal``; zero / absent → ``None``.

Pure functions — no DB, no network. The API adapter and (later) the CSV
adapter both feed dicts through here so downstream code sees one shape.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from services.integrations.yclients.dto import RawServiceRecord, RawStaffRecord


def _to_decimal(value) -> Decimal | None:
    """YClients price → Decimal. Zero / absent / unparsable → None."""
    if value in (None, "", 0, 0.0):
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return dec if dec != 0 else None


def _duration_minutes(seance_length) -> int | None:
    """YClients ``seance_length`` (seconds) → minutes, rounded half-up.

    Integer half-up (``(sec + 30) // 60``) instead of ``round()`` so a
    90.5-minute service rounds to 91, not 90 (``round`` uses banker's
    rounding, which is surprising for a duration).
    """
    if not isinstance(seance_length, (int, float)) or not seance_length:
        return None
    return int((seance_length + 30) // 60)


def normalize_service(raw: dict) -> RawServiceRecord | None:
    """One raw service dict → ``RawServiceRecord``, or ``None`` if a folder.

    ``is_folder`` truthy means the row is a category grouping in YClients,
    not a bookable service — the caller drops it.
    """
    if raw.get("is_folder"):
        return None

    category_id = raw.get("category_id")
    return RawServiceRecord(
        external_service_id=str(raw.get("id", "")),
        name=raw.get("title", ""),
        duration_min=_duration_minutes(raw.get("seance_length")),
        price_min=_to_decimal(raw.get("price_min")),
        price_max=_to_decimal(raw.get("price_max")),
        category_id=str(category_id) if category_id not in (None, "") else None,
        raw=raw,
    )


def normalize_services(raw_list: list[dict]) -> list[RawServiceRecord]:
    """Map a list of raw service dicts, dropping folders. Preserves order."""
    out: list[RawServiceRecord] = []
    for raw in raw_list:
        rec = normalize_service(raw)
        if rec is not None:
            out.append(rec)
    return out


def normalize_staff(raw: dict) -> RawStaffRecord:
    """One raw YClients staff dict → ``RawStaffRecord``."""
    return RawStaffRecord(
        external_staff_id=str(raw.get("id", "")),
        name=raw.get("name", ""),
        specialization=raw.get("specialization", "") or "",
        bookable=bool(raw.get("bookable", True)),
        raw=raw,
    )


def normalize_staff_list(raw_list: list[dict]) -> list[RawStaffRecord]:
    """Map a list of raw staff dicts. Preserves order."""
    return [normalize_staff(raw) for raw in raw_list]
