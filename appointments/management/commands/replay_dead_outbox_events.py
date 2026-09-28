"""C5 — Operator replay command for dead-lettered ``OutboxEvent`` rows.

Closes the C2/C4 retry loop: once the HTTP publisher has exhausted
the retry budget for a row (8 attempts at exponential backoff, ~4.5h
total), the row transitions to ``bot_delivery_status='dead'`` and
stops being re-attempted. The publisher will not pick it up again
without operator action — that's the safety floor that prevents a
zombie row from generating thousands of failed POSTs per day.

This command is that operator action. It resets the per-row
delivery state so the publisher picks the row up on its next tick:

* ``bot_delivery_status`` → ``'pending'``
* ``bot_dead_lettered_at`` → ``NULL``
* ``bot_attempt_count`` → ``0``
* ``bot_next_retry_at`` → ``NULL``
* ``bot_last_error`` is preserved so the post-replay log still shows
  the reason the row hit DLQ originally.

Selection is always opt-in: by default, no rows match, the operator
must explicitly pass ``--tenant`` / ``--since`` / ``--topic`` (or
``--all``) to target a working set. ``--dry-run`` lists the matched
rows without any mutation. Without ``--dry-run``, the reset runs inside
a single transaction so a SIGINT mid-loop leaves the DB in the
pre-command state.

### ``--tenant`` / ``--since`` require ``--topic`` (DRF-2525)

A salon's DLQ holds more than one topic, and topics are not equally
safe to replay. 25.09 the six dead ``booking.completed`` of one salon
sat next to three dead ``booking.created`` of the same salon; replaying
the latter creates a bot proxy and tells the client «вы записаны» about
a month-old booking — irreversible and visible to the client. Without a
topic filter ``--tenant`` would have taken all nine. So a widening
selector is refused unless the operator names the topic(s); the refusal
fires before any read-out, dry-run included, so the previewed count is
always the count that will run. ``--event-id`` is already surgical and
needs no topic; ``--all`` stays the one explicit «everything» switch.

Use case (from the pilot runbook): ops detects bot-platform ingest
broken transiently — recovers it, then runs::

    python manage.py replay_dead_outbox_events \
        --since "2026-06-01T08:00:00Z" --topic booking.completed --dry-run

inspects the matched count, then re-runs without ``--dry-run`` to
let the publisher pick the rows up.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime

from appointments.models import OutboxEvent


class Command(BaseCommand):
    help = (
        "Reset dead-lettered OutboxEvent rows so the publisher re-attempts "
        "delivery. Filter by tenant / since / event-id; --dry-run lists "
        "matches without mutation."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--tenant",
            dest="tenant",
            default=None,
            help=(
                "Only replay rows whose envelope payload.tenant_id matches "
                "this UUID. Most common pilot-day filter when a single "
                "tenant's webhook integration broke."
            ),
        )
        parser.add_argument(
            "--since",
            dest="since",
            default=None,
            help=(
                "Only replay rows dead-lettered at or after this UTC "
                "ISO-8601 datetime (e.g. 2026-06-01T08:00:00Z). Common "
                "filter when ops can name the start of the outage window."
            ),
        )
        parser.add_argument(
            "--event-id",
            dest="event_id",
            default=None,
            help=(
                "Replay exactly one row by OutboxEvent.id. Bypasses the "
                "--all gate so a single named row can be retried surgically."
            ),
        )
        parser.add_argument(
            "--topic",
            dest="topics",
            action="append",
            choices=OutboxEvent.Topic.values,
            default=None,
            help=(
                "Only replay rows of this topic (repeatable). REQUIRED with "
                "--tenant / --since: a salon's DLQ mixes topics, and "
                "replaying booking.created re-announces old bookings to "
                "clients (DRF-2525)."
            ),
        )
        parser.add_argument(
            "--all",
            dest="all_dead",
            action="store_true",
            default=False,
            help=(
                "Replay ALL dead-lettered rows. Requires no other filters "
                "to be set — explicit opt-in so a forgotten --tenant does "
                "not silently widen the scope."
            ),
        )
        parser.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            default=False,
            help=(
                "Print matched rows without mutating anything. The default "
                "for any first invocation; flip off only when the operator "
                "is confident in the count."
            ),
        )

    def handle(self, *_args: Any, **options: Any) -> None:
        qs = self._build_queryset(options)
        matched = qs.count()
        self.stdout.write(f"Matched dead-lettered rows: {matched}")
        if matched == 0:
            self.stdout.write(self.style.NOTICE("Nothing to replay."))
            return

        # Preview first 20 rows so the operator can sanity-check the
        # filter narrowed to expected scope before flipping --dry-run.
        preview = qs.order_by("created_at")[:20]
        for row in preview:
            self.stdout.write(
                f"  {row.id}  topic={row.topic}  "
                f"attempts={row.bot_attempt_count}  "
                f"dead_at={row.bot_dead_lettered_at}  "
                f"last_error={row.bot_last_error[:80] if row.bot_last_error else ''}"
            )
        if matched > 20:
            self.stdout.write(f"  ... and {matched - 20} more")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(
                "Dry-run: no rows mutated. Re-run without --dry-run to reset.",
            ))
            return

        with transaction.atomic():
            updated = qs.update(
                bot_delivery_status=OutboxEvent.BotDeliveryStatus.PENDING,
                bot_dead_lettered_at=None,
                bot_attempt_count=0,
                bot_next_retry_at=None,
                bot_response_status=None,
                # bot_last_error preserved — gives the post-replay audit
                # a stable reason-trail without losing context.
            )
        self.stdout.write(self.style.SUCCESS(
            f"Reset {updated} row(s). Publisher will pick them up on next tick.",
        ))

    # ----------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------

    def _build_queryset(self, options: dict[str, Any]):
        """Construct the row selector from CLI options.

        Always scopes to ``bot_dead_lettered_at IS NOT NULL`` and
        ``bot_delivery_status='dead'`` — the command refuses to touch
        non-DLQ rows. A future ``--include-failed`` flag could widen
        the scope but is out of scope here (deliberately narrow MVP).
        """
        tenant = options.get("tenant")
        since_raw = options.get("since")
        event_id = options.get("event_id")
        all_dead = options.get("all_dead")
        topics = options.get("topics") or []

        # Validate scope: at least one selector is required.
        named_filters = [tenant, since_raw, event_id, topics]
        any_named = any(named_filters)
        if not any_named and not all_dead:
            raise CommandError(
                "No selector given. Pass at least one of --tenant, --since, "
                "--topic, --event-id, or --all to scope the replay set."
            )
        if all_dead and any_named:
            raise CommandError(
                "--all cannot be combined with --tenant/--since/--topic/--event-id. "
                "Either replay everything or replay a narrowed set."
            )
        # DRF-2525: a widening selector without a topic takes every topic
        # of the salon/window — booking.created included. Refuse before
        # any read-out so the dry-run count is the count that will run.
        if (tenant or since_raw) and not topics:
            raise CommandError(
                "--tenant/--since need --topic: a DLQ mixes topics, and "
                "replaying booking.created re-announces old bookings to "
                "clients. Name the topic(s), e.g. --topic booking.completed, "
                "or replay rows one by one with --event-id."
            )

        qs = OutboxEvent.objects.filter(
            bot_delivery_status=OutboxEvent.BotDeliveryStatus.DEAD,
            bot_dead_lettered_at__isnull=False,
        )
        if topics:
            qs = qs.filter(topic__in=topics)

        if event_id:
            return qs.filter(id=event_id)

        if tenant:
            # tenant_id lives inside the envelope payload as a string
            # UUID. Use JSONB path lookup so the filter pushes down
            # to PostgreSQL.
            qs = qs.filter(payload__tenant_id=tenant)

        if since_raw:
            since_dt = parse_datetime(since_raw)
            if since_dt is None or not isinstance(since_dt, datetime):
                raise CommandError(
                    f"--since must be ISO-8601 (e.g. "
                    f"2026-06-01T08:00:00Z); got {since_raw!r}."
                )
            qs = qs.filter(bot_dead_lettered_at__gte=since_dt)

        return qs
