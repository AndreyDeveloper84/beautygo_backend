"""Remove `Payment` from `appointments` app-registry state.

Mirror of `payments/migrations/0001_initial.py`. Both migrations use
`SeparateDatabaseAndState` with empty `database_operations` — the
physical table `appointments_payment` is unchanged.

The pair must be applied together (depends_on ensures order). After
both run, Django sees `Payment` as living in the `payments` app while
the SQL table keeps its legacy name. A follow-up #426 step 2 renames
the table to `payments_payment` via `AlterModelTable`.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('appointments', '0005_appointment_appt_tenant_status_idx_and_more'),
        # Ensure payments owns Payment in Django state before we delete
        # it from appointments' state — otherwise Django briefly loses
        # the model entirely between the two migrations.
        ('payments', '0001_initial'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveIndex(
                    model_name='payment',
                    name='payment_appointment_idx',
                ),
                migrations.RemoveField(
                    model_name='payment',
                    name='appointment',
                ),
                migrations.DeleteModel(name='Payment'),
            ],
            database_operations=[],
        ),
    ]
