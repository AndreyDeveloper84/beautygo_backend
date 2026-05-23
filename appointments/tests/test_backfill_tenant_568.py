"""Regression tests for #568 — backfill Appointment.tenant_id (migration 0008).

Status update post-#590:
- Migration 0009 added schema-level NOT NULL + CheckConstraint on
  Appointment.tenant. Legacy NULL inserts are now structurally
  impossible at the DB level — the data tests that exercised the
  backfill SQL on NULL rows can't be reproduced because the test
  DB applies 0009 before tests run.
- The backfill SQL itself remains audit-relevant: 0009's pre-flight
  verifier raises if any NULLs exist, which transitively pins that
  0008 successfully ran. Migration-graph test continues to be useful.
- Detailed backfill behaviour (orphan untouched / idempotent /
  non-overwrite) is now pinned by 0009's structural guarantee plus
  test_tenant_not_null_590.py.
"""
from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db
class TestMigrationApplied:
    """0008 sanity — confirms backfill is on the migration graph."""

    def test_migration_0008_in_graph(self):
        executor = MigrationExecutor(connection)
        names = {
            n[1] for n in executor.loader.graph.nodes if n[0] == "appointments"
        }
        assert "0008_backfill_appointment_tenant" in names, (
            f"Migration 0008 missing from graph; found {sorted(names)}"
        )
