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

    def _migrate(self, app: str, target: str) -> None:
        # transaction=True on the django_db fixture means we own the
        # outer transaction — MigrationExecutor can issue DDL inside.
        executor = MigrationExecutor(connection)
        executor.migrate([(app, target)])

    def test_forward_renames_to_payments_payment(self):
        # Start from the just-before-rename state.
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

        # Re-apply so the next test in the class (and any teardown
        # fixture that pokes the Payment table) sees the canonical
        # post-PR-2 state. Without this, ordering between the two
        # test methods becomes load-bearing.
        self._migrate("payments", "0002_rename_table")
