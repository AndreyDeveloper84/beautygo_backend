"""Writing the access journal — and refusing the access when it cannot be written.

Owner ruling §96, the sentence that decides the shape of this module:

    Экспорт считается совершённым только после успешной записи аудита.
    Аудит недоступен — экспорт не выполняем.

So this is the one audit path in the repository that is deliberately NOT
best-effort. Everywhere else (``users.personal_context_events``,
``users.identity_events``) an audit failure is swallowed because losing a
metric is cheaper than failing a feature. Here the reasoning inverts:
inability to record that personal data was disclosed is inability to
disclose it. :class:`AuditUnavailable` therefore propagates, and the caller
turns it into a refusal.

Ordering, and what happens on a failure in the middle
-----------------------------------------------------

There is no "middle" state, and that is by construction rather than by
luck:

* **Reads** (export, personal-context GET): the journal is written after the
  response body has been built and BEFORE it leaves the process. A failed
  write replaces the response with a refusal, so nothing was disclosed.
  Cost of the safe direction: a read that was performed but not delivered
  leaves no row, which is the correct bias — the journal must never claim a
  disclosure that did not reach anyone.
* **Writes and erasures** (personal-data DELETE, personal-context
  PATCH/DELETE): the handler and the journal write share ONE transaction.
  A failed journal write rolls the effect back. There is no state in which
  data was erased without a record of the erasure, which is the failure this
  ordering exists to prevent.

Denials are recorded on the same path and cannot fail closed any harder than
they already are: the request is being refused either way. A denial whose
journal write fails is still refused, and the failure is logged at ERROR —
because a silently unrecorded refusal is exactly the blindness §96 is about.
"""
from __future__ import annotations

import logging

from privacy_audit.models import PersonalDataAccessLog

logger = logging.getLogger("privacy_audit")

# Header a caller uses to state «основание / номер обращения». Nothing sends
# it as of 10.09.2026 — see the model docstring. Named here so the day a
# caller does, there is one spelling and not three.
BASIS_HEADER = "HTTP_X_ACCESS_BASIS"


class AuditUnavailable(RuntimeError):
    """The access journal could not be written.

    Raised, never swallowed. The caller's job is to turn this into a refusal
    — not into a warning next to a successful response.
    """


def build_payload(
    *,
    caller_purpose: str,
    actor,
    object_id,
    operation: str,
    object_category: str,
    result: str,
    actor_named: bool,
    denial_reason: str = "",
    basis: str = "",
    request_id: str = "",
) -> dict:
    """The record, as plain data — one shape for the table and for the queue.

    Built once so a queued record and a directly-written one cannot drift
    apart. A queue that stores a different shape than the table replays into
    a journal that disagrees with itself, and nobody notices until somebody
    reads a year-old row.

    ``actor_role`` is derived here rather than passed, so "a service with no
    human behind it" is spelled the same way at every call site: ``service``.
    An empty role and a service call are different facts, and one blank would
    merge them.

    The caller-supplied external identity is deliberately NOT included. It is
    unbounded caller-controlled text, and a record that echoed it would let
    whoever is probing write strings into the very file investigators read.
    """
    return {
        "caller_purpose": caller_purpose,
        "actor_id": getattr(actor, "pk", None),
        "actor_role": (
            (getattr(actor, "role", "") or "") if actor is not None else "service"
        ),
        # Global client subject — see the model's tenant field comment.
        "tenant_id": None,
        "operation": operation,
        "object_category": object_category,
        "object_id": object_id,
        "result": result,
        "denial_reason": denial_reason,
        "actor_named": actor_named,
        "basis": basis or "",
        "request_id": request_id or "",
    }


def record_access(
    *,
    caller_purpose: str,
    actor,
    object_id,
    operation: str,
    object_category: str,
    result: str,
    actor_named: bool,
    denial_reason: str = "",
    basis: str = "",
    request_id: str = "",
) -> PersonalDataAccessLog:
    """Append one row, or raise :class:`AuditUnavailable`.

    ``actor`` is the resolved ``User`` or ``None``. ``actor_role`` is derived
    here rather than passed, so "a service with no human behind it" is spelled
    the same way at every call site: ``service``. An empty role and a service
    call are different facts, and a single blank would merge them.

    The caller-supplied external identity is deliberately NOT stored. It is
    unbounded caller-controlled text, and a journal that echoed it would let
    whoever is probing write strings into the record that the people reading
    that record will later read. The resolved ``actor`` FK answers the same
    question with a value the system chose.
    """
    payload = build_payload(
        caller_purpose=caller_purpose, actor=actor, object_id=object_id,
        operation=operation, object_category=object_category, result=result,
        actor_named=actor_named, denial_reason=denial_reason, basis=basis,
        request_id=request_id,
    )
    actor_id = payload.pop("actor_id")
    try:
        return PersonalDataAccessLog.objects.create(actor_id=actor_id, **payload)
    except Exception as exc:  # noqa: BLE001 — re-raised as AuditUnavailable
        logger.error(
            "privacy_audit.write_failed operation=%s result=%s subject=%s err=%s",
            operation, result, object_id, exc,
        )
        raise AuditUnavailable(str(exc)) from exc


def record_or_queue(**kwargs) -> str:
    """Record the access, and decide what an unrecordable one means.

    Returns how the record was stored: ``"written"`` (the row exists),
    ``"queued"`` (it is in the guaranteed queue and will), or raises
    :class:`AuditUnavailable` when the operation must not proceed at all.

    The fork is the owner's boundary, and it lives in
    :mod:`privacy_audit.policy` rather than here so that "which operations
    stop" is one readable set instead of a condition buried in a handler:

    * a **disclosing or destroying** operation that cannot be journalled does
      not happen — the exception propagates and the caller refuses it;
    * anything else is queued, because our outage must not stop a person
      acting on their own data.

    A **denial** is queued too, whatever the operation. That is not in the
    ruling and it follows from it: an attempt to reach somebody else's subject
    has to arrive in the journal even if the journal was down when it
    happened, and the queue is exactly the mechanism that makes "eventually"
    honest. Refusing harder is not available — the request was already
    refused.
    """
    from privacy_audit import policy, spool

    try:
        record_access(**kwargs)
        return "written"
    except AuditUnavailable:
        allowed = kwargs.get("result") == PersonalDataAccessLog.Result.ALLOWED
        if allowed and policy.stops_when_unauditable(kwargs.get("operation", "")):
            raise
        try:
            spool.enqueue(build_payload(**kwargs))
        except spool.SpoolUnavailable:
            # Both the table and the queue are gone. The record is lost; the
            # spool has already logged it at CRITICAL. For a queued operation
            # the owner's ruling says the product does not stop, so we do not
            # convert our outage into the person's failure.
            return "lost"
        return "queued"


def basis_from(request) -> str:
    """Read the stated basis, or "" when nobody stated one.

    Never substitutes a plausible value. «Человек молчал» and «человек
    выбрал» must stay distinguishable, and a default here would merge them
    permanently — the row is immutable, so a wrong value written today is a
    wrong value forever.
    """
    return (request.META.get(BASIS_HEADER, "") or "").strip()[:128]
