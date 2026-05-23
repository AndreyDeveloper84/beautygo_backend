"""Backfill Appointment.tenant_id from specialist.tenant_id for legacy NULL rows (#568).

Why:
PR #142 (#520) added `tenant_id` stamping in CreateBookingService for
new bookings. Legacy rows created before #520 still have `tenant_id=NULL`.
The Q(tenant__isnull=True) backward-compat clause in
`AppointmentViewSet.get_queryset` keeps them accessible — but post-
2026-05-28 STRICT_TENANT consumer-side flip, bot-platform will reject
envelopes with `tenant_id: null`. This migration fills the gap before
the flip.

Strategy:
- Vendor-aware single UPDATE. Postgres supports UPDATE … FROM JOIN;
  SQLite (tests) needs a correlated subquery.
- Only touches rows where Appointment.tenant_id IS NULL — re-running
  is a no-op (idempotent).
- Leaves rows where specialist.tenant_id is also NULL untouched.
  These will be caught by a separate SpecialistProfile backfill (out
  of scope here — different ownership).

Reverse:
- No-op. Reversing the backfill would re-introduce the gap the
  STRICT_TENANT flip is set to reject. If a true rollback is needed,
  do it via raw SQL with explicit knowledge of which rows were touched.
"""
from django.db import migrations


def backfill_tenant_id(apps, schema_editor):
    """Fill Appointment.tenant_id from related SpecialistProfile.tenant_id.

    Pilot scale (~10k rows max) fits in one UPDATE. If this ever ships
    against >100k rows, switch to the chunked variant (see #568 PR body
    discussion).
    """
    vendor = schema_editor.connection.vendor

    if vendor == "postgresql":
        # UPDATE … FROM is Postgres-native and the fastest path.
        schema_editor.execute(
            """
            UPDATE appointments_appointment AS a
            SET tenant_id = sp.tenant_id
            FROM users_specialistprofile AS sp
            WHERE a.specialist_id = sp.id
              AND a.tenant_id IS NULL
              AND sp.tenant_id IS NOT NULL
            """
        )
    else:
        # SQLite + others: correlated subquery. Slightly slower but
        # universal. Used by the test suite running on SQLite.
        schema_editor.execute(
            """
            UPDATE appointments_appointment
            SET tenant_id = (
                SELECT sp.tenant_id
                FROM users_specialistprofile AS sp
                WHERE sp.id = appointments_appointment.specialist_id
            )
            WHERE tenant_id IS NULL
              AND specialist_id IN (
                  SELECT id FROM users_specialistprofile
                  WHERE tenant_id IS NOT NULL
              )
            """
        )


def reverse_noop(apps, schema_editor):
    """Reverse intentionally a no-op — see migration docstring."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('appointments', '0007_idempotencykey'),
    ]

    operations = [
        migrations.RunPython(backfill_tenant_id, reverse_noop),
    ]
