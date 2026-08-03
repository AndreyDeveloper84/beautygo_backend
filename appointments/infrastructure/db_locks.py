"""Postgres advisory locks — closes the phantom-insert collision race.

``select_for_update()`` on the conflict-check queryset only locks rows
that already overlap the target interval. When the target slot is
genuinely open (no existing overlapping row), the lock set is empty and
two concurrent create/reschedule attempts for the same specialist can
both pass the conflict check and both write overlapping appointments —
there is nothing to serialise them against.

A transaction-scoped advisory lock keyed by specialist_id closes this:
every create/reschedule attempt for a given specialist takes the same
lock as the *first* statement inside its atomic block, so the second
attempt blocks until the first commits or rolls back — by which point
its own conflict check sees the first attempt's write (if any).
"""
from __future__ import annotations

from uuid import UUID


def specialist_advisory_lock(specialist_id: UUID) -> None:
    """Serialise create/reschedule attempts for one specialist.

    No-op on non-Postgres backends (SQLite unit tests) — those don't
    support advisory locks and don't exercise real concurrency anyway;
    ``select_for_update()`` is already a no-op there too (CLAUDE.md gate:
    concurrency behaviour is only meaningful under Postgres).
    """
    from django.db import connection

    if connection.vendor != 'postgresql':
        return

    with connection.cursor() as cursor:
        # hashtextextended(text, seed) -> bigint, the exact signature
        # pg_advisory_xact_lock(bigint) wants. Transaction-scoped: the
        # lock releases automatically on commit or rollback, no explicit
        # unlock needed (and none is possible mid-transaction anyway).
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [str(specialist_id)],
        )
