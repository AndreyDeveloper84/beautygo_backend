"""The guaranteed queue behind the access journal.

Owner ruling (10.09.2026): operations that neither disclose nor destroy must
not stop when the journal is unavailable — «для них допустима гарантированная
очередь с последующей записью».

**Гарантированная** is the load-bearing word. A queue without proven delivery
is the same as no record at all, only calmer-looking. Two things follow, and
both are mechanisms here rather than intentions:

### 1. The queue must not share a failure mode with what it insures

This is §23 of the executor rules — a guard placed inside the thing it
watches. If the journal cannot be written because the database is unavailable,
a queue that is a table in that same database fails identically and buys
nothing. So the spool is an append-only file: a different mechanism, failing
for different reasons.

That choice has a cost, named rather than hidden: the spool is **per-process
local storage**. On a multi-instance deployment each instance holds its own
spool file, and draining must run where the file is (or the directory must be
shared storage). The drain command says so, and ops has to know it.

### 2. "The queue is empty" must be a counter, not an absence

:func:`pending_count` counts records that were spooled and not yet drained.
Zero from it means "nothing is waiting" only because the same function returns
non-zero when something is. An absence of log lines would not have that
property — which is exactly the trap the staged counter already walked into
once on this surface.

### What is written here

The same fields the journal row carries, and nothing else: no exported values,
no personal-context values, no credential, no caller-supplied header. A spool
file is a file on a disk somebody can read; it must not become the one place
where personal data sits in plain text.

### The single path to a lost record, stated out loud

If the synchronous write fails AND the spool write fails, the record is lost.
That is logged at CRITICAL and the operation still proceeds — for queued
operations only, because the owner decided that our outage must not stop the
product for a person acting on their own data. It is the only loss path, and
naming it is the difference between a known limit and a surprise.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings

logger = logging.getLogger("privacy_audit")

_SUFFIX = ".audit.json"


class SpoolUnavailable(RuntimeError):
    """The queue itself could not accept the record — the only loss path."""


def spool_dir() -> Path:
    """Where queued records live.

    Default sits under the project directory rather than the system temp:
    a queue that a reboot can clear is not a guaranteed queue.
    """
    configured = getattr(settings, "PRIVACY_AUDIT_SPOOL_DIR", "") or ""
    if configured:
        return Path(configured)
    return Path(settings.BASE_DIR) / "var" / "privacy_audit_spool"


def enqueue(record: dict) -> Path:
    """Append one record to the queue, or raise :class:`SpoolUnavailable`.

    Written to a temporary file in the same directory and then renamed, so a
    process that dies mid-write leaves no half-record that the drain would
    read as truth. ``os.replace`` is atomic within a filesystem.
    """
    directory = spool_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        payload = dict(record)
        payload["_queued_at"] = datetime.now(timezone.utc).isoformat()
        fd, tmp_name = tempfile.mkstemp(dir=str(directory), suffix=".partial")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, default=str)
        except Exception:
            os.unlink(tmp_name)
            raise
        final = directory / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}-{uuid.uuid4().hex}{_SUFFIX}"
        os.replace(tmp_name, final)
    except Exception as exc:  # noqa: BLE001 — re-raised under our own name
        logger.critical(
            "privacy_audit.spool_write_failed operation=%s result=%s err=%s — "
            "the access record is LOST, this is the only path on which that "
            "happens",
            record.get("operation"), record.get("result"), exc,
        )
        raise SpoolUnavailable(str(exc)) from exc
    return final


def pending_count() -> int:
    """How many records are waiting. Counts, so that a zero means something."""
    directory = spool_dir()
    if not directory.exists():
        return 0
    return sum(1 for _ in directory.glob(f"*{_SUFFIX}"))


def drain() -> tuple[int, int]:
    """Write every queued record into the journal. Returns (written, failed).

    A record is removed from the queue only after its row exists. A drain that
    dies halfway leaves the rest queued, and re-running it is safe: the queue
    is the source of truth until the row is there.

    Records are replayed oldest-first (the filename carries the queueing
    timestamp), so the journal keeps the order in which accesses happened
    rather than the order in which the filesystem lists them.
    """
    from privacy_audit.models import PersonalDataAccessLog

    directory = spool_dir()
    if not directory.exists():
        return (0, 0)

    written = failed = 0
    for path in sorted(directory.glob(f"*{_SUFFIX}")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a corrupt record must not stop the rest
            logger.error("privacy_audit.spool_unreadable file=%s", path.name)
            failed += 1
            continue
        payload.pop("_queued_at", None)
        actor_id = payload.pop("actor_id", None)
        try:
            PersonalDataAccessLog.objects.create(actor_id=actor_id, **payload)
        except Exception as exc:  # noqa: BLE001 — leave it queued and say why
            logger.error(
                "privacy_audit.spool_replay_failed file=%s err=%s", path.name, exc,
            )
            failed += 1
            continue
        path.unlink(missing_ok=True)
        written += 1
    if written or failed:
        logger.info(
            "privacy_audit.spool_drained written=%s failed=%s remaining=%s",
            written, failed, pending_count(),
        )
    return (written, failed)
