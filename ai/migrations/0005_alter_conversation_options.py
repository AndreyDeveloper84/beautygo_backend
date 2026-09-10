"""DRF-1128 — close Conversation.Meta.ordering with a tie-break on id.

Hand-written rather than generated. ``makemigrations ai`` also wants to
drop and recreate ``ai_conversation_one_active_per_user_tenant``: that
drift predates this change (it is already reported against origin/dev
with this file absent) and recreating a partial unique index on a live
pilot table is not something an ordering fix should carry. This
migration therefore contains the ``AlterModelOptions`` alone.

``AlterModelOptions`` is state-only — it emits no SQL.

DRF-1624: расхождение закрыто в
``0006_drf1624_constraint_condition_order``. Причина оказалась не в
базе, а в порядке операндов ``Q``: модель писала условие kwargs'ами,
которые ``Q.__init__`` сортирует, а ``0004`` — позиционными
кортежами, которые не сортируются. Отказ ЭТОЙ миграции тащить
перестройку индекса был верным: ordering-фикс действительно не
должен был её нести. Перестройка выпущена отдельно, в 0006, после
замера таблицы на пилоте — 2 строки на 10.09.2026.
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
