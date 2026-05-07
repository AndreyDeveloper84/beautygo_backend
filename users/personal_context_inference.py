"""DRF-230 PR 2 — UserPersonalContext inference from booking history.

Pure-Python service used by the Celery task ``infer_user_patterns``.
Kept Django-aware (model imports, querysets) but free of Celery
plumbing so unit tests don't need a worker.

Two inferences land here today; more follow as we learn what the
maxbot side actually values:

1. ``favorite_masters`` — specialists the user has booked at least
   ``FAVORITE_MIN_COMPLETED`` (default 3) completed appointments with.
2. ``busy_days`` — ISO weekday short names (mon..sun) the user has
   essentially never booked, given a long-enough booking history. We
   only mark a day busy when (a) the user has at least
   ``BUSY_DAYS_MIN_HISTORY`` bookings total and (b) zero of those fall
   on the weekday in question.

Both write under ``data_sources["<field>"] = "inferred"`` and refuse
to overwrite a value the user explicitly typed
(``data_sources["<field>"] == "explicit"``). This keeps user intent
sticky across nightly runs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from django.db.models import Count, Q

from appointments.models import Appointment
from users.models import UserPersonalContext


logger = logging.getLogger("users.personal_context.inference")


# Tunables — surface as Django settings later if we need per-env tweaks.
FAVORITE_MIN_COMPLETED = 3
BUSY_DAYS_MIN_HISTORY = 8

ISO_WEEKDAY_SHORT = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class InferenceOutcome:
    """Per-user infer result, used by tests + the orchestration task."""

    user_id: str
    favorite_masters_added: list[str]
    busy_days_added: list[str]
    skipped_explicit: list[str]


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _user_owns_explicit(ctx: UserPersonalContext, field: str) -> bool:
    """Did the user type this themselves? Then nightly inference shuts up."""
    sources = ctx.data_sources or {}
    return sources.get(field) == "explicit"


def _stamp_inferred(ctx: UserPersonalContext, field: str) -> None:
    """Record provenance + last-touched timestamp for a field."""
    sources = dict(ctx.data_sources or {})
    sources[field] = "inferred"
    ctx.data_sources = sources


def _completed_appointments(user) -> "Appointment.objects":
    return Appointment.objects.filter(
        client=user,
        status=Appointment.Status.COMPLETED,
    )


# ----------------------------------------------------------------------------
# Inference passes
# ----------------------------------------------------------------------------


def _infer_favorite_masters(ctx: UserPersonalContext) -> list[str]:
    """Specialists the user rebooked >= ``FAVORITE_MIN_COMPLETED`` times.

    Replaces ``ctx.favorite_masters`` with the inferred list (sorted by
    booking count descending so the engine can read the top-N quickly).
    Returns the list of specialist UUIDs added.
    """
    if _user_owns_explicit(ctx, "favorite_masters"):
        return []

    rows = (
        _completed_appointments(ctx.user)
        .values("specialist_id")
        .annotate(total=Count("id"))
        .filter(total__gte=FAVORITE_MIN_COMPLETED)
        .order_by("-total", "specialist_id")
    )
    inferred = [str(row["specialist_id"]) for row in rows]
    ctx.favorite_masters = inferred
    _stamp_inferred(ctx, "favorite_masters")
    return inferred


def _infer_busy_days(ctx: UserPersonalContext) -> list[str]:
    """Weekdays the user almost never books.

    Heuristic: with at least ``BUSY_DAYS_MIN_HISTORY`` total bookings,
    any weekday with zero bookings is "busy". Less than the minimum
    history → leave the field alone (sample too small).

    Looks at all non-cancelled bookings (any status that holds a slot
    or completed) so user signals are captured early — we don't wait
    for the appointment to complete to learn "she avoids Mondays".
    """
    if _user_owns_explicit(ctx, "busy_days"):
        return []

    qs = Appointment.objects.filter(
        client=ctx.user,
    ).exclude(
        # Drop cancelled/no-show — those don't reflect intent.
        status__in=[
            Appointment.Status.CANCELLED,
            Appointment.Status.NO_SHOW,
        ],
    )
    total = qs.count()
    if total < BUSY_DAYS_MIN_HISTORY:
        return []

    booked_weekdays: set[int] = set()
    for ts in qs.values_list("start_datetime", flat=True):
        if ts is None:
            continue
        # Python weekday(): Monday=0..Sunday=6 — matches our short list.
        booked_weekdays.add(ts.weekday())

    inferred = [
        ISO_WEEKDAY_SHORT[i]
        for i in range(7)
        if i not in booked_weekdays
    ]
    ctx.busy_days = inferred
    _stamp_inferred(ctx, "busy_days")
    return inferred


# ----------------------------------------------------------------------------
# Public entrypoint
# ----------------------------------------------------------------------------


def infer_for_user(user) -> InferenceOutcome:
    """Run all inferences for a single user. Idempotent.

    Lazy-creates the context row if missing — keeps the task safe to
    schedule for users who never opened personal-context endpoints.
    """
    ctx, _ = UserPersonalContext.objects.get_or_create(user=user)
    skipped: list[str] = []

    if _user_owns_explicit(ctx, "favorite_masters"):
        skipped.append("favorite_masters")
        favs: list[str] = []
    else:
        favs = _infer_favorite_masters(ctx)

    if _user_owns_explicit(ctx, "busy_days"):
        skipped.append("busy_days")
        busy: list[str] = []
    else:
        busy = _infer_busy_days(ctx)

    ctx.save(update_fields=[
        "favorite_masters", "busy_days", "data_sources", "updated_at",
    ])

    logger.info(
        "personal_context.inferred user=%s favs=%d busy=%d skipped=%s",
        user.pk, len(favs), len(busy), ",".join(skipped) or "-",
    )
    return InferenceOutcome(
        user_id=str(user.pk),
        favorite_masters_added=favs,
        busy_days_added=busy,
        skipped_explicit=skipped,
    )


def infer_for_active_users(*, since=None) -> dict[str, int]:
    """Run inference for every user with at least one Appointment.

    The ``since`` arg is a freshness gate — if a user's context was
    last updated after this timestamp, we skip them (cheap opt-out
    when running many times per day or backfilling). Returns a dict of
    counters for the orchestration task to log.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()
    users = (
        User.objects
        .filter(role="client")
        .filter(Q(appointments_as_client__isnull=False))
        .distinct()
    )
    if since is not None:
        # Skip rows recently updated; missing context counts as old.
        users = users.exclude(
            personal_context__updated_at__gt=since,
        )

    processed = 0
    failures = 0
    favs_total = 0
    busy_total = 0
    for user in users.iterator():
        try:
            outcome = infer_for_user(user)
        except Exception as exc:  # pragma: no cover - belt + suspenders
            logger.exception(
                "personal_context.inference_failed user=%s err=%s",
                user.pk, exc,
            )
            failures += 1
            continue
        processed += 1
        favs_total += len(outcome.favorite_masters_added)
        busy_total += len(outcome.busy_days_added)

    return {
        "processed_users": processed,
        "failed_users": failures,
        "favorite_masters_inferred": favs_total,
        "busy_days_inferred": busy_total,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
