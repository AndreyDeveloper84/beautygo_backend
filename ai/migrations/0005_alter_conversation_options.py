"""DRF-1128 — close Conversation.Meta.ordering with a tie-break on id.

Hand-written rather than generated. ``makemigrations ai`` also wants to
drop and recreate ``ai_conversation_one_active_per_user_tenant``: that
drift predates this change (it is already reported against origin/dev
with this file absent) and recreating a partial unique index on a live
pilot table is not something an ordering fix should carry. This
migration therefore contains the ``AlterModelOptions`` alone.

``AlterModelOptions`` is state-only — it emits no SQL.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0004_conversation_tenant_fk"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="conversation",
            options={
                "ordering": ["-last_message_at", "-created_at", "-id"],
                "verbose_name": "AI Conversation",
                "verbose_name_plural": "AI Conversations",
            },
        ),
    ]
