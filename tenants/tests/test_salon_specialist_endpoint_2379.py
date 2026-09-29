"""Ручка ``POST /internal/tenants/salon-specialists/`` (DRF-2379).

Служба проверена соседним файлом; здесь — сама дверь: право, коды исходов и
то, что повтор по проводу возвращает того же специалиста.

Почему повтор проверяется ещё и через HTTP, а не только у службы: бот зовёт
эту ручку из обработчика заведения мастера и **вправе повторить** её после
обрыва. Если повтор родит второго специалиста, у салона появится второй
человек в расписании — не «лишняя строка», а лишний мастер.
"""

from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from tenants.models import Tenant
from users.models import SpecialistProfile

pytestmark = pytest.mark.django_db

URL = "/api/v1/internal/tenants/salon-specialists/"

PROVISIONING = "test-tenant-provisioning-2379"
IDENTITY = "test-identity-provisioning-2379"
GENERAL = "test-general-bot-token-2379"

CLAIM = "bot:max:2379777"


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


@pytest.fixture
def salon() -> Tenant:
    return Tenant.all_objects.create(
        id=uuid.uuid4(),
        slug="salon-2379-http",
        name="Салон 2379",
        city="Москва",
        kind=Tenant.Kind.SALON,
    )


def _body(salon: Tenant, claim: str = CLAIM) -> dict:
    return {
        "tenant_id": str(salon.id),
        "external_user_id": claim,
        "display_name": "Лера",
    }


class TestДверьЗаводитСпециалиста:
    def test_201_и_каталожный_ключ_в_ответе(self, client, salon: Tenant) -> None:
        resp = client.post(URL, _body(salon), format="json")

        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["tenant_id"] == str(salon.id)
        assert uuid.UUID(data["specialist_id"])  # это и есть ключ для бота

    def test_ключ_совпадает_с_заведённым_профилем(self, client, salon: Tenant) -> None:
        resp = client.post(URL, _body(salon), format="json")

        profile = SpecialistProfile.objects.get(tenant=salon)
        assert resp.json()["data"]["specialist_id"] == str(profile.id)


class TestПовторПоПроводуНеРождаетВторого:
    def test_второй_вызов_отвечает_200_тем_же_ключом(self, client, salon: Tenant) -> None:
        first = client.post(URL, _body(salon), format="json")
        second = client.post(URL, _body(salon), format="json")

        assert first.status_code == 201
        assert second.status_code == 200
        assert (
            second.json()["data"]["specialist_id"]
            == first.json()["data"]["specialist_id"]
        )

    def test_в_салоне_остался_один_специалист(self, client, salon: Tenant) -> None:
        client.post(URL, _body(salon), format="json")
        client.post(URL, _body(salon), format="json")

        assert SpecialistProfile.objects.filter(tenant=salon).count() == 1


class TestИсходыРазличимыКодом:
    def test_салона_нет_404(self, client) -> None:
        body = {
            "tenant_id": str(uuid.uuid4()),
            "external_user_id": CLAIM,
            "display_name": "Лера",
        }

        resp = client.post(URL, body, format="json")

        assert resp.status_code == 404
        assert resp.json()["error"]["details"]["reason"] == "tenant_not_found"

    def test_кабинет_соло_409(self, client) -> None:
        solo = Tenant.all_objects.create(
            id=uuid.uuid4(), slug="solo-2379-http", name="Соло", kind=Tenant.Kind.SOLO,
        )

        resp = client.post(
            URL,
            {
                "tenant_id": str(solo.id),
                "external_user_id": CLAIM,
                "display_name": "Лера",
            },
            format="json",
        )

        assert resp.status_code == 409
        assert resp.json()["error"]["details"]["reason"] == "tenant_is_solo"

    def test_claim_занят_другим_салоном_409(self, client, salon: Tenant) -> None:
        other = Tenant.all_objects.create(
            id=uuid.uuid4(), slug="salon-2379-http-b", name="Другой",
            kind=Tenant.Kind.SALON,
        )
        client.post(URL, _body(salon), format="json")

        resp = client.post(URL, _body(other), format="json")

        assert resp.status_code == 409
        assert resp.json()["error"]["details"]["reason"] == "claim_bound_elsewhere"


class TestПравоТоЖе:
    """Граница токена — та же, что у соседей-provisioning."""

    def test_общий_токен_бота_отвергается_и_ничего_не_создаёт(
        self, tokens, salon: Tenant,
    ) -> None:
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {GENERAL}")

        resp = c.post(URL, _body(salon), format="json")

        assert resp.status_code == 403
        assert not SpecialistProfile.objects.filter(tenant=salon).exists()

    def test_identity_токен_отвергается(self, tokens, salon: Tenant) -> None:
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {IDENTITY}")

        assert c.post(URL, _body(salon), format="json").status_code == 403

    def test_без_токена_отвергается(self, tokens, salon: Tenant) -> None:
        assert APIClient().post(URL, _body(salon), format="json").status_code == 403
