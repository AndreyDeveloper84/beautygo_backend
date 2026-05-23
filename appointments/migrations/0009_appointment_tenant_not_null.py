"""Schema-lock Appointment.tenant = NOT NULL + CheckConstraint (#590).

Closes the accidental-NULL window left by #568. PR #144's backfill
filled all NULLs at the data layer; this migration enforces the
invariant at the schema layer so future admin / management / raw
inserts can't silently land in the gap.

Sequence:
1. Pre-flight ``verify_no_null_tenant`` RunPython — refuses to apply
   if any Appointment row still has ``tenant_id IS NULL``. That can
   only happen if migration 0008 (#568) wasn't run yet OR if orphan
   specialists (specialist.tenant_id=NULL) had bookings the
   SpecialistProfile-side backfill hasn't reached yet. Surfaces the
   blocker loudly instead of failing at AlterField time with a
   cryptic IntegrityError.
2. ``AlterField(null=False)`` flips the FK declaration.
3. ``AddConstraint(CheckConstraint(Q(tenant__isnull=False)))`` —
   belt-and-suspenders DB-level guard. Stops a future migration that
   accidentally re-introduces null=True from re-opening the gap.

Reverse: documented no-op for the verifier; framework auto-reverses
the AlterField + DropConstraint if rolling back to 0008.
"""
from django.db import migrations, models


def verify_no_null_tenant(apps, schema_editor):
    """Refuse to flip null=False if any rows still have tenant=NULL.

    Returns silently when zero NULLs exist. Raises RuntimeError with
    a count + remediation pointer otherwise. The RunPython framework
    treats raise as migration-failure → entire migration rolls back,
    schema unchanged.
    """
    Appointment = apps.get_model("appointments", "Appointment")
    null_count = Appointment.objects.filter(tenant__isnull=True).count()
    if null_count > 0:
        raise RuntimeError(
            f"Cannot lock Appointment.tenant=NOT_NULL: {null_count} "
            "rows still have tenant_id IS NULL. Run migration 0008 "
            "(backfill from specialist.tenant) first; if orphan "
            "SpecialistProfiles with tenant=NULL had bookings, "
            "backfill SpecialistProfile.tenant via the DRF-242.4 "
            "data-migration before applying 0009. See "
            "ai-bot-platform#586 for the ops sanity query."
        )


def reverse_verify_noop(apps, schema_editor):
    """Reverse: no-op. Re-introducing null=True doesn't need a guard."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('appointments', '0008_backfill_appointment_tenant'),
    ]

    operations = [
        migrations.RunPython(verify_no_null_tenant, reverse_verify_noop),
        migrations.AlterField(
            model_name='appointment',
            name='tenant',
            field=models.ForeignKey(
                on_delete=models.PROTECT,
                related_name='appointments',
                to='tenants.tenant',
            ),
        ),
        migrations.AddConstraint(
            model_name='appointment',
            constraint=models.CheckConstraint(
                check=models.Q(tenant__isnull=False),
                name='appt_tenant_not_null',
            ),
        ),
    ]
