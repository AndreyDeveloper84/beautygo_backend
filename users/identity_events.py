"""Durable audit for the cross-repo identity binding (E2E-BOT-02B).

``users.services.bind_external_identity`` / ``unlink_external_identity``
write their audit trail as ``AnalyticsEvent`` rows, the same
durable-audit vehicle AMD-010 uses for personal-data deletion
(``users/personal_context_events``).

Two delivery guarantees, by outcome:

- **Mutating outcomes** (``created``, ``idempotent`` re-bind,
  ``unlinked`` with an actual pointer cleared) are emitted with
  ``strict=True`` from INSIDE the identity-mutation transaction: the
  audit row commits or rolls back together with the binding. An
  audit-sink failure therefore fails — and rolls back — the identity
  operation itself. No committed binding without a committed audit row.
  This covers RE-links: a re-bind after an unlink (or a re-unlink after
  a re-bind) is a NEW operation, not a retry, and always gets its own
  row — see the generation discriminator below.
- **Non-mutating outcomes** (``conflict``, ``rejected``, no-op
  ``already_unbound``) are emitted best-effort after the fact: there
  is no mutation to keep consistent, so a lost audit row is acceptable
  (logged at WARNING). Repeats of the SAME rejection are deduped
  app-level, so a looping caller cannot flood the table.

Payload contract (safe-by-design):

- ``operation`` — ``bind_external_identity`` | ``unlink_external_identity``;
- ``external_user_id`` / ``proxy_user_id`` / ``target_user_id`` — id
  references, never personal values;
- ``result`` — ``created`` | ``idempotent`` | ``conflict`` |
  ``rejected`` | ``unlinked``;
- ``reason`` — coarse, safe category (no free-form error text);
- ``initiator`` — which trusted caller ran the operation
  (``identity_provisioning`` for the s2s endpoint,
  ``e2e_fixture_bootstrap`` for the provisioning command);
- ``request_id`` — correlation id stamped by RequestIDMiddleware when
  the call came over HTTP.

NEVER write secrets here: no bearer tokens, no Authorization header,
no raw request bodies.
"""
from __future__ import annotations

import logging
import uuid

from django.db import IntegrityError, transaction

from analytics import event_catalogue

logger = logging.getLogger("users.identity.events")

#: Coarse, safe reason categories — no free-form error text in audit.
REASON_BOUND = "bound"
REASON_ALREADY_BOUND_SAME = "already_bound_same_target"
REASON_ALREADY_BOUND_OTHER = "already_bound_different_target"
REASON_TARGET_NOT_BINDABLE = "target_not_bindable"
REASON_INVALID_EXTERNAL_ID = "invalid_external_user_id"
REASON_EXTERNAL_ID_COLLISION = "external_id_collision"
REASON_SUPPORT_UNLINK = "support_unlink"
REASON_ALREADY_UNBOUND = "already_unbound"
REASON_EXTERNAL_IDENTITY_UNKNOWN = "external_identity_unknown"


#: Mutating results whose dedup key carries a GENERATION discriminator.
#: A genuinely new operation after an opposite-direction one (re-bind
#: after unlink, re-unlink after re-bind) must get a FRESH audit row —
#: collapsing it into the previous generation's key would silently drop
#: the audit of a committed mutation, breaking the "no committed
#: binding without a committed audit row" guarantee exactly on the
#: managed relink path that audit exists for (AYLA-DEC-0016 §4).
_OPPOSITE_RESULT = {
    "created": "unlinked",
    "idempotent": "unlinked",
    "unlinked": "created",
}


def _generation(event_model, *, external_user_id, target_user_id, result):
    """Generation discriminator for the dedup key.

    The generation is the number of COMMITTED opposite-direction audit
    rows for the same ``(external_user_id, target_user_id)`` pair:

    - a pure RETRY of an operation sees an unchanged generation and
      dedups against its first attempt's row, as before;
    - a re-bind after an unlink (or a re-unlink after a re-bind) sees
      the generation bumped by the intervening opposite operation and
      gets a fresh key — hence a fresh audit row.

    Non-mutating results (and calls without a resolved target) use
    generation 0. Mutating outcomes are only ever emitted from inside
    the identity transaction while holding the proxy row lock
    (``bind_external_identity`` / ``unlink_external_identity``), so the
    count is stable against concurrent bind/unlink on the same
    identity.
    """
    opposite = _OPPOSITE_RESULT.get(result)
    if opposite is None or target_user_id is None:
        return 0
    return event_model.objects.filter(
        event_name=event_catalogue.EXTERNAL_IDENTITY_BOUND,
        payload__external_user_id=external_user_id,
        payload__target_user_id=str(target_user_id),
        payload__result=opposite,
    ).count()


