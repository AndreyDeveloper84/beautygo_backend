"""CP-2 / DRF-1617 — durable audit for the internal personal-data surface.

§7 of the Controlled Pilot launch pack asks for ``audit sensitive access``.
Two halves were missing, and they were missing in different ways:

* **Sensitive READ was not audited at all.** ``AMD-010`` has audited the
  deletion since C5.2, but the export only ever reached ``logger.info``.
  A record that lives as long as log rotation answers "who read this
  person's data" for as long as nobody asks late.
* **Refusals had no durable record.** A denied attempt to reach a foreign
  subject is the single event on this surface that a reader will want a
  year later, and it is the one that a rotated log file destroys.

Everything else — a caller with no credential, a misdirected purpose, a
caller that named nobody — stays a log line on purpose. Those are
operational states with a short useful life; writing a row for each would
hand an unauthenticated caller a way to grow our tables.

**Vehicle, and the open question about it.** These use ``AnalyticsEvent``,
the same vehicle ``AMD-010`` already uses for the deletion audit, rather
than importing the bot's ``apps/audit``: the mechanism is already here,
already proven on this exact contract, and does not cross a repository
boundary. Whether ``AnalyticsEvent`` retention satisfies the legal
retention requirement for a 152-ФЗ access log is an OWNER question, open
as of 10.09.2026 (carried by the main window). If the answer is "it needs
its own retention", the vehicle inside these two helpers changes and the
call sites do not — which is why the call sites name an intent
(``emit_personal_data_exported``) and not a table.

**Never written here:** exported values, personal-context values, the
bearer token, the ``X-External-User-ID`` header. The audit records THAT a
subject's data was read and by which initiator, plus the NAMES of the
sections handed over — enough to answer "what was disclosed" from the
contract, without becoming a second copy of the data it is auditing.
"""
from __future__ import annotations

import logging
import uuid

from analytics import event_catalogue

logger = logging.getLogger("users.internal_authz")


def _emit(actor, event_name: str, payload: dict) -> None:
    """Best-effort durable write; never raises.

    Same discipline as ``users.personal_context_events._emit``: an audit
    failure must not turn a correct answer (or a correct refusal) into a
    500. It is logged at WARNING so the loss is itself visible — a silent
    ``except: pass`` here would mean the audit could be empty for a week
    with nothing to show for it.
    """
    try:
        from analytics.models import AnalyticsEvent

        AnalyticsEvent.objects.create(
            actor=actor,
            event_name=event_name,
            payload=payload or {},
            app_type=AnalyticsEvent.AppType.CLIENT,
            client_event_id=uuid.uuid4(),
        )
    except Exception as exc:  # noqa: BLE001 — see docstring
        logger.warning(
            "internal_authz.audit_write_failed event=%s err=%s", event_name, exc,
        )


def emit_personal_data_exported(user, *, sections: list[str], initiator: str) -> None:
    """C5.1 — 152-ФЗ audit of a sensitive READ.

    ``sections`` are the NAMES of what was handed over (e.g.
    ``["profile", "personal_context"]``), never the values. ``created_at``
    on the row is the audit timestamp, exactly as for the AMD-010 deletion
    record, so the two halves of the 152-ФЗ trail read the same way.
    """
    _emit(user, event_catalogue.PERSONAL_DATA_EXPORTED, {
        "user_id": str(user.pk),
        "sections": sections,
        "initiator": initiator,
    })


def emit_subject_access_denied(
    *, reason: str, path: str, subject_id: str | None = None,
) -> None:
    """A caller named itself and named a subject that was not its own.

    ``actor`` on the row is deliberately ``NULL``: the row is about the
    TARGET, not about the caller, and resolving the caller here would mean
    a refused request gets to touch the identity tables. The targeted
    subject id goes in the payload when the route named one.

    The route is recorded, the credential and the external identity are
    not — an audit row that carried the attacker's own header would turn
    the audit into a place to read back probes.
    """
    _emit(None, event_catalogue.INTERNAL_SUBJECT_ACCESS_DENIED, {
        "reason": reason,
        "path": path,
        "subject_id": subject_id,
    })
