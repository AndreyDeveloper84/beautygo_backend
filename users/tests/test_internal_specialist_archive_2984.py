"""DRF-2984 C3 — PATCH /api/v1/internal/specialists/{id}/archive/.

The bot's archive of a master (deactivate / return) is the salon's
decision, so the subject is an admin of the master's tenant, not the
master. ``archived=true`` takes the profile out of the catalog pool —
neither sold nor billed; the master's own pause is untouched.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from billing.models import SpecialistSubscription, TariffPlan
from billing.pricing import billable_masters_count
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User

pytestmark = pytest.mark.django_db

RUNTIME = "test-runtime-token-2984a"  # noqa: S105
PROVISIONING = "test-provisioning-token-2984a"  # noqa: S105
OWNER = "bot:max:2984101"
OTHER_OWNER = "bot:max:2984102"
MASTER = "bot:max:2984103"


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME
    settings.AYLA_IDENTITY_PROVISIONING_TOKEN = PROVISIONING


def _client(actor: str | None = OWNER, bearer: str = RUNTIME) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if actor is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = actor
    return c


def _person(external_id: str, nick: str, *, role: str = "client") -> User:
    real = User.objects.create_user(
        username=nick, password="x", role=role,  # noqa: S106
        phone=f"+7999298{nick[-4:]}",
    )
    User.objects.create(
        username=external_id, role="client", is_proxy=True, is_guest=False,
        linked_user=real,
    )
    return real


def _admin(tenant: Tenant, external_id: str, nick: str) -> User:
    user = _person(external_id, nick)
    TenantUserRelationship.objects.create(
        user=user, tenant=tenant, role=TenantUserRelationship.Role.ADMIN,
        is_active=True,
    )
    return user


@pytest.fixture
def salon() -> Tenant:
    return Tenant.objects.create(name="Archive Salon", slug="archive-2984")


@pytest.fixture
def owner(salon) -> User:
    return _admin(salon, OWNER, "arch_owner_0001")


@pytest.fixture
def master(salon) -> SpecialistProfile:
    user = _person(MASTER, "arch_master_0002", role="specialist")
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = salon
    profile.status = "active"
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.save()
    return profile


def _url(profile) -> str:
    return f"/api/v1/internal/specialists/{profile.pk}/archive/"


def _patch(profile, body, **kw):
    return _client(**kw).patch(_url(profile), body, format="json")


class TestArchive:
    def test_archive_and_return(self, owner, master):
        resp = _patch(master, {"archived": True})
        assert resp.status_code == 200, resp.content
        assert resp.json()["data"] == {
            "specialist_id": str(master.pk), "archived": True,
            "accepting_bookings": True, "status": "active",
        }
        master.refresh_from_db()
        assert master.is_available is False
        assert master.is_booking_enabled is True
        assert _patch(master, {"archived": False}).json()["data"]["archived"] is False
        master.refresh_from_db()
        assert master.is_available is True

    def test_archived_master_is_not_billed(self, salon, owner, master):
        sub = SpecialistSubscription.objects.create(
            user=owner, tenant=salon, tariff=TariffPlan.objects.get(code="salon"),
        )
        assert billable_masters_count(sub) == 1
        _patch(master, {"archived": True})
        assert billable_masters_count(sub) == 0

    def test_draft_can_be_archived(self, owner, master):
        SpecialistProfile.objects.filter(pk=master.pk).update(status="draft")
        assert _patch(master, {"archived": True}).status_code == 200

    @pytest.mark.parametrize("body", [{"archived": "true"}, {"archived": 1}, {}])
    def test_only_a_boolean(self, owner, master, body):
        assert _patch(master, body).status_code == 400
        master.refresh_from_db()
        assert master.is_available is True


class TestSubject:
    def test_unnamed_403(self, owner, master):
        assert _patch(master, {"archived": True}, actor=None).status_code == 403

    def test_provisioning_credential_refused(self, owner, master):
        assert _patch(master, {"archived": True}, bearer=PROVISIONING).status_code == 403

    def test_admin_of_another_salon_404(self, owner, master):
        _admin(Tenant.objects.create(name="Other", slug="archive-other"), OTHER_OWNER, "arch_other_0003")
        assert _patch(master, {"archived": True}, actor=OTHER_OWNER).status_code == 404
        master.refresh_from_db()
        assert master.is_available is True

    def test_the_master_themself_is_not_the_subject(self, owner, master):
        assert _patch(master, {"archived": True}, actor=MASTER).status_code == 404

    def test_master_without_salon_404(self, owner, master):
        SpecialistProfile.objects.filter(pk=master.pk).update(tenant=None)
        assert _patch(master, {"archived": True}).status_code == 404

    def test_revoked_admin_404(self, salon, owner, master):
        TenantUserRelationship.objects.filter(user=owner).update(is_active=False)
        assert _patch(master, {"archived": True}).status_code == 404
