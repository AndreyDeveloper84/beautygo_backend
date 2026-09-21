"""Чтение вида тенанта для бота — единственный источник «чьё место и кто ведёт услуги» (DRF-2254).

Бот решал «соло» подсчётом людей (``is_solo_provider``), каталог — признаком
``Tenant.kind``; экраны самообслуживания мастера бот открывал по первому, а
каталог разрешал или отказывал по второму. Решение: источник один —
``Tenant.kind`` каталога; бот читает его этой ручкой и отдаёт в ``/me`` как
``workspace_kind``.

* k1 — соло-workspace → ``kind=solo``; салон → ``kind=salon``;
* k2 — тенанта нет → 404 ``TENANT_NOT_FOUND``;
* k3 — только общий внутренний токен бота: без токена, с чужим и с
  токеном provisioning — отказ (чтение не даёт права заводить тенанты, и
  наоборот);
* k4 — только чтение: POST/PATCH/DELETE не принимаются.
"""

from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from tenants.models import Tenant
from tenants.solo_provisioning import provision_solo_workspace

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token-2254"  # noqa: S105  # pragma: allowlist secret
PROVISIONING_TOKEN = "test-provisioning-token-2254"  # noqa: S105  # pragma: allowlist secret
URL = "/api/v1/internal/tenants/{tid}/kind/"


@pytest.fixture(autouse=True)
def _settings(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN
    settings.AYLA_TENANT_PROVISIONING_TOKEN = PROVISIONING_TOKEN


def _client(token: str | None = RUNTIME_TOKEN) -> APIClient:
    c = APIClient()
    if token is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    return c


@pytest.fixture
def solo() -> Tenant:
    return provision_solo_workspace(
        tenant_id=uuid.uuid4(), slug="solo-max-2254", name="Студия 2254", city="Пенза",
        external_user_id="bot:max:2254001", display_name="Мастер",
    ).tenant


@pytest.fixture
def salon() -> Tenant:
    return Tenant.objects.create(slug="salon-2254", name="Салон 2254", city="Пенза")


class TestK1Kind:
    def test_solo_workspace_is_solo(self, solo) -> None:
        resp = _client().get(URL.format(tid=solo.pk))
        assert resp.status_code == 200, resp.content
        assert resp.json()["data"] == {"id": str(solo.pk), "kind": "solo"}

    def test_salon_is_salon(self, salon) -> None:
        resp = _client().get(URL.format(tid=salon.pk))
        assert resp.status_code == 200, resp.content
        assert resp.json()["data"] == {"id": str(salon.pk), "kind": "salon"}


class TestK2Unknown:
    def test_unknown_tenant_is_404(self) -> None:
        resp = _client().get(URL.format(tid=uuid.uuid4()))
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "TENANT_NOT_FOUND"


class TestK3OnlyTheBotsInternalToken:
    @pytest.mark.parametrize("token", [None, "wrong", PROVISIONING_TOKEN])
    def test_other_credentials_are_refused(self, salon, token) -> None:
        resp = _client(token).get(URL.format(tid=salon.pk))
        assert resp.status_code in (401, 403)
        assert "kind" not in resp.content.decode()


class TestK4ReadOnly:
    @pytest.mark.parametrize("method", ["post", "patch", "put", "delete"])
    def test_writes_are_not_allowed(self, salon, method) -> None:
        resp = getattr(_client(), method)(URL.format(tid=salon.pk), {"kind": "solo"}, format="json")
        assert resp.status_code == 405
        salon.refresh_from_db()
        assert salon.kind == Tenant.Kind.SALON
