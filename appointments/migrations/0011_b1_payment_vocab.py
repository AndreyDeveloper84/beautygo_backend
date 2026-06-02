"""B-1 payment vocabulary alignment (Variant C).

Renames the ``payment.confirmed`` topic to ``payment.captured`` and
adds ``payment.failed`` to the OutboxEvent.Topic enum. Closes codex
P0-1 (Ayla emitted ``payment.confirmed`` while bot-platform's
eventbus consumer registers ``payment.captured``; every successful
capture landed in the DLQ).

Two steps:

1. ``AlterField`` flips the choices tuple (no data change at the
   schema layer — TextChoices uses CharField for storage).
2. RunPython rewrites any in-flight unprocessed row that still uses
   the legacy topic name. The dispatcher / publisher would otherwise
   skip those rows as ``unknown_topic`` because the legacy value
   disappeared from the choices. Processed rows (``processed_at``
   non-null) keep the legacy literal — they're historical audit data
   and rewriting them would race with replay tooling.
"""
from django.db import migrations, models


_LEGACY_TO_NEW = {
    "payment.confirmed": "payment.captured",
}


def _rewrite_legacy_topics(apps, schema_editor):
    OutboxEvent = apps.get_model("appointments", "OutboxEvent")
    # Only touch rows still queued for any consumer — once
    # processed_at is set the row is historical audit data and
    # rewriting it could race with replay tooling reading the
    # legacy value.
    for legacy, new in _LEGACY_TO_NEW.items():
        OutboxEvent.objects.filter(
            topic=legacy, processed_at__isnull=True,
        ).update(topic=new)


def _restore_legacy_topics(apps, schema_editor):
    """Reverse: put legacy names back on still-unprocessed rows so a
    rollback to the old code does not lose pending work.

    ``payment.failed`` has no legacy counterpart — it's a new topic
    added in B-1b. Pending rows under this topic are deleted on
    reverse so they don't sit as ``unknown_topic`` after rollback to
    pre-B-1 code (dispatcher would silently skip them anyway, but
    the explicit delete keeps the queue clean). Processed
    ``payment.failed`` rows stay (historical audit data).
    """
    OutboxEvent = apps.get_model("appointments", "OutboxEvent")
    inverse = {v: k for k, v in _LEGACY_TO_NEW.items()}
    for new, legacy in inverse.items():
        OutboxEvent.objects.filter(
            topic=new, processed_at__isnull=True,
        ).update(topic=legacy)
    OutboxEvent.objects.filter(
        topic="payment.failed", processed_at__isnull=True,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('appointments', '0010_outbox_dual_delivery_fields'),
    ]

    operations = [
        migrations.AlterField(
            model_name='outboxevent',
            name='topic',
            field=models.CharField(choices=[('booking.created', 'Запись создана'), ('booking.confirmed', 'Запись подтверждена'), ('booking.cancelled', 'Запись отменена'), ('booking.rescheduled', 'Запись перенесена'), ('booking.completed', 'Запись завершена'), ('booking.no_show', 'Клиент не пришёл'), ('payment.captured', 'Оплата захвачена'), ('payment.failed', 'Оплата не прошла'), ('payment.refunded', 'Возврат оплаты'), ('cache.invalidate_slots', 'Инвалидация кеша слотов'), ('tenant.relationship.revoked', 'TenantUserRelationship отозван (#246 Q1)')], max_length=50),
        ),
        migrations.RunPython(_rewrite_legacy_topics, _restore_legacy_topics),
    ]
