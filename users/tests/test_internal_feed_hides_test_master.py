"""Внутренний список мастеров не отдаёт мастера тестового набора.

Решение владельца 10.10.2026: тестовые мастера исключены из обычной выдачи
боту и клиентам независимо от статуса подтверждения; санкционированная
сквозная проверка идёт изолированным путём и доступ сохраняет.

«Тестовый мастер» — мастер, у которого есть предложение по услуге с пометкой
«синтетика» (``users.sellable.synthetic_offer_master_q``). Набор засевается настоящей
командой засева — той же, что пойдёт на стенд.

Что держат узлы:

* список (он же фид зеркала бота и живой запрос мастеров салона) и карточка
  по идентификатору тест-мастера не отдают — никому и при любом состоянии
  профиля;
* фильтр не шире признака: настоящий мастер того же демо-салона приходит
  как раньше;
* тестовый путь жив: слоты и услуги тест-мастера по идентификатору отдаются.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from services.models import SalonService, SpecialistService
from services.tests.test_synthetic_rows_leak_sweep import TOKEN, seeded  # noqa: F401 — засеянный набор
from tenants.models import Tenant
from users.models import SpecialistProfile, User
from users.sellable import synthetic_offer_master_q

pytestmark = pytest.mark.django_db

LIST_URL = "/api/v1/internal/specialists/"
Status = SpecialistProfile.ProfileStatus


def _api(external_user_id: str | None = None) -> APIClient:
    client = APIClient()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
    if external_user_id is not None:
        client.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return client


def _listed(client: APIClient, **query) -> set[str]:
    response = client.get(LIST_URL, query)
    assert response.status_code == 200, response.content[:300]
    payload = response.json()
    rows = payload["results"] if isinstance(payload, dict) and "results" in payload else payload
    assert isinstance(rows, list), payload
    return {str(row["id"]) for row in rows}


@pytest.fixture
def demo_salon(seeded) -> Tenant:  # noqa: F811
    return Tenant.all_objects.get(pk=seeded["query"]["tenant"])


@pytest.fixture
def real_master(demo_salon) -> SpecialistProfile:
    """Обычный мастер ТОГО ЖЕ демо-салона — такие на стенде есть, и фильтр их не касается."""
    user = User.objects.create_user(username="feed-real-demo-master", password="x", role="specialist")
    SpecialistProfile.objects.filter(user=user).update(
        tenant=demo_salon, display_name="Настоящий демо-мастер", status=Status.ACTIVE,
        is_available=True, is_booking_enabled=True,
    )
    return SpecialistProfile.objects.get(user=user)


def _callers(seeded) -> dict[str, APIClient]:  # noqa: F811
    bot_plain = User.objects.create_user(
        username="bot:feed-plain", password="x", role="client", phone="+79995550011", is_proxy=True,
    )
    bot_persona = User.objects.create_user(
        username="bot:feed-persona", password="x", role="client", phone="+79995550012", is_proxy=True,
        is_test_persona=True,
    )
    return {
        "сервисный токен (синк зеркала)": _api(),
        "бот от имени обычного человека": _api(bot_plain.username),
        "бот от имени тестовой персоны": _api(bot_persona.username),
    }


class TestTheTestMasterIsNotInTheFeed:
    def test_the_seeded_master_is_a_test_master_by_the_predicate(self, seeded, real_master) -> None:  # noqa: F811
        """Положительный контроль признака: иначе «не пришёл» мог бы значить «не засеян»."""
        test_masters = set(map(str, SpecialistProfile.objects.filter(synthetic_offer_master_q()).values_list("pk", flat=True)))
        assert test_masters == {seeded["master_id"]}

    def test_no_caller_gets_him_in_the_list(self, seeded, real_master) -> None:  # noqa: F811
        tenant = seeded["query"]["tenant"]
        for who, client in _callers(seeded).items():
            for query in ({}, {"tenant": tenant}):
                listed = _listed(client, **query)
                assert seeded["master_id"] not in listed, (who, query)
                assert str(real_master.pk) in listed, (who, query)

    def test_no_caller_gets_his_card(self, seeded, real_master) -> None:  # noqa: F811
        for who, client in _callers(seeded).items():
            assert client.get(f"{LIST_URL}{seeded['master_id']}/").status_code == 404, who
            assert client.get(f"{LIST_URL}{real_master.pk}/").status_code == 200, who

    @pytest.mark.parametrize(
        "state",
        [
            {"status": Status.ACTIVE, "is_available": True, "is_booking_enabled": True},
            {"status": Status.ACTIVE, "is_available": True, "is_booking_enabled": False},  # приём на паузе
            {"status": Status.PENDING, "is_available": True, "is_booking_enabled": True},  # на верификации
            {"status": Status.DRAFT, "is_available": True, "is_booking_enabled": False},
        ],
    )
    def test_no_state_of_the_profile_brings_him_back(self, seeded, state) -> None:  # noqa: F811
        """Независимо от статуса подтверждения — слова владельца."""
        SpecialistProfile.objects.filter(pk=seeded["master_id"]).update(**state)
        assert seeded["master_id"] not in _listed(_api(), tenant=seeded["query"]["tenant"])
        assert _api().get(f"{LIST_URL}{seeded['master_id']}/").status_code == 404

    def test_a_master_with_one_real_and_one_synthetic_offer_is_hidden_too(
        self, seeded, real_master, demo_salon,  # noqa: F811
    ) -> None:
        """Смешанный случай решён в сторону «скрыть»: синтетическое предложение делает мастера тестовым."""
        synthetic_offer = SalonService.objects.get(synthetic=True)
        real_offer = SalonService.objects.create(
            tenant=demo_salon, category=synthetic_offer.category, name="Настоящая услуга демо-салона",
            duration_minutes=60, base_price=Decimal("1000"),
        )
        SpecialistService.objects.create(salon_service=real_offer, specialist=real_master, price=Decimal("1000"))
        assert str(real_master.pk) in _listed(_api())  # одно настоящее предложение — обычный мастер

        SpecialistService.objects.create(
            salon_service=synthetic_offer, specialist=real_master, price=synthetic_offer.base_price,
        )
        assert str(real_master.pk) not in _listed(_api())
        assert _api().get(f"{LIST_URL}{real_master.pk}/").status_code == 404


class TestTheSanctionedTestPathStaysOpen:
    """Кандидаты шага отдают идентификаторы под серверным разрешением; дальше — по идентификатору."""

    def test_his_slots_for_the_synthetic_offer_still_come(self, seeded) -> None:  # noqa: F811
        offer = SalonService.objects.get(synthetic=True)
        day = (timezone.localdate() + timedelta(days=2)).isoformat()
        response = _api().get(
            f"{LIST_URL}{seeded['master_id']}/slots/", {"service_id": str(offer.pk), "date": day},
        )
        assert response.status_code == 200, response.content[:300]
        payload = response.json()
        slots = payload.get("slots") if "slots" in payload else (payload.get("data") or {}).get("slots")
        assert slots, payload

    def test_his_services_by_id_still_answer(self, seeded) -> None:  # noqa: F811
        assert _api().get(f"{LIST_URL}{seeded['master_id']}/services/").status_code == 200
