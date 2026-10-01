"""Safeguards shared by the management commands that move money (DRF-2689).

Two commands do: ``retry_capture`` (captures a held payment — a charge to
the client, irreversible) and ``complete_elapsed_backlog`` (closes visits,
which charges the platform fee). Both are operational: the place they are
meant to run is the live database. So the seed commands' guard — «only on a
database with ``e2e`` / ``golden`` in its name» (``bootstrap_e2e_wave1``,
``seed_golden``) — does not carry over: it would forbid them exactly where
they are needed, and nothing in the code names the live database.

What can go wrong here is the operator being wrong about which shell they
are in. Hence:

* **the operator names the database** (``--database``); the command compares
  it with the one it is connected to and refuses, before the first write, on
  a mismatch or when it was not named. No name is hard-coded and there is no
  hatch — naming the database *is* the deliberate act;
* **the provider mode is shown and recorded** (``test`` / ``live`` by the
  YooKassa key prefix). It is a witness, not a refusal: a copy of the live
  database on a stand with live keys is the case a database name cannot see.
  The key itself is never printed or stored;
* **every run leaves a receipt** — one ``AnalyticsEvent`` — written *before*
  the first write of a real run. The charge is external and cannot share a
  transaction with the receipt, so the order is the only fail-closed form:
  no receipt, no charge. The receipt records intent; the outcome of each
  payment stays in its own row.

A hard rule «money commands run only on database X» needs the owner's word
(which stand is the money stand; whether stands with live keys exist on
purpose) and is DRF-2699, not this module.
"""
from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.core.management.base import CommandError

from analytics.models import AnalyticsEvent
from privacy_audit.retention import OPERATOR_MAX_LENGTH


class ReceiptUnwritten(CommandError):
    """The run receipt could not be written — nothing was done."""


def database_name() -> str:
    """The database this process is connected to."""
    return str(settings.DATABASES["default"].get("NAME", ""))


def provider_mode() -> str:
    """``test`` / ``live`` by the YooKassa secret key prefix.

    ``unset`` — no key; ``unknown`` — a key of neither shape. Reads the
    prefix only: the key is not returned, printed or stored.
    """
    key = str(getattr(settings, "YOOKASSA_SECRET_KEY", "") or "")
    if not key:
        return "unset"
    if key.startswith("test_"):
        return "test"
    if key.startswith("live_"):
        return "live"
    return "unknown"


def require_named_database(named: str | None, *, command: str, required: bool) -> str:
    """Refuse unless the operator named the database this process is on.

    ``required`` — a real run. A dry run may omit the name; a name that was
    given is compared either way, because an operator who is wrong about
    where they are should hear it before they add ``--apply``.
    """
    actual = database_name()
    if named is None:
        if required:
            raise CommandError(
                f"{command}: --database is required to write. Name the database "
                "you mean to change; a dry run prints the one this process is "
                "connected to."
            )
        return actual
    if named != actual:
        raise CommandError(
            f"{command}: --database {named!r} is not the database this process "
            f"is connected to ({actual!r}). Nothing was done."
        )
    return actual


def clean_operator(raw: str | None, *, command: str, required: bool) -> str:
    """The ``--operator`` label for the receipt: a role or a tag, not a name.

    Not verified — the receipt answers «who said they ran it». Over the
    column length is a refusal, not a truncation (DRF-2653).
    """
    operator = (raw or "").strip()
    if not operator:
        if required:
            raise CommandError(
                f"{command}: --operator is required to write — the run receipt "
                "is not written without it."
            )
        return ""
    if len(operator) > OPERATOR_MAX_LENGTH:
        raise CommandError(
            f"{command}: --operator is longer than {OPERATOR_MAX_LENGTH} "
            "characters; it is not truncated."
        )
    return operator


def write_receipt(event_name: str, payload: dict[str, Any]) -> AnalyticsEvent:
    """One server-side receipt row, in the shape of the other ``*_RUN`` events."""
    return AnalyticsEvent.objects.create(
        event_name=event_name,
        payload=payload,
        app_type=AnalyticsEvent.AppType.CLIENT,
        client_event_id=uuid.uuid4(),
    )
