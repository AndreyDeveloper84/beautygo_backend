"""provision_salon_admin — the precondition for everything else (DRF-1062).

The audit of 2026-08-14 found the pilot salon with zero administrators, so
``IsTenantAdmin`` could never pass for anyone. This command is what stops
the new admin surface from being as unreachable as the bot's Mini App,
which sits built and deployed behind an empty staff table.

What matters here: it can be run twice, and it refuses to quietly restore
access somebody removed on purpose.
"""
from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from tenants.models import Tenant
from users.models import TenantUserRelationship, User

PHONE = "+79990001062"


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="prov-1062", name="Салон провижининга")


def _run(**kwargs):
    call_command("provision_salon_admin", **kwargs)


def _active(user, tenant):
    return TenantUserRelationship.objects.filter(
        user=user, tenant=tenant, is_active=True,
    ).first()


class TestProvisioning:
    def test_creates_account_and_grants_admin(self, salon):
        _run(phone=PHONE, tenant=salon.slug)

        user = User.objects.get(phone=PHONE)
        rel = _active(user, salon)
        assert rel is not None
        assert rel.role == TenantUserRelationship.Role.ADMIN

    def test_running_twice_changes_nothing(self, salon):
        _run(phone=PHONE, tenant=salon.slug)
        _run(phone=PHONE, tenant=salon.slug)

        assert User.objects.filter(phone=PHONE).count() == 1
        assert TenantUserRelationship.objects.filter(
            tenant=salon, is_active=True,
        ).count() == 1

    def test_existing_account_is_reused_not_duplicated(self, salon):
        existing = User.objects.create_user(
            username="already_here", password="x", role="client", phone=PHONE,
        )

        _run(phone=PHONE, tenant=salon.slug)

        assert User.objects.filter(phone=PHONE).count() == 1
        assert _active(existing, salon).role == TenantUserRelationship.Role.ADMIN

    def test_customer_relationship_is_promoted_in_place(self, salon):
        """The partial unique constraint allows one active row per pair, so
        a second grant would fail rather than upgrade them."""
        user = User.objects.create_user(
            username="was_customer", password="x", role="client", phone=PHONE,
        )
        TenantUserRelationship.objects.filter(user=user).delete()
        TenantUserRelationship.objects.create(
            user=user, tenant=salon,
            role=TenantUserRelationship.Role.CUSTOMER, is_active=True,
        )

        _run(phone=PHONE, tenant=salon.slug)

        assert TenantUserRelationship.objects.filter(
            user=user, tenant=salon, is_active=True,
        ).count() == 1
        assert _active(user, salon).role == TenantUserRelationship.Role.ADMIN

    def test_revoked_access_is_not_silently_restored(self, salon):
        user = User.objects.create_user(
            username="was_revoked", password="x", role="client", phone=PHONE,
        )
        TenantUserRelationship.objects.filter(user=user).delete()
        TenantUserRelationship.objects.create(
            user=user, tenant=salon,
            role=TenantUserRelationship.Role.ADMIN, is_active=False,
        )

        with pytest.raises(CommandError, match="revoked"):
            _run(phone=PHONE, tenant=salon.slug)

    def test_unknown_tenant_fails_loudly(self, db):
        with pytest.raises(CommandError, match="does not exist"):
            _run(phone=PHONE, tenant="no-such-salon")

    def test_dry_run_writes_nothing(self, salon):
        _run(phone=PHONE, tenant=salon.slug, dry_run=True)

        assert not User.objects.filter(phone=PHONE).exists()
