"""DRF-242.7 — seed migration creates default tenants.

The migration is the source of truth for tenant existence on a freshly
provisioned environment. These tests assert both seeded tenants land in
the database and that the migration is idempotent (re-running yields
no duplicates).
"""
from __future__ import annotations

import pytest

from tenants.models import Tenant


pytestmark = pytest.mark.django_db


class TestSeedDefaultTenants:
    """The seed runs as part of pytest-django's migration apply, so by
    the time a test starts the rows already exist. We verify that, then
    verify idempotency by importing the migration's seed function and
    re-invoking it directly."""

    def test_formula_tela_tenant_seeded(self):
        t = Tenant.objects.filter(slug="formula-tela").first()
        assert t is not None, "formula-tela tenant must be seeded by migration"
        assert t.name == "Формула тела"
        assert t.is_active is True

    def test_ayla_marketplace_tenant_seeded(self):
        t = Tenant.objects.filter(slug="ayla-marketplace").first()
        assert t is not None, "ayla-marketplace tenant must be seeded by migration"
        assert t.name == "Ayla Marketplace"
        assert t.is_active is True

    def test_seed_function_is_idempotent(self):
        """Re-running the seed must not create duplicates. Important
        because operators may run `migrate` multiple times on the same
        env (e.g. after a partial deploy)."""
        # Late import — module isn't on the import path until migrations
        # have been imported by Django.
        import importlib

        seed_module = importlib.import_module(
            "tenants.migrations.0003_seed_default_tenants",
        )
        before = Tenant.all_objects.count()
        seed_module.seed(apps=_RealAppsShim(), schema_editor=None)
        after = Tenant.all_objects.count()
        assert before == after


class _RealAppsShim:
    """Minimal stand-in for the migration `apps` registry — we want
    real model semantics in this idempotency test, not the historical
    proxy that Django passes during actual migration runs."""

    def get_model(self, app_label: str, model_name: str):
        from django.apps import apps as real_apps

        return real_apps.get_model(app_label, model_name)
