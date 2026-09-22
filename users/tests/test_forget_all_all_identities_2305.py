"""DRF-2305 — «забудь всё» стирает всех личностей субъекта на каждом пути.

C5.2 (путь бота, ``InternalPersonalDataDeleteView``) с DRF-2214 обходит
``subject_users`` — аккаунт и связанные прокси ``bot:…``, как D3. Два других
пути — кнопка в приложении (``DELETE /users/me/personal-context/``) и internal
``personal-context`` — стирали одну личность, и у связанного прокси оставалось
всё, что каталог о нём запомнил: цели, план, профиль питания, дневник с фото, а
после #545 и outbox питания, адресованный ``bot:…``.

Узлы — по всем трём путям: дневник и запомненное прокси стёрты, надгробие
прокси без строки профиля не создаётся, сосед и непривязанный прокси не
тронуты; outbox прокси — под strict xfail до слияния #545 (стирание outbox
вводит он).
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from nutrition.models import NutritionOutboxEvent
from users.models import UserPersonalContext
from users.tests.test_forget_all_catalog_2214 import _remembered_counts, _seed_remembered
from users.tests.test_forget_all_diary_2214 import (
    ALL_PATHS,
    _all_present,
    _all_zero,
    _diary_counts,
    _photo_exists,
    _seed_diary,
)
from users.tests.test_memory_erasure_matrix import (  # noqa: F401 — фикстура по имени
    _set_token,
    user,
)

User = get_user_model()
pytestmark = pytest.mark.django_db


def _proxy(owner, name: str):
    return User.objects.create(
        username=f"bot:max:{name}", role="client", is_proxy=True, linked_user=owner
    )


class TestEveryPathErasesTheWholeSubject:
    @ALL_PATHS
    def test_a_linked_proxys_diary_and_goals_go(self, user, forget) -> None:  # noqa: F811
        proxy = _proxy(user, "fa2305-proxy")
        scan = _seed_diary(proxy, tag="p2305")
        _seed_remembered(proxy)
        assert _all_present(_diary_counts(proxy))
        assert _all_present(_remembered_counts(proxy))

        forget(user)

        assert _all_zero(_diary_counts(proxy)), _diary_counts(proxy)
        assert _all_zero(_remembered_counts(proxy)), _remembered_counts(proxy)
        assert not _photo_exists(scan)

    @ALL_PATHS
    def test_a_proxy_without_a_profile_row_gets_no_tombstone(self, user, forget) -> None:  # noqa: F811
        """Как C5.2 (DRF-1038): стирание не СОЗДАЁТ надгробие личности без строки."""
        proxy = _proxy(user, "fa2305-bare")
        _seed_diary(proxy, tag="b2305")
        assert not UserPersonalContext.objects.filter(user=proxy).exists()

        forget(user)

        assert _all_zero(_diary_counts(proxy)), _diary_counts(proxy)
        assert not UserPersonalContext.objects.filter(user=proxy).exists()

    @ALL_PATHS
    def test_a_neighbour_and_an_unlinked_proxy_are_untouched(self, user, forget) -> None:  # noqa: F811
        linked = _proxy(user, "fa2305-linked")
        stranger = User.objects.create(
            username="bot:max:fa2305-stranger", role="client", is_proxy=True
        )
        neighbour = User.objects.create_user(
            username="fa2305_neighbour", password="x", role="client", phone="+79995559305"
        )
        _seed_diary(linked, tag="l2305")
        s_scan = _seed_diary(stranger, tag="s2305")
        _seed_diary(neighbour, tag="n2305")
        s_before, n_before = _diary_counts(stranger), _diary_counts(neighbour)

        forget(user)

        assert _all_zero(_diary_counts(linked))  # положительно: свой прокси стёрт
        assert _diary_counts(stranger) == s_before
        assert _diary_counts(neighbour) == n_before
        assert _photo_exists(s_scan)


class TestTheProxyOutbox:
    @ALL_PATHS
    @pytest.mark.xfail(
        strict=True,
        reason="стирание NutritionOutboxEvent вводит #545 (DRF-2277); после слияния — снять",
    )
    def test_the_proxys_outbox_goes(self, user, forget) -> None:  # noqa: F811
        proxy = _proxy(user, "fa2305-outbox")
        NutritionOutboxEvent.objects.create(
            topic=NutritionOutboxEvent.Topic.WATER_LOGGED,
            external_user_id=proxy.username,
            payload={"ml": 250},
        )
        other = NutritionOutboxEvent.objects.create(
            topic=NutritionOutboxEvent.Topic.WATER_LOGGED,
            external_user_id="bot:max:fa2305-someone-else",
            payload={"ml": 300},
        )

        forget(user)

        assert NutritionOutboxEvent.objects.filter(pk=other.pk).exists()  # чужое на месте
        assert not NutritionOutboxEvent.objects.filter(external_user_id=proxy.username).exists()
