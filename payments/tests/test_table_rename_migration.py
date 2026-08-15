"""Migration test for #492 — `appointments_payment → payments_payment`.

Forward + reverse paths exercised via MigrationExecutor. The test
runs against the same Postgres CI uses; SQLite would not catch a
botched `AlterModelTable` because it doesn't track table renames in
the same way (Postgres `ALTER TABLE … RENAME TO …` is the SQL we
actually emit).

Strategy A from the original Plan-subagent dispatch (#426): physical
rename in PR 2 after the state-only move in PR 1 (#134, merge commit
11cb9e64).
"""
from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


# Smoke marker — these tests need a real Postgres backend because they
# inspect raw catalog information. LocMem / SQLite would emit no-op
# results for the catalog queries. Skip cleanly outside Postgres.
postgres_only = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="Table-rename migration test requires Postgres catalogs",
)


def _table_exists(table_name: str) -> bool:
    """Return True if the table is visible in the current Postgres schema."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s) IS NOT NULL",
            [f"public.{table_name}"],
        )
        return bool(cur.fetchone()[0])


@postgres_only
@pytest.mark.django_db(transaction=True)
class TestPaymentTableRenameMigration:
    """Forward = 0002 applied. Reverse = back to 0001 (PR 1 state)."""

    @pytest.fixture(autouse=True)
    def _restore_payments_schema(self):
        """Put the ``payments`` app back on its latest migration.

        Not belt-and-braces — load-bearing. ``transaction=True`` means
        pytest-django does NOT wrap the test in a transaction (that is
        the whole point: DDL needs to be able to commit), so every
        ``executor.migrate`` here lands permanently in the test database.
        These tests deliberately rewind ``payments`` to 0001/0002, and
        without this teardown they leave it there — for the rest of the
        session. Every test that runs afterwards in the same process
        sees a Payment table with no ``capture_state`` and no
        ``UserPaymentMethod``.

        That is not hypothetical: it is what turned CI red on PR #227.
        The suite stayed green for months only because nothing collected
        after ``payments/`` happened to touch those columns; the salon's
        manual-booking path (DRF-1063 block D) was the first that did,
        and it looked like a defect in the new code rather than a
        pre-existing hole here.

        Targets the graph's leaf rather than a hardcoded name so the
        next migration this app gains is restored automatically.
        """
        yield
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes("payments")
        if leaves:
            executor.migrate(leaves)

    def _migrate(self, app: str, target: str) -> None:
        # transaction=True on the django_db fixture means there is NO
        # enclosing transaction, so MigrationExecutor can issue DDL —
        # and that DDL commits. See _restore_payments_schema for the
        # consequence and why the teardown above is mandatory.
        executor = MigrationExecutor(connection)
        executor.migrate([(app, target)])

    def test_forward_renames_to_payments_payment(self):
        # Rewind payments only — appointments stays at 0006 with Payment
        # already removed from its state. Both apps "see" Payment over
        # the appointments_payment table for the duration of the test,
        # which is fine here because the assertions only inspect raw
        # Postgres catalog (to_regclass), not Django ProjectState.
        self._migrate("payments", "0001_initial")
        assert _table_exists("appointments_payment")
        assert not _table_exists("payments_payment")

        # Apply the rename.
        self._migrate("payments", "0002_rename_table")
        assert not _table_exists("appointments_payment")
        assert _table_exists("payments_payment")

    def test_reverse_restores_appointments_payment(self):
        # Be defensive — explicitly stand the test up at 0002 first so
        # this case is independent of test ordering.
        self._migrate("payments", "0002_rename_table")
        assert _table_exists("payments_payment")

        # Step backwards. Django auto-reverses AlterModelTable.
        self._migrate("payments", "0001_initial")
        assert _table_exists("appointments_payment")
        assert not _table_exists("payments_payment")

        # Re-apply so the assertion above stays the last thing the
        # reader's eye lands on, rather than an unwind that could be
        # mistaken for the test's main behaviour.
        #
        # This used to be described as "belt + suspenders — pytest-
        # django's transaction=True rollback would restore the canonical
        # state anyway". That was wrong, and the error is worth keeping
        # visible: transaction=True means there is no rollback to speak
        # of, and stopping at 0002 left 0003/0004 unapplied for the rest
        # of the session. The autouse teardown is what actually restores
        # the schema now.
        self._migrate("payments", "0002_rename_table")
