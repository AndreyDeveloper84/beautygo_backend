"""``POST /api/v1/internal/tenants/`` — салон по slug для бота (DRF-1525).

Владелец 11.09.2026: экран «подключить салон» в боте = slug + название
+ город + кнопка; UUID человек не вводит и не видит. Значит UUID обязан
приходить отсюда, и обязан приходить ОДИН на slug.

Сторож — провижининг-токен §11. Его отказ проверяется здесь положительно
и по каждому исходу отдельно: пустой токен → 403, а не 500 (ручка не
падает) и не 201 (ручка не заводит). Один тест «403» не отличил бы
«сторож работает» от «ручка сломана и падает до сторожа».
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.db import IntegrityError
from rest_framework.test import APIClient

from tenants.models import Tenant

pytestmark = pytest.mark.django_db

URL = "/api/v1/internal/tenants/"
# Не «formula-tela»: этот slug заводит seed-миграция, и тест «первый вызов
# создаёт» на нём получал бы 200 от чужой строки.
SLUG = "salon-u-olgi-1525"
NAME = "Салон у Ольги"
PROVISIONING = "test-tenant-provisioning-1525"
IDENTITY = "test-identity-provisioning-1525"
GENERAL = "test-general-bot-token-1525"


@pytest.fixture
def tokens(settings):
    # DRF-1695 (C1): три секрета, три силы. Ручка тенантов — под своим.
    settings.AYLA_TENANT_PROVISIONING_TOKEN = PROVISIONING
    settings.AYLA_IDENTITY_PROVISIONING_TOKEN = IDENTITY
    settings.AYLA_INTERNAL_API_TOKEN = GENERAL


@pytest.fixture
def client(tokens):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {PROVISIONING}")
    return c


def _body(**over):
    base = {"slug": SLUG, "name": NAME, "city": "Пенза"}
    base.update(over)
    return base


class TestTheGuardRefusesByName:
    def test_an_empty_token_is_403_not_500_and_creates_nothing(self, settings):
        """Положительная проба сторожа (требование главного окна).

        Три утверждения, а не одно: код 403 (не 500 — ручка не упала до
        сторожа; не 201 — ручка не завела), и в базе пусто.
        """
        settings.AYLA_TENANT_PROVISIONING_TOKEN = ""
        settings.AYLA_IDENTITY_PROVISIONING_TOKEN = ""
        settings.AYLA_INTERNAL_API_TOKEN = GENERAL
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Bearer anything")

        r = c.post(URL, _body(), format="json")

        assert r.status_code == 403, r.content
        assert Tenant.all_objects.filter(slug=SLUG).count() == 0

    def test_the_general_bot_token_is_refused(self, tokens):
        """Право заводить салоны — не право читать зеркало."""
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {GENERAL}")

        r = c.post(URL, _body(), format="json")

        assert r.status_code == 403, r.content
        assert Tenant.all_objects.filter(slug=SLUG).count() == 0

    def test_no_bearer_at_all_is_403(self, tokens):
        r = APIClient().post(URL, _body(), format="json")
        assert r.status_code == 403, r.content

    def test_the_identity_secret_does_not_open_tenants_once_the_tenant_secret_exists(self, tokens):
        """DRF-1695 (C1): одна сила — один секрет. Identity-токен здесь чужой."""
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {IDENTITY}")

        r = c.post(URL, _body(), format="json")

        assert r.status_code == 403, r.content
        assert Tenant.all_objects.filter(slug=SLUG).count() == 0

    def test_an_empty_tenant_secret_closes_the_route_to_the_identity_secret_too(
        self, settings
    ):
        """DRF-1828 (M27, A3): переходное правило DRF-1695 снято.

        Пока ``AYLA_TENANT_PROVISIONING_TOKEN`` не задан, ручка закрыта ВСЕМ —
        identity-токен её больше не открывает: тот же сторож вот-вот начнёт
        заводить ``User(specialist)``, и identity-полномочие не имеет права
        быть provisioning-полномочием даже «на одну выкладку». Положительная
        стража на тех же данных: с заданным tenant-секретом ручка заводит.
        """
        settings.AYLA_TENANT_PROVISIONING_TOKEN = ""
        settings.AYLA_IDENTITY_PROVISIONING_TOKEN = IDENTITY
        settings.AYLA_INTERNAL_API_TOKEN = GENERAL
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {IDENTITY}")

        assert c.post(URL, _body(), format="json").status_code == 403
        assert Tenant.all_objects.filter(slug=SLUG).count() == 0

        settings.AYLA_TENANT_PROVISIONING_TOKEN = PROVISIONING
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {PROVISIONING}")
        assert c.post(URL, _body(), format="json").status_code == 201
        # И identity-токен по-прежнему чужой — как и до снятия правила.
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {IDENTITY}")
        assert c.post(URL, _body(slug="vtoroy-1525"), format="json").status_code == 403

    def test_equal_secrets_fail_closed_per_request(self, settings):
        """Совпали tenant и identity — снова одна сила; ручка отказывает всем."""
        settings.AYLA_TENANT_PROVISIONING_TOKEN = IDENTITY
        settings.AYLA_IDENTITY_PROVISIONING_TOKEN = IDENTITY
        settings.AYLA_INTERNAL_API_TOKEN = GENERAL
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {IDENTITY}")

        assert c.post(URL, _body(), format="json").status_code == 403
        assert Tenant.all_objects.filter(slug=SLUG).count() == 0


class TestTheCallIsIdempotentBySlug:
    def test_first_call_creates_and_returns_the_uuid(self, client):
        """Положительная стража сторожа: с верным токеном ручка ЗАВОДИТ.

        Без неё тесты выше зеленели бы и на ручке, которая отказывает
        всем, — то есть на выключенной ручке вместо сторожа.
        """
        r = client.post(URL, _body(), format="json")

        assert r.status_code == 201, r.content
        row = Tenant.all_objects.get(slug=SLUG)
        data = r.json()["data"]
        assert data == {
            "id": str(row.id),
            "slug": SLUG,
            "name": NAME,
            "city": "Пенза",
            "is_active": True,
        }

    def test_repeat_with_the_same_name_is_200_with_the_same_uuid(self, client):
        first = client.post(URL, _body(), format="json").json()["data"]

        second = client.post(URL, _body(), format="json")

        assert second.status_code == 200, second.content
        assert second.json()["data"]["id"] == first["id"]
        assert Tenant.all_objects.filter(slug=SLUG).count() == 1

    def test_an_existing_salon_made_in_the_admin_is_found_not_duplicated(self, client):
        """Главный случай пилота: салон завели в админке Ayla раньше."""
        row = Tenant.all_objects.create(slug=SLUG, name=NAME)

        r = client.post(URL, _body(city="Пенза"), format="json")

        assert r.status_code == 200, r.content
        assert r.json()["data"]["id"] == str(row.id)
        row.refresh_from_db()
        # Существующая строка не правится: город остался пустым и уехал
        # как null, а не как «Пенза» из запроса.
        assert row.city == ""
        assert r.json()["data"]["city"] is None

    def test_the_same_slug_with_another_name_is_409_naming_the_owner(self, client):
        Tenant.all_objects.create(slug=SLUG, name=NAME)

        r = client.post(URL, _body(name="Другой салон"), format="json")

        assert r.status_code == 409, r.content
        err = r.json()["error"]
        assert err["code"] == "TENANT_SLUG_TAKEN"
        assert err["details"]["existing_name"] == NAME
        assert err["details"]["requested_name"] == "Другой салон"
        assert Tenant.all_objects.filter(slug=SLUG).count() == 1

    def test_an_inactive_salon_is_returned_as_inactive_not_recreated(self, client):
        """Выключенный салон — не «нет салона»: второй строки не будет."""
        row = Tenant.all_objects.create(
            slug=SLUG, name=NAME, is_active=False,
        )

        r = client.post(URL, _body(), format="json")

        assert r.status_code == 200, r.content
        assert r.json()["data"]["id"] == str(row.id)
        assert r.json()["data"]["is_active"] is False

    def test_a_lost_race_reads_the_winner_row(self, client):
        """Два экрана нажали одновременно: проигравший получает строку
        победителя, а не 500 и не вторую строку."""
        from tenants import provisioning

        winner = Tenant.all_objects.create(slug=SLUG, name=NAME)

        class _Empty:
            def first(self):
                return None

        with patch.object(
            provisioning.Tenant.all_objects, "filter", return_value=_Empty()
        ), patch.object(
            provisioning.Tenant.all_objects, "create",
            side_effect=IntegrityError("duplicate key value violates unique constraint"),
        ):
            r = client.post(URL, _body(), format="json")

        assert r.status_code == 200, r.content
        assert r.json()["data"]["id"] == str(winner.id)
        # Подмена снята: обычное создание после пробы работает.
        assert Tenant.all_objects.create(slug="after-race-1525", name="x").pk


class TestTheRequestShape:
    def test_a_bad_slug_is_400_and_creates_nothing(self, client):
        r = client.post(URL, _body(slug="Формула Тела"), format="json")
        assert r.status_code == 400, r.content
        assert not Tenant.all_objects.filter(name=NAME).exists()

    def test_city_is_optional(self, client):
        body = _body()
        del body["city"]

        r = client.post(URL, body, format="json")

        assert r.status_code == 201, r.content
        assert r.json()["data"]["city"] is None


class TestTheBootCheckRefusesOnePowerUnderTwoNames:
    def _errors(self):
        from users.checks import tenant_provisioning_token_check

        return {e.id for e in tenant_provisioning_token_check(None)}

    def test_distinct_secrets_are_clean(self, settings):
        settings.AYLA_TENANT_PROVISIONING_TOKEN = PROVISIONING
        settings.AYLA_IDENTITY_PROVISIONING_TOKEN = IDENTITY
        settings.AYLA_INTERNAL_API_TOKEN = GENERAL
        assert self._errors() == set()

    def test_empty_tenant_secret_is_clean(self, settings):
        settings.AYLA_TENANT_PROVISIONING_TOKEN = ""
        settings.AYLA_IDENTITY_PROVISIONING_TOKEN = IDENTITY
        settings.AYLA_INTERNAL_API_TOKEN = GENERAL
        assert self._errors() == set()

    def test_equal_to_general_is_E002_and_equal_to_identity_is_E003(self, settings):
        settings.AYLA_TENANT_PROVISIONING_TOKEN = GENERAL
        settings.AYLA_IDENTITY_PROVISIONING_TOKEN = IDENTITY
        settings.AYLA_INTERNAL_API_TOKEN = GENERAL
        assert self._errors() == {"users.E002"}

        settings.AYLA_TENANT_PROVISIONING_TOKEN = IDENTITY
        assert self._errors() == {"users.E003"}
