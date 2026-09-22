"""Ключи ответа C5.1 — закрытый список; у бота есть его зеркало (DRF-2307).

Бот вкладывает ответ C5.1 в свою выгрузку целиком (раздел ``ayla``) и сам
ключей не разбирает, но свой **состав** выгрузки и матрицу «забудь всё» ведёт
по зеркалу этих ключей — ai-bot-platform
``apps/identity/export_coverage.py::CATALOG_EXPORT_SECTIONS``. Машиной два
репозитория не сверяются (живой узел бота запускается вручную, стенд
``cross-boundary`` выключен), поэтому спусковой крючок стоит здесь, где
рождается расхождение: новый раздел C5.1 без правки
:data:`users.personal_data_api.EXPORT_SECTIONS` краснит этот узел, а список
рядом говорит, что ещё обновить в боте.

Так уже было: #545 и #548 добавили три раздела, а бот узнал о них отдельным
листом (DRF-2307).
"""
from __future__ import annotations

import pytest

from users.tests.test_memory_erasure_matrix import (  # noqa: F401 — фикстуры по имени
    _internal,
    _set_token,
    user,
)

pytestmark = pytest.mark.django_db

EXPORT_URL = "/api/v1/internal/users/{user_id}/personal-data/export/"

#: Ключи ответа, которых нет у связанной личности (у неё — свой идентификатор).
_ACCOUNT_ONLY = ("user_id", "exported_at", "linked_identities")


def _declared() -> tuple[str, ...]:
    from users import personal_data_api

    declared = getattr(personal_data_api, "EXPORT_SECTIONS", None)
    assert declared, "нет закрытого списка ключей C5.1 в users.personal_data_api"
    return tuple(declared)


def _export(u) -> dict:
    resp = _internal().get(EXPORT_URL.format(user_id=u.pk))
    assert resp.status_code == 200, resp.content
    return resp.json()["data"]


class TestTheResponseIsTheDeclaredList:
    def test_the_account_carries_exactly_the_declared_sections(self, user) -> None:  # noqa: F811
        data = _export(user)

        assert "profile" in data  # наличие: это ответ C5.1
        assert set(data) == set(_declared()), (
            sorted(set(data) - set(_declared())),
            sorted(set(_declared()) - set(data)),
        )

    def test_a_linked_identity_carries_the_same_data_sections(self, user) -> None:  # noqa: F811
        from django.contrib.auth import get_user_model

        User = get_user_model()
        User.objects.create(
            username="bot:max:pin2307-proxy", role="client", is_proxy=True, linked_user=user
        )

        (linked,) = [
            item for item in _export(user)["linked_identities"] if item["external_user_id"] == "bot:max:pin2307-proxy"
        ]

        data_sections = set(_declared()) - set(_ACCOUNT_ONLY)
        assert data_sections  # наличие
        assert set(linked) == data_sections | {"external_user_id"}

    def test_the_list_points_to_the_bot_mirror(self) -> None:
        """Список без указателя на зеркало — половина крючка: автор не узнает, куда идти."""
        import inspect

        from users import personal_data_api

        source = inspect.getsource(personal_data_api)
        assert _declared()  # наличие
        assert "CATALOG_EXPORT_SECTIONS" in source
        assert "ai-bot-platform" in source
