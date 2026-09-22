"""Выгрузка по ст. 14 несёт избранных мастеров (DRF-2214, PR-2b).

«Забудь всё» избранных мастеров оставляет (CD §72 п.3, ``KEPT_BY_FORGET_ALL``):
это выбор человека в приложении BeautyGO. Но то, что остаётся, человек тоже
вправе увидеть в своей выгрузке — ст. 14 о составе обрабатываемых данных, а не
о том, что стирается. Раздел ``favorite_specialists``: какой мастер и когда
добавлен. Поля — закрытым списком, как у остальных разделов
(``users.remembered_export.FIELDS``).
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from users.models import FavoriteSpecialist
from users.tests.test_memory_erasure_matrix import (  # noqa: F401 — фикстуры по имени
    _internal,
    _set_token,
    user,
)

User = get_user_model()
pytestmark = pytest.mark.django_db

EXPORT_URL = "/api/v1/internal/users/{user_id}/personal-data/export/"


def _export(u) -> dict:
    resp = _internal().get(EXPORT_URL.format(user_id=u.pk))
    assert resp.status_code == 200, resp.content
    return resp.json()["data"]


def _master(tag: str, name: str):
    master = User.objects.create_user(
        username=f"fav2214_{tag}", password="x", role="specialist", phone=f"+7999062215{tag}"
    )
    sp = master.specialist_profile
    sp.display_name = name
    sp.status = "active"
    sp.save()
    return sp


class TestTheFavouritesAreExported:
    def test_which_master_and_when(self, user) -> None:  # noqa: F811
        sp = _master("1", "Анна Маникюр")
        fav = FavoriteSpecialist.objects.create(user=user, specialist=sp)

        favourites = _export(user)["favorite_specialists"]

        assert [f["specialist"]["display_name"] for f in favourites] == ["Анна Маникюр"]
        assert favourites[0]["specialist"]["id"] == str(sp.pk)
        assert favourites[0]["created_at"] == fav.created_at.isoformat()

    def test_empty_for_a_person_with_none(self, user) -> None:  # noqa: F811
        data = _export(user)

        assert "profile" in data  # наличие: выгрузка та самая
        assert data["favorite_specialists"] == []

    def test_a_linked_proxy_carries_its_own(self, user) -> None:  # noqa: F811
        proxy = User.objects.create(
            username="bot:max:fav2214-proxy", role="client", is_proxy=True, linked_user=user
        )
        FavoriteSpecialist.objects.create(user=proxy, specialist=_master("2", "Ольга Брови"))

        data = _export(user)

        linked = {item["external_user_id"]: item for item in data["linked_identities"]}
        assert [f["specialist"]["display_name"] for f in linked[proxy.username]["favorite_specialists"]] == [
            "Ольга Брови"
        ]
        assert data["favorite_specialists"] == []

    def test_a_neighbours_favourites_are_not_exported(self, user) -> None:  # noqa: F811
        neighbour = User.objects.create_user(
            username="fav2214_neighbour", password="x", role="client", phone="+79995559005"
        )
        FavoriteSpecialist.objects.create(user=neighbour, specialist=_master("3", "Чужой мастер"))
        FavoriteSpecialist.objects.create(user=user, specialist=_master("4", "Свой мастер"))

        data = _export(user)

        assert [f["specialist"]["display_name"] for f in data["favorite_specialists"]] == ["Свой мастер"]
        assert "Чужой мастер" not in repr(data)


class TestTheFieldsAreDecided:
    def test_the_model_is_classified(self) -> None:
        """Поле модели без решения краснит общий сторож классификации — модель обязана быть в FIELDS."""
        from users.remembered_export import FIELDS

        assert FIELDS  # наличие
        assert "users.FavoriteSpecialist" in FIELDS
