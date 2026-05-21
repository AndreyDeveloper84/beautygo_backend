"""Rename DB table appointments_payment → payments_payment.

Step 2 of 2 for #426 (issue #492). PR 1 (#134, merge commit 11cb9e64)
did the Django-state-only move of Payment from the appointments app
to the payments app, leaving the physical table named
`appointments_payment` via a `Meta.db_table` pin. This migration emits
the actual `ALTER TABLE … RENAME TO …` and drops the pin so Django's
default naming (`<app_label>_<modelname>` → `payments_payment`) takes
over.

Postgres-side this is a single atomic SQL statement holding ACCESS
EXCLUSIVE on the table for milliseconds. Auto-reversed by Django via
the inverse `AlterModelTable(name='payment', table='appointments_payment')`.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0001_initial'),
        # Also depend on the appointments-side state removal so the
        # plan applies both halves before this rename. Without this
        # edge a `migrate payments` invocation without `migrate
        # appointments` could try to rename a table the appointments
        # app still thinks it owns.
        ('appointments', '0006_remove_payment_state'),
    ]

    operations = [
        migrations.AlterModelTable(
            name='payment',
            table='payments_payment',
        ),
    ]
