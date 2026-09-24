"""Специалист заводится в СУЩЕСТВУЮЩЕМ салоне, и повтор не рождает второго (DRF-2379).

Решение владельца §77 п.27: привязка мастера к каталогу происходит сама, когда
салон заводит мастера. До этого листа она была тремя шагами в двух системах:
профиль специалиста заводился в админке каталога, ключ приезжал синком, и
действие повторялось в админке бота.

Почему нельзя было взять соседний ``solo_provisioning``: он заводит **тенант
целиком**, а у салона он уже есть — второй завёл бы параллельный кабинет с
теми же людьми. Здесь тенант ищется, и его отсутствие — отказ.

Что стерегут узлы ниже:

* специалист заводится в уже существующем салоне, с ролью ``staff``;
* **повтор возвращает того же** и ничего не создаёт — это требование главного
  окна и защита от повтора бота после обрыва: второй специалист был бы не
  «лишней строкой», а вторым человеком в расписании салона;
* claim, занятый специалистом другого салона, — отказ с именем, а не
  молчаливое переселение человека между салонами;
* соло-кабинет этой дверью не обслуживается;
* рабочий аккаунт — не прокси и вне пространства ``bot:`` (иначе привязка
  личности ответит ``collision``, как и у соло).
"""

from __future__ import annotations

import uuid

import pytest

from tenants.models import Tenant
from tenants.salon_specialist_provisioning import (
    SalonSpecialistRefused,
    provision_salon_specialist,
)
from users.models import SpecialistProfile, TenantUserRelationship

pytestmark = pytest.mark.django_db

CLAIM = "bot:max:2379001"


@pytest.fixture
def salon() -> Tenant:
    return Tenant.all_objects.create(
        id=uuid.uuid4(),
        slug="salon-2379",
        name="Салон 2379",
        city="Москва",
        kind=Tenant.Kind.SALON,
    )


class TestСпециалистЗаводитсяВСуществующемСалоне:
    def test_профиль_создан_и_принадлежит_салону(self, salon: Tenant) -> None:
        result = provision_salon_specialist(
            tenant_id=salon.id, external_user_id=CLAIM, display_name="Лера",
        )

        assert result.created is True
        assert result.profile.tenant_id == salon.id
        assert result.profile.display_name == "Лера"

    def test_роль_staff_а_не_admin(self, salon: Tenant) -> None:
        """Специалист салона не администрирует салон — этим он и отличается от соло."""
        result = provision_salon_specialist(
            tenant_id=salon.id, external_user_id=CLAIM, display_name="Лера",
        )

        tur = TenantUserRelationship.objects.get(
            user=result.profile.user, tenant=salon, is_active=True,
        )
        assert tur.role == TenantUserRelationship.Role.STAFF

    def test_рабочий_аккаунт_не_прокси_и_вне_пространства_бота(self, salon: Tenant) -> None:
        result = provision_salon_specialist(
            tenant_id=salon.id, external_user_id=CLAIM, display_name="Лера",
        )

        user = result.profile.user
        assert user.is_proxy is False
        assert not user.username.startswith("bot:")

    def test_кабинет_соло_этой_дверью_не_заводится(self, salon: Tenant) -> None:
        """Отличие от `solo_provisioning`: тенант ищется, а не создаётся.

        Считать ВСЕ тенанты нельзя: корневая фикстура заводит
        `test-default-tenant` лениво, и счётчик поймал бы её, а не предмет —
        первый заход этого узла именно на этом и покраснел. Проверяется то,
        чего дверь не должна делать: рождать кабинет соло-мастера.
        """
        solo_before = Tenant.all_objects.filter(kind=Tenant.Kind.SOLO).count()

        result = provision_salon_specialist(
            tenant_id=salon.id, external_user_id=CLAIM, display_name="Лера",
        )

        assert Tenant.all_objects.filter(kind=Tenant.Kind.SOLO).count() == solo_before
        assert result.profile.user.tenant_id == salon.id


class TestПовторНеРождаетВторого:
    """Требование главного окна: второй вызов возвращает того же."""

    def test_второй_вызов_возвращает_того_же_специалиста(self, salon: Tenant) -> None:
        first = provision_salon_specialist(
            tenant_id=salon.id, external_user_id=CLAIM, display_name="Лера",
        )
        second = provision_salon_specialist(
            tenant_id=salon.id, external_user_id=CLAIM, display_name="Лера",
        )

        assert second.created is False
        assert second.profile.id == first.profile.id

    def test_второго_профиля_в_салоне_не_появилось(self, salon: Tenant) -> None:
        provision_salon_specialist(
            tenant_id=salon.id, external_user_id=CLAIM, display_name="Лера",
        )
        provision_salon_specialist(
            tenant_id=salon.id, external_user_id=CLAIM, display_name="Лера",
        )

        assert SpecialistProfile.objects.filter(tenant=salon).count() == 1

    def test_повтор_не_переписывает_имя(self, salon: Tenant) -> None:
        """На существующей строке здесь не обновляется ничего — как и у соло."""
        provision_salon_specialist(
            tenant_id=salon.id, external_user_id=CLAIM, display_name="Лера",
        )
        second = provision_salon_specialist(
            tenant_id=salon.id, external_user_id=CLAIM, display_name="Другое имя",
        )

        assert second.profile.display_name == "Лера"


class TestОтказыИмеютИмя:
    def test_салона_нет(self) -> None:
        with pytest.raises(SalonSpecialistRefused) as exc:
            provision_salon_specialist(
                tenant_id=uuid.uuid4(), external_user_id=CLAIM, display_name="Лера",
            )

        assert exc.value.reason == "tenant_not_found"

    def test_это_кабинет_соло_мастера(self) -> None:
        solo = Tenant.all_objects.create(
            id=uuid.uuid4(), slug="solo-2379", name="Соло", kind=Tenant.Kind.SOLO,
        )

        with pytest.raises(SalonSpecialistRefused) as exc:
            provision_salon_specialist(
                tenant_id=solo.id, external_user_id=CLAIM, display_name="Лера",
            )

        assert exc.value.reason == "tenant_is_solo"

    def test_claim_занят_специалистом_другого_салона(self, salon: Tenant) -> None:
        """Молча переселять человека между салонами нельзя."""
        other = Tenant.all_objects.create(
            id=uuid.uuid4(), slug="salon-2379-b", name="Другой", kind=Tenant.Kind.SALON,
        )
        provision_salon_specialist(
            tenant_id=salon.id, external_user_id=CLAIM, display_name="Лера",
        )

        with pytest.raises(SalonSpecialistRefused) as exc:
            provision_salon_specialist(
                tenant_id=other.id, external_user_id=CLAIM, display_name="Лера",
            )

        assert exc.value.reason == "claim_bound_elsewhere"

    def test_claim_не_той_формы(self, salon: Tenant) -> None:
        with pytest.raises(SalonSpecialistRefused) as exc:
            provision_salon_specialist(
                tenant_id=salon.id, external_user_id="не-claim", display_name="Лера",
            )

        assert exc.value.reason == "invalid_external_user_id"
