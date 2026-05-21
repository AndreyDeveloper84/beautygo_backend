"""Initial migration for the `payments` app — STATE-ONLY move of the
existing `Payment` model from `appointments`.

No DDL is emitted. The physical `appointments_payment` table is
unchanged; only Django's app-registry state is updated to attribute the
model to the `payments` app. A companion migration in `appointments` —
`0006_remove_payment_state` — mirrors this by removing the model from
appointments' state (also state-only).

A follow-up PR (#426 step 2) will rename the underlying table to
`payments_payment` via `AlterModelTable`.

See issue #426 and the migration plan dispatched via Plan-subagent.
"""
import django.core.validators
import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        # Wait for the original Payment migration so the table exists
        # in real DBs (state-side, Django's auto-detector sees this as
        # a no-op once the appointments mirror migration also runs).
        ('appointments', '0005_appointment_appt_tenant_status_idx_and_more'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='Payment',
                    fields=[
                        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('amount', models.DecimalField(decimal_places=2, max_digits=10, validators=[django.core.validators.MinValueValidator(0.01)])),
                        ('status', models.CharField(choices=[('pending', 'Ожидает'), ('authorized', 'Авторизован'), ('paid', 'Оплачен'), ('failed', 'Ошибка'), ('refunded', 'Возвращён'), ('partially_refunded', 'Частичный возврат')], default='pending', max_length=25)),
                        ('specialist_income', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                        ('platform_fee', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                        ('refunded_amount', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                        ('provider', models.CharField(blank=True, default='', max_length=50)),
                        ('provider_payment_id', models.CharField(blank=True, db_index=True, default='', max_length=200)),
                        ('provider_client_secret', models.CharField(blank=True, default='', max_length=500)),
                        ('last_webhook_event_id', models.CharField(blank=True, default='', max_length=200)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('appointment', models.ForeignKey(
                            on_delete=django.db.models.deletion.PROTECT,
                            related_name='payments',
                            to='appointments.appointment',
                        )),
                    ],
                    options={
                        'verbose_name': 'Платёж',
                        'verbose_name_plural': 'Платежи',
                        'db_table': 'appointments_payment',
                        'indexes': [models.Index(fields=['appointment'], name='payment_appointment_idx')],
                    },
                ),
            ],
            # No database_operations — the physical table already exists,
            # owned (state-wise) by appointments before this migration.
            # The mirror migration in appointments removes it from state
            # at the same migration plan tick.
            database_operations=[],
        ),
    ]
