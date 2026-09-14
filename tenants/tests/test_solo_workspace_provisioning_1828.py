"""DRF-1828 (M27, G1/G4): POST /api/v1/internal/tenants/solo-workspaces/.

Решение владельца 12.09: при создании solo workspace каталог сразу заводит
``Tenant`` (тот же UUID, что у бота; ``kind=solo``) и
``SpecialistProfile(status=DRAFT)``; pre-LINKED — workspace setup authority,
не identity authority; при LINKED новый профиль не создаётся — существующий
DRAFT привязывается одной записью.

Что стережётся:

* граница токена: provisioning-токен заводит, общий и identity — 403;
* всё создаётся ровно по одному разу в одной транзакции, и повтор с тем же
  claim возвращает те же id;
* чужой slug / чужой UUID / claim под другим workspace — 409 с именем причины,
  ничего не создано и ничего не обновлено;
* рабочий ``User`` — не прокси и не в пространстве ``bot:``;
* LINKED одной записью: после ``bind_external_identity_by_operator`` тот же
  профиль, ``me/identity`` отдаёт W, FK профиля не двигался;
* claim читают ровно два места (сторож против «второй личности»).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from rest_framework.test import APIClient

from tenants.models import Tenant
from users.models import Profile, SpecialistProfile, TenantUserRelationship, User
from users.services import bind_external_identity_by_operator, resolve_external_user

pytestmark = pytest.mark.django_db

URL = "/api/v1/internal/tenants/solo-workspaces/"
IDENTITY_URL = "/api/v1/internal/me/identity/"
PROVISIONING = "test-tenant-provisioning-1828"
IDENTITY = "test-identity-provisioning-1828"
GENERAL = "test-general-bot-token-1828"

TENANT_ID = uuid.UUID("11111111-2222-4333-8444-555555555555")
SLUG = "solo-max-1828abcd"
EXTERNAL = "bot:max:1828001"


@pytest.fixture
def tokens(settings):
    settings.AYLA_TENANT_PROVISIONING_TOKEN = PROVISIONING
    settings.AYLA_IDENTITY_PROVISIONING_TOKEN = IDENTITY
    settings.AYLA_INTERNAL_API_TOKEN = GENERAL


@pytest.fixture
def client(tokens):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {PROVISIONING}")
    return c


def _body(**over):
    base = {
        "tenant_id": str(TENANT_ID),
        "slug": SLUG,
        "name": "Студия Ольга",
        "city": "Пенза",
        "external_user_id": EXTERNAL,
        "display_name": "Ольга Петрова",
    }
    base.update(over)
    return base


def _counts() -> dict:
    return {
        "tenants": Tenant.all_objects.filter(slug=SLUG).count(),
        "users": User.objects.filter(username__startswith="solo:").count(),
        "profiles": SpecialistProfile.objects.filter(provisioned_external_user_id=EXTERNAL).count(),
    }


class TestTheGuardIsTheProvisioningOne:
    def test_general_bearer_is_refused_and_creates_nothing(self, tokens):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {GENERAL}")
        assert c.post(URL, _body(), format="json").status_code == 403
        assert _counts() == {"tenants": 0, "users": 0, "profiles": 0}

    def test_identity_bearer_is_refused_and_creates_nothing(self, tokens):
        """A3: identity-полномочие ≠ provisioning-полномочие — на ручке,
        которая заводит User(specialist), это не абстракция."""
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {IDENTITY}")
        assert c.post(URL, _body(), format="json").status_code == 403
        assert _counts() == {"tenants": 0, "users": 0, "profiles": 0}

    def test_empty_tenant_token_closes_the_route(self, settings):
        settings.AYLA_TENANT_PROVISIONING_TOKEN = ""
        settings.AYLA_IDENTITY_PROVISIONING_TOKEN = IDENTITY
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {IDENTITY}")
        assert c.post(URL, _body(), format="json").status_code == 403


class TestOneWorkspaceIsCreatedOnce:
    def test_first_call_creates_the_whole_workspace_in_one_go(self, client):
        r = client.post(URL, _body(), format="json")
        assert r.status_code == 201, r.content
        data = r.json()["data"]

        tenant = Tenant.all_objects.get(id=TENANT_ID)
        assert tenant.slug == SLUG and tenant.kind == Tenant.Kind.SOLO
        assert tenant.city == "Пенза" and tenant.is_active is True
        assert data["tenant_id"] == str(TENANT_ID) and data["slug"] == SLUG

        profile = SpecialistProfile.objects.get(id=data["specialist_id"])
        assert profile.status == "draft"
        assert profile.display_name == "Ольга Петрова"
        assert profile.tenant_id == tenant.id
        assert profile.provisioned_external_user_id == EXTERNAL
        assert data["status"] == "draft"

        user = profile.user
        assert str(user.id) == data["user_id"]
        assert user.role == "specialist" and user.is_proxy is False
        assert user.username == f"solo:{SLUG}"
        assert not user.username.startswith("bot:")
        assert not user.has_usable_password()
        assert user.tenant_id == tenant.id
        assert Profile.objects.filter(user=user).exists()

        tur = TenantUserRelationship.objects.get(user=user, tenant=tenant, is_active=True)
        assert tur.role == TenantUserRelationship.Role.ADMIN
        assert tur.granted_by == TenantUserRelationship.GrantedBy.SYSTEM

    def test_a_repeat_with_the_same_claim_returns_the_same_ids_and_creates_nothing_new(
        self, client
    ):
        first = client.post(URL, _body(), format="json").json()["data"]
        before = _counts()

        r = client.post(URL, _body(display_name="Другое имя — не обновляется"), format="json")
        assert r.status_code == 200, r.content
        assert r.json()["data"] == first
        assert _counts() == before
        # Ничего не обновлено на существующей строке.
        assert SpecialistProfile.objects.get(id=first["specialist_id"]).display_name == "Ольга Петрова"

    def test_two_masters_are_two_workspaces(self, client):
        assert client.post(URL, _body(), format="json").status_code == 201
        other = _body(
            tenant_id=str(uuid.uuid4()), slug="solo-max-1828other", external_user_id="bot:max:1828002"
        )
        assert client.post(URL, other, format="json").status_code == 201
        assert Tenant.all_objects.filter(kind=Tenant.Kind.SOLO).count() == 2


class TestForeignRowsAreRefusedNotUpdated:
    def test_a_slug_of_another_tenant_is_409(self, client):
        Tenant.all_objects.create(slug=SLUG, name="Чужой салон")
        r = client.post(URL, _body(), format="json")
        assert r.status_code == 409, r.content
        assert r.json()["error"]["details"]["reason"] == "slug_taken"
        assert User.objects.filter(username__startswith="solo:").count() == 0
        assert Tenant.all_objects.get(slug=SLUG).name == "Чужой салон"

    def test_a_uuid_of_another_tenant_is_409(self, client):
        Tenant.all_objects.create(id=TENANT_ID, slug="salon-1828-x", name="Чужой салон")
        r = client.post(URL, _body(), format="json")
        assert r.status_code == 409, r.content
        assert r.json()["error"]["details"]["reason"] == "tenant_id_taken"
        assert Tenant.all_objects.get(id=TENANT_ID).kind == Tenant.Kind.SALON

    def test_a_claim_bound_to_another_workspace_is_409(self, client):
        assert client.post(URL, _body(), format="json").status_code == 201
        r = client.post(
            URL, _body(tenant_id=str(uuid.uuid4()), slug="solo-max-1828zzzz"), format="json"
        )
        assert r.status_code == 409, r.content
        assert r.json()["error"]["details"]["reason"] == "claim_bound_elsewhere"
        assert Tenant.all_objects.filter(slug="solo-max-1828zzzz").count() == 0

    def test_a_malformed_external_id_is_refused_before_any_write(self, client):
        r = client.post(URL, _body(external_user_id="not-an-external-id"), format="json")
        assert r.status_code == 409, r.content
        assert r.json()["error"]["details"]["reason"] == "invalid_external_user_id"
        assert _counts() == {"tenants": 0, "users": 0, "profiles": 0}


class TestLinkedIsOneWriteOnTheProxyNotASecondProfile:
    def test_operator_bind_lands_on_the_provisioned_profile(self, client, settings):
        """G1: при LINKED новый профиль не создаётся — прокси получает
        ``linked_user`` на рабочий W, и ``me/identity`` отдаёт W."""

        data = client.post(URL, _body(), format="json").json()["data"]
        w = User.objects.get(id=data["user_id"])

        # Бот представился — каталог завёл прокси лениво (как на пилоте).
        proxy = resolve_external_user(EXTERNAL)
        assert proxy.is_proxy is True and proxy.pk != w.pk
        profiles_before = SpecialistProfile.objects.count()

        operator = User.objects.create_superuser(
            username="op-1828", password="pw", email="op-1828@x.y", role="admin",  # pragma: allowlist secret
        )
        bind_external_identity_by_operator(EXTERNAL, w.id, actor=operator)

        proxy.refresh_from_db()
        assert proxy.linked_user_id == w.pk
        assert SpecialistProfile.objects.count() == profiles_before  # второго профиля нет
        assert SpecialistProfile.objects.get(user=w).id == uuid.UUID(data["specialist_id"])
        assert resolve_external_user(EXTERNAL).pk == w.pk

        bot = APIClient()
        bot.credentials(HTTP_AUTHORIZATION=f"Bearer {GENERAL}", HTTP_X_EXTERNAL_USER_ID=EXTERNAL)
        r = bot.get(IDENTITY_URL)
        assert r.status_code == 200, r.content
        assert r.json()["data"] == {"ayla_user_id": str(w.id), "is_proxy": False}


class TestTheClaimIsNotASecondIdentityGraph:
    ALLOWED_READERS = {
        "tenants/solo_provisioning.py",
        "users/models.py",  # определение поля
    }

    def test_only_the_provisioning_service_reads_the_claim(self):
        """Риск 1 pre-flight: если claim начнут читать резолверы личности
        или сторожа без условия «прокси не связан», pre-LINKED станет identity
        authority. Список читателей закрытый; миграции и тесты не считаются.
        M28 добавит сюда ровно один класс — permission provisioned workspace."""

        root = Path(__file__).resolve().parents[2]
        pattern = re.compile(r"provisioned_external_user_id")
        readers: set[str] = set()
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if "/migrations/" in rel or "/tests/" in rel or rel.startswith("tests/"):
                continue
            if ".venv" in rel or rel.startswith("scripts/"):
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                readers.add(rel)
        assert readers == self.ALLOWED_READERS, sorted(readers)
        # Положительная стража: сам сторож видит хотя бы настоящих читателей.
        assert "tenants/solo_provisioning.py" in readers
