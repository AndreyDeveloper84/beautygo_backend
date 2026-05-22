"""IdempotencyKey table for replay protection on cancel + reschedule (#512)."""
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('appointments', '0006_remove_payment_state'),
    ]

    operations = [
        migrations.CreateModel(
            name='IdempotencyKey',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('key', models.CharField(help_text='Client-provided X-Idempotency-Key header value.', max_length=128)),
                ('operation_name', models.CharField(
                    help_text="Operation identifier, e.g. 'booking.cancel'. Scoped so a client can reuse the same key value across different operations without collision.",
                    max_length=64,
                )),
                ('target_type', models.CharField(blank=True, default='', help_text="Optional model name of the target row (e.g. 'Appointment').", max_length=64)),
                ('target_id', models.CharField(blank=True, default='', help_text='Optional target row id (UUID stringified) for audit only.', max_length=64)),
                ('request_body_hash', models.CharField(help_text='SHA256 of the normalised request body. Mismatched on replay = 422.', max_length=64)),
                ('response_status', models.PositiveSmallIntegerField()),
                ('response_payload', models.JSONField(default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='idempotency_keys',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'verbose_name': 'Idempotency Key',
                'verbose_name_plural': 'Idempotency Keys',
            },
        ),
        migrations.AddConstraint(
            model_name='idempotencykey',
            constraint=models.UniqueConstraint(
                fields=('user', 'operation_name', 'key', 'target_id'),
                name='idempotency_unique_user_op_key_target',
            ),
        ),
        migrations.AddIndex(
            model_name='idempotencykey',
            index=models.Index(
                fields=['user', 'operation_name', 'key', 'target_id'],
                name='idempotency_lookup_idx',
            ),
        ),
    ]
