"""DRF-2826 K4 — the salon-admin link also assigns a RECEPTIONIST, authorised by an admin.

Owner decision 06.10: the link widens to ``admin | receptionist``; a
receptionist is assigned by an authorised ADMIN of THAT salon (server-side
check + the same readback); a receptionist does not assign or raise roles;
DRF-2085 is superseded only in the list of assignable roles; existing
assignments are never changed automatically.

The endpoint and credential are DRF-2085's; see ``test_salon_admin_link_2085``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from tenants.models import Tenant
from users.models import TenantUserRelationship, User
from users.permissions import IsTenantAdmin, IsTenantBookingDesk
from users.salon_admin_linking import USERNAME_PREFIX

pytestmark = pytest.mark.django_db

LINK = "test-salon-admin-link-2826"
GENERAL = "test-general-bot-token-2826"
NEW_DESK = "bot:max:2826new"
ACTOR = "bot_admin:2826"
ROLE = TenantUserRelationship.Role


@pytest.fixture(autouse=True)
def _env(settings):
    settings.AYLA_SALON_ADMIN_LINK_TOKEN = LINK
    settings.AYLA_INTERNAL_API_TOKEN = GENERAL
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def salon():
    return Tenant.objects.create(slug="link-2826-a", name="Салон А")


@pytest.fixture
def other_salon():
    return Tenant.objects.create(slug="link-2826-b", name="Салон Б")


def _person(username, tenant, role):
    user = User.objects.create_user(
        username=username, password="x", role="client", phone=None,
    )
    TenantUserRelationship.objects.create(user=user, tenant=tenant, role=role, is_active=True)
    return user


@pytest.fixture
def admin_a(salon):
    return _person("bot:max:2826admin", salon, ROLE.ADMIN)


@pytest.fixture
def admin_b(other_salon):
    return _person("bot:max:2826adminb", other_salon, ROLE.ADMIN)


@pytest.fixture
def desk_a(salon):
    return _person("bot:max:2826desk", salon, ROLE.RECEPTIONIST)


def _link(slug, **body):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {LINK}")
    payload = {
        "external_user_id": NEW_DESK,
        "actor": ACTOR,
        "correlation_id": "corr-2826",
        "idempotency_key": "idem-2826-0001",
    }
    payload.update(body)
    return client.post(f"/api/v1/internal/tenants/{slug}/salon-admins/", payload, format="json")


def _probe(external, tenant):
    from users.services import resolve_external_user_readonly

    return SimpleNamespace(user=resolve_external_user_readonly(external), tenant=tenant)


def _fresh_desks(tenant):
    return TenantUserRelationship.objects.filter(
        tenant=tenant, role=ROLE.RECEPTIONIST, user__username__startswith=USERNAME_PREFIX,
    ).count()


class TestAnAdminAssignsAReceptionist:
    def test_k1_admin_of_the_salon_assigns_and_the_desk_works_without_admin_powers(
        self, salon, admin_a,
    ):
        resp = _link(salon.slug, role="receptionist", assigned_by_external_user_id=admin_a.username)

        assert resp.status_code == 201, resp.data
        body = resp.data.get("data", resp.data)
        assert body["role"] == "receptionist"
        probe = _probe(NEW_DESK, salon)
        assert IsTenantBookingDesk().has_permission(probe, None) is True
        assert IsTenantAdmin().has_permission(probe, None) is False
        assert probe.user.role == "client"  # authority from the TUR, not User.role

    def test_k2_the_admin_path_is_unchanged(self, salon):
        resp = _link(salon.slug)

        assert resp.status_code == 201, resp.data
        assert IsTenantAdmin().has_permission(_probe(NEW_DESK, salon), None) is True


class TestOnlyAnAdminOfThisSalonAssigns:
    @pytest.mark.parametrize("assigner", ["admin_b", "desk_a", None])
    def test_k3_other_salons_admin_a_receptionist_or_nobody_is_refused(
        self, request, salon, assigner,
    ):
        external = request.getfixturevalue(assigner).username if assigner else ""

        resp = _link(salon.slug, role="receptionist", assigned_by_external_user_id=external)

        assert resp.status_code == 403, resp.data
        assert "assigner_not_admin" in str(resp.data)
        assert _fresh_desks(salon) == 0
        assert not User.objects.filter(username=NEW_DESK, linked_user__isnull=False).exists()

    @pytest.mark.parametrize("role", ["staff", "customer", "owner"])
    def test_k4_no_other_role_is_assignable(self, salon, admin_a, role):
        resp = _link(salon.slug, role=role, assigned_by_external_user_id=admin_a.username)

        assert resp.status_code == 400, resp.data


class TestNothingExistingChanges:
    def test_k5_existing_admin_grants_are_untouched(self, salon, admin_a):
        before = list(
            TenantUserRelationship.objects.filter(tenant=salon, role=ROLE.ADMIN, is_active=True)
            .values_list("id", "user_id")
        )

        _link(salon.slug, role="receptionist", assigned_by_external_user_id=admin_a.username)

        after = list(
            TenantUserRelationship.objects.filter(tenant=salon, role=ROLE.ADMIN, is_active=True)
            .values_list("id", "user_id")
        )
        assert before == after
        assert len(before) == 1  # presence: the admin grant is there

    def test_k6_the_same_key_with_another_role_is_a_reused_key(self, salon, admin_a):
        first = _link(salon.slug, role="receptionist", assigned_by_external_user_id=admin_a.username)
        again = _link(salon.slug, role="admin")

        assert first.status_code == 201, first.data
        assert again.status_code == 409, again.data
        assert "idempotency_key_reused" in str(again.data)
