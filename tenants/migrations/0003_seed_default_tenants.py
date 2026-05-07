# DRF-242.7 — seed migration for default tenants.
"""Seed `formula-tela` and `ayla-marketplace` tenants.

Until DRF-242.6 every legacy row was assigned a tenant via the
`backfill_tenants` management command, which was created at runtime if
missing. That works for dev / staging where someone manually runs the
command, but a freshly-provisioned environment needs both tenants to
exist *before* any user / specialist row is inserted (otherwise
strict-mode middleware blocks every request the moment it boots).

This migration creates them idempotently. ``get_or_create`` so re-running
on an environment that already has them via `backfill_tenants` is a
no-op. Reverse migration is intentionally a no-op — these rows reference
real users / appointments and the schema-level cascade is PROTECT, so
``unmigrate`` would fail anyway. Operators must reassign data first if
they ever need to remove a tenant.

Why not just leave it to `backfill_tenants`? Because the management
command is a per-deploy step that's easy to forget. A migration is
deterministic, runs on every fresh `migrate`, and is auditable in the
migration history.
"""
from django.db import migrations


def seed(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    # ``formula-tela`` — single-tenant origin from the bot, kept so
    # ProxyUsers and existing FoodScans stay attributable.
    Tenant.objects.get_or_create(
        slug="formula-tela",
        defaults={"name": "Формула тела", "is_active": True},
    )
    # ``ayla-marketplace`` — default for new mobile-app users until they
    # join a specific tenant.
    Tenant.objects.get_or_create(
        slug="ayla-marketplace",
        defaults={"name": "Ayla Marketplace", "is_active": True},
    )


def unseed(apps, schema_editor):
    """No-op: see module docstring. PROTECT prevents naive deletion
    anyway, so the safer behaviour is to do nothing on rollback."""


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0002_rename_tenants_ten_is_acti_2c1ec7_idx_tenants_ten_is_acti_c80453_idx"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
