"""Convert Conversation.tenant_id (UUIDField) → Conversation.tenant (FK to Tenant).

DRF-242.2. The DB column name stays `tenant_id` because Django's FK default
column naming matches the original column. So existing call sites that
read/write `conv.tenant_id` (raw UUID) keep working — Django exposes both
the `tenant` instance accessor and the `tenant_id` raw-column attribute
on FK fields.

Migration order is constraint-first / field-replace / constraint-last so
neither the partial unique nor the composite index ever references a
non-existent column at any point.

PROTECT on delete: dropping a Tenant must not silently delete user
conversations — that's a billing/legal incident path, not auto-cleanup.
Admins must reassign or soft-delete conversations first.

Data note: this migration drops the tenant_id column and recreates it as
a FK column. Any rows whose tenant_id pointed at a UUID NOT present in
tenants.Tenant would have failed FK creation. By construction (DRF-240
just shipped, no production tenant assignment yet) the column is empty
in dev/staging. If a future env has stray values, run
`UPDATE ai_conversation SET tenant_id = NULL WHERE tenant_id NOT IN (SELECT id FROM tenants_tenant);`
before applying.
"""
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0003_one_active_conversation_per_user_plus_action_index"),
        ("tenants", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 1. Drop the constraint + index that reference the old field.
        migrations.RemoveConstraint(
            model_name="conversation",
            name="ai_conversation_one_active_per_user_tenant",
        ),
        migrations.RemoveIndex(
            model_name="conversation",
            name="ai_conversa_tenant__febb36_idx",
        ),
        # 2. Drop the old UUIDField. SQLite recreates the table; PostgreSQL
        #    does ALTER TABLE DROP COLUMN. Both safe because the column is
        #    empty in dev/staging (see module docstring).
        migrations.RemoveField(
            model_name="conversation",
            name="tenant_id",
        ),
        # 3. Add the FK. Django's default db_column for `tenant` FK is
        #    `tenant_id`, matching the original column name — so any raw
        #    SQL or `obj.tenant_id` reads keep working.
        migrations.AddField(
            model_name="conversation",
            name="tenant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.PROTECT,
                related_name="conversations",
                to="tenants.tenant",
            ),
        ),
        # 4. Re-add index + constraint with the new field name.
        migrations.AddIndex(
            model_name="conversation",
            index=models.Index(
                fields=["tenant", "is_active", "-last_message_at"],
                name="ai_conversa_tenant__febb36_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="conversation",
            constraint=models.UniqueConstraint(
                fields=("user", "tenant"),
                condition=models.Q(
                    ("is_active", True), ("deleted_at__isnull", True),
                ),
                name="ai_conversation_one_active_per_user_tenant",
            ),
        ),
    ]
