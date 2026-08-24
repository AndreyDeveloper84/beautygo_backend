"""DRF-1366/1367/1368 — the one erasure verb for ``UserPersonalContext``.

Ayla owns the declared preferences (owner ruling 2026-08-24,
``Ayla/docs/OD_MEMORY.md`` §1). Until this module landed the owning side had
no operation for "erase what you own": callers either named fields one by
one (the bot bridge, three of twelve) or dropped the row (the app wipe and
the C5.2 cascade) — and a dropped row is not a terminal state, because
``users.infer_user_patterns`` lazy-creates it again the same night and
refills ``favorite_masters`` / ``busy_days`` from booking history.

Two shapes, one verb:

- **Live account** → the row stays as a *tombstone*: every declared field
  back at its model default, and ``data_sources[field] = "erased"`` for all
  of them. The tombstone is the terminal state — nightly inference already
  refuses to write a field the subject owns, and ``"erased"`` joins
  ``"explicit"`` in that set. No new mechanism, no new column.
- **Soft-deleted account** (``user.deleted_at`` set) → nothing is left
  behind at all. The ``User`` row is its own tombstone: inference skips
  deleted accounts outright, so there is no row to protect.

Why not a tombstone table: the row we already have carries provenance, and
provenance is exactly the mechanism that decides whether inference may
write. Extending its vocabulary by one word keeps a single source of that
decision. It also costs no migration — and migrations on the live pilot are
not free.

Why the field list is derived from the model and never written down: the
list *is* the defect DRF-1367 describes. ``_CLEARABLE_FIELDS`` on the bridge
named three of twelve and drifted the moment field four landed. A new
personal field on ``UserPersonalContext`` is erased by this verb the day it
is added, with no edit here. The failure direction is deliberate: a new
*service* field forgotten in ``SERVICE_FIELDS`` gets erased too — erasing
too much beats quietly keeping something the subject asked us to forget.
"""
from __future__ import annotations

import logging

from users.models import UserPersonalContext
from users.personal_context_events import emit_personal_data_deleted


logger = logging.getLogger("users.personal_context.erasure")


#: Provenance marker written for every declared field by an erasure.
#: Read by ``personal_context_inference`` (never overwrite) and by
#: ``personalization_engine`` rule 5 (never re-ask).
ERASED = "erased"

#: Bookkeeping the personalization engine owns — not declared preferences.
#: ``data_sources`` is rewritten by the erasure (it becomes the tombstone);
#: the other two are behavioural records about the person and are cleared.
SERVICE_FIELDS = frozenset({
    "skipped_questions",
    "last_asked_at",
    "data_sources",
})


def declared_fields() -> tuple[str, ...]:
    """Every field of the context that holds something about the person.

    Derived from the model, in declaration order. Excludes the primary key,
    the owner FK, the auto timestamps (all non-editable or structural) and
    the engine's bookkeeping.
    """
    return tuple(
        field.name
        for field in UserPersonalContext._meta.concrete_fields
        if field.editable
        and field.name != "user"
        and field.name not in SERVICE_FIELDS
    )


def default_for(field_name: str):
    """The model's own default for a declared field.

    ``[]`` for the JSON lists, ``""`` for the char fields, ``False`` for the
    boolean, ``None`` for the nullable decimal/float columns — read off the
    field, not off a hand-kept table that can disagree with the column.
    """
    return UserPersonalContext._meta.get_field(field_name).get_default()


def _holds_anything(ctx: UserPersonalContext) -> bool:
    """Did the row actually carry a declared value worth reporting as erased?

    A tombstone (all defaults) holds nothing, so a repeat erasure honestly
    reports an empty scope — the idempotency contract C5.2 documents.
    """
    return any(
        getattr(ctx, name) != default_for(name)
        for name in declared_fields()
    )


def _tombstone(ctx: UserPersonalContext) -> None:
    """Reset every declared field and mark the whole row erased."""
    for name in declared_fields():
        setattr(ctx, name, default_for(name))
    ctx.data_sources = {name: ERASED for name in declared_fields()}
    ctx.skipped_questions = {}
    ctx.last_asked_at = {}
    ctx.save()


def erase_personal_context(user, *, initiator: str) -> list[str]:
    """Erase everything Ayla holds about ``user`` in the personal context.

    Idempotent. Returns the audit scope — ``["personal_context"]`` when a
    declared value was actually removed, ``[]`` when there was nothing left
    to remove. Every call is audited via AMD-010 (``AnalyticsEvent``),
    repeats included, and never with the erased values.

    ``initiator`` names the caller for the audit trail: ``"app"`` (the
    authenticated 152-ФЗ wipe), ``"internal_api"`` (the bot's C5.2 account
    cascade), ``"bot_forget_all"`` (the memory erase verb), or
    ``"account_delete"`` (mobile "удалить аккаунт").
    """
    ctx = UserPersonalContext.objects.filter(user=user).first()
    had_data = ctx is not None and _holds_anything(ctx)
    account_gone = getattr(user, "deleted_at", None) is not None

    if account_gone:
        # Nothing is left behind for a deleted account: no row, no
        # tombstone. Inference refuses deleted users, so the guard the
        # tombstone would provide is already there — and a row about a
        # person who asked to be gone is exactly what must not survive.
        if ctx is not None:
            ctx.delete()
    else:
        if ctx is None:
            # No row yet, and the subject just asked us to forget. Create
            # the tombstone anyway: without it tonight's inference is free
            # to invent favourite masters for someone who erased.
            ctx = UserPersonalContext(user=user)
        _tombstone(ctx)

    scope = ["personal_context"] if had_data else []
    emit_personal_data_deleted(user, scope=scope, initiator=initiator)
    logger.info(
        "personal_context.erased user=%s scope=%s initiator=%s account_gone=%s",
        user.pk, scope, initiator, account_gone,
    )
    return scope


def mark_field_erased(ctx: UserPersonalContext, field_name: str) -> None:
    """Tombstone a single field the subject reset from the app.

    The whole-profile verb above is the 152-ФЗ "forget everything"; this is
    "forget my favourite masters". Same terminal guarantee, one field wide —
    without it, per-field reset of an inferred field is undone overnight.
    """
    sources = dict(ctx.data_sources or {})
    sources[field_name] = ERASED
    ctx.data_sources = sources
