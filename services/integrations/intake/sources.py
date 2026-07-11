"""Intake source adapters — one normalized shape, many origins.

A source yields normalized DTOs; the pipeline (later chunk) maps them
onto ``DraftSalonService`` / ``ExternalSourceMapping``. Keeping the
adapter behind a ``Protocol`` lets the CSV bootstrap (PR4) drop in
without touching pipeline logic — the founder-approved "unified
pipeline + 2 adapters" design.

PR1 ships the YClients API adapter only. It stops at "normalized DTOs";
no DB writes, no S3A model imports.
"""
from __future__ import annotations

from typing import Protocol

from services.integrations.intake.normalize import (
    normalize_services,
    normalize_staff_list,
)
from services.integrations.yclients.dto import RawServiceRecord, RawStaffRecord


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
