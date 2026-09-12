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

There is no queue (owner D1, 12.09.2026): see :mod:`privacy_audit.policy`.
"""
from __future__ import annotations

import logging

from django.db import transaction

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
        # Own savepoint: when the caller holds a transaction (unsafe methods
        # share one with the effect), a failed INSERT here must not abort
        # that transaction for an operation §107 lets proceed — Postgres
        # would otherwise refuse every later statement in it. For the
        # fail-closed operations the caller rolls the whole thing back
        # anyway, so the savepoint costs nothing there.
        with transaction.atomic():
            return PersonalDataAccessLog.objects.create(actor_id=actor_id, **payload)
    except Exception as exc:  # noqa: BLE001 — re-raised as AuditUnavailable
        logger.error(
            "privacy_audit.write_failed operation=%s result=%s subject=%s err=%s",
            operation, result, object_id, exc,
        )
        raise AuditUnavailable(str(exc)) from exc


def record_or_lose(**kwargs) -> str:
    """Record the access, and decide what an unrecordable one means.

    Returns ``"written"`` when the row exists, ``"lost"`` when the operation
    is allowed to proceed without a row, or raises :class:`AuditUnavailable`
    when the operation must not proceed at all.

    The fork is the owner's boundary and lives in :mod:`privacy_audit.policy`:

    * a **disclosing or destroying** operation that cannot be journalled does
      not happen — the exception propagates and the caller refuses it (§96,
      D1 12.09.2026);
    * anything else proceeds (§107: our outage must not stop a person acting
      on their own data) — and, since the owner rejected a disk queue as a
      second sensitive store (D1), the loss is written at ERROR with the
      operation and the subject id. That line is the whole record of the
      access; it is a named blind spot, not a silent one.

    A **denial** proceeds too, whatever the operation: refusing harder is not
    available — the request was already refused — and the ERROR line is what
    stands in for the missing row.
    """
    from privacy_audit import policy

    try:
        record_access(**kwargs)
        return "written"
    except AuditUnavailable:
        allowed = kwargs.get("result") == PersonalDataAccessLog.Result.ALLOWED
        operation = kwargs.get("operation", "")
        if allowed and policy.stops_when_unauditable(operation):
            raise
        logger.error(
            "privacy_audit.record_lost operation=%s result=%s subject=%s actor_named=%s "
            "— the journal is unavailable and this operation is not fail-closed (§107, D1)",
            operation,
            "allowed" if allowed else "denied",
            kwargs.get("object_id"),
            kwargs.get("actor_named"),
        )
        return "lost"


def basis_from(request) -> str:
    """Read the stated basis, or "" when nobody stated one.

    Never substitutes a plausible value. «Человек молчал» and «человек
    выбрал» must stay distinguishable, and a default here would merge them
    permanently — the row is immutable, so a wrong value written today is a
    wrong value forever.
    """
    return (request.META.get(BASIS_HEADER, "") or "").strip()[:128]