def emit_identity_binding(
    *,
    actor,
    external_user_id: str,
    proxy_user_id=None,
    target_user_id=None,
    result: str,
    reason: str,
    initiator: str,
    request_id: str | None = None,
    operation: str = "bind_external_identity",
    strict: bool = False,
) -> None:
    """Write the durable audit row for one binding operation.

    ``actor`` is the target User when one exists (per-user audit
    queries stay possible via the ``(actor, -created_at)`` index);
    ``None`` when the target never resolved. ``created_at`` on the row
    is the audit timestamp.

    ``strict=True`` re-raises write failures: callers use it ONLY for
    mutating outcomes, from inside the mutation's transaction, so a
    failed audit rolls the identity change back. ``strict=False``
    (default) swallows + logs — for outcomes where nothing mutated.

    ``client_event_id`` is DETERMINISTIC (uuid5 over operation /
    external id / target / result / generation): a retry of the same
    logical operation hits the ``(actor, client_event_id)`` dedup
    constraint instead of writing a second audit row, while a NEW
    operation after an opposite-direction one (re-bind after unlink)
    gets a fresh key via the generation discriminator and always
    writes its own row. The duplicate is absorbed via a savepoint so
    it never poisons the caller's transaction.

    An ``IntegrityError`` from the sink is swallowed ONLY when the row
    with our deterministic key verifiably exists (a genuine replay);
    any other ``IntegrityError`` (FK on a deleted actor, a future NOT
    NULL, …) is a real sink failure — re-raised in strict mode so the
    identity mutation rolls back, logged otherwise.

    NULL-actor rows (rejections whose target never resolved) fall
    outside BOTH partial dedup constraints (the actor and the
    anonymous-session namespaces), so best-effort NULL-actor emissions
    dedup app-level: the same rejection repeated by a looping caller
    writes one row, not an unbounded stream.
    """
    from analytics.models import AnalyticsEvent

    generation = _generation(
        AnalyticsEvent,
        external_user_id=external_user_id,
        target_user_id=target_user_id,
        result=result,
    )
    dedup_key = uuid.uuid5(
        uuid.NAMESPACE_URL,
        "ayla:identity:{}:{}:{}:{}:{}".format(
            operation, external_user_id, target_user_id, result, generation,
        ),
    )
    if actor is None and not strict:
        # App-level dedup for the NULL-actor namespace (no DB
        # constraint covers it). Best-effort: a rare concurrent
        # duplicate row is acceptable, an unbounded stream is not.
        if AnalyticsEvent.objects.filter(
            client_event_id=dedup_key,
        ).exists():
            return
    try:
        # Savepoint: a duplicate-key IntegrityError must not break an
        # enclosing transaction (strict callers run inside one).
        with transaction.atomic():
            AnalyticsEvent.objects.create(
                actor=actor,
                event_name=event_catalogue.EXTERNAL_IDENTITY_BOUND,
                payload={
                    "operation": operation,
                    "external_user_id": external_user_id,
                    "proxy_user_id": (
                        str(proxy_user_id) if proxy_user_id else None
                    ),
                    "target_user_id": (
                        str(target_user_id) if target_user_id else None
                    ),
                    "result": result,
                    "reason": reason,
                    "initiator": initiator,
                    "request_id": request_id,
                },
                app_type=AnalyticsEvent.AppType.CLIENT,
                client_event_id=dedup_key,
            )
    except IntegrityError as exc:
        # The savepoint rolled back, so the enclosing transaction (if
        # any) is usable again. A row with OUR deterministic key means
        # a genuine idempotent replay — the audit row is already there,
        # which is exactly the state strict mode guarantees. Any OTHER
        # IntegrityError is a real sink failure and must NOT be
        # swallowed, or a binding could commit without its audit row.
        if AnalyticsEvent.objects.filter(
            actor=actor, client_event_id=dedup_key,
        ).exists():
            if strict:
                # A strict replay means the mutation commits WITHOUT a
                # fresh audit row: the row with our deterministic key is
                # already there, so the strict guarantee (no committed
                # binding without a committed audit row) still holds —
                # but ONLY as long as audit history is intact. If the
                # generation counter collapsed onto an old key because
                # opposite-direction rows were lost (retention,
                # anonymisation), this replay is silently reusing a
                # previous generation's row. Never swallow that mute.
                logger.warning(
                    "identity.binding_audit_replay_absorbed"
                    " external_user_id=%s result=%s key=%s",
                    external_user_id, result, dedup_key,
                )
            return
        if strict:
            raise
        logger.warning(
            "identity.binding_audit_failed external_user_id=%s result=%s err=%s",
            external_user_id, result, exc,
        )
    except Exception as exc:
        if strict:
            raise
        logger.warning(
            "identity.binding_audit_failed external_user_id=%s result=%s err=%s",
            external_user_id, result, exc,
        )
