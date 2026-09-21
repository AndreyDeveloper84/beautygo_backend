"""Ответ и журнал стирания говорят правду о стёртом (DRF-2214).

После #526 и #530 «забудь всё» стирает цели, план, профиль питания и дневник,
но говорило об этом по-старому:

- **Журнал AMD-010** (``personal_data_deleted``) писал ``scope=[]``, если строка
  профиля уже была надгробием, — а по докстрингу ``erase_personal_context``
  это «нечего было стирать», хотя стёрты цели и дневник. У связанного прокси
  без строки профиля его дневник и цели стирались без события вовсе.
- **C5.2** отвечал ``deleted: []`` при том же.
- **C5.3** ``erasure-status`` — чтение, по которому бот пишет человеку
  «удалено» (ai-bot-platform DRF-1950), — смотрел только строку профиля:
  «стёрто», даже если цели или дневник лежат.

Теперь ``scope`` называет стёртое закрытым словарём
(``users.forget_all_catalog.REMEMBERED_SCOPE``), а ``erased`` требует, чтобы
запомненных строк не осталось, — только счёт, без значений.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from analytics import event_catalogue
from analytics.models import AnalyticsEvent
from users.models import UserPersonalContext
from users.tests.test_forget_all_catalog_2214 import (
    GOAL_TEXT,
    _remembered_counts,
    _seed_remembered,
)
from users.tests.test_forget_all_diary_2214 import (
    _bot_url,
    _diary_counts,
    _seed_diary,
)
from users.tests.test_memory_erasure_matrix import (  # noqa: F401 — фикстуры по имени
    APP_PC_URL,
    _app,
    _internal,
    _set_token,
    user,
)

User = get_user_model()
pytestmark = pytest.mark.django_db

#: Всё, что каталог запомнил вне профиля, — словарь scope журнала и ответа.
REMEMBERED = ["goals", "wellness_plan", "nutrition_profile", "food_diary", "shown_hints"]


def _status_url(u) -> str:
    return f"/api/v1/internal/users/{u.pk}/personal-data/erasure-status/"


def _events(u) -> list[dict]:
    return [
        row.payload
        for row in AnalyticsEvent.objects.filter(
            actor=u, event_name=event_catalogue.PERSONAL_DATA_DELETED
        ).order_by("created_at", "id")
    ]


def _erase_via_bot(u) -> dict:
    resp = _internal().delete(_bot_url(u))
    assert resp.status_code == 200, resp.content
    return resp.json()["data"]


def _status(u) -> dict:
    resp = _internal().get(_status_url(u))
    assert resp.status_code == 200, resp.content
    return resp.json()["data"]


def _tombstoned(u) -> None:
    """Профиль уже стёрт раньше — строка-надгробие, как после первого «забудь всё»."""
    UserPersonalContext.objects.create(user=u, diet_type="keto")
    _erase_via_bot(u)
    assert _events(u)  # наличие: первое стирание записано


def _seed_everything(u) -> None:
    _seed_remembered(u)
    _seed_diary(u)
    assert all(n >= 1 for n in _remembered_counts(u).values())
    assert all(n >= 1 for n in _diary_counts(u).values())


class TestTheJournalNamesWhatWasErased:
    def test_goals_and_diary_behind_a_tombstone_are_not_nothing(self, user) -> None:  # noqa: F811
        """Главный узел: профиль уже надгробие, стёрты цели и дневник — scope их называет."""
        _tombstoned(user)
        _seed_everything(user)

        _erase_via_bot(user)

        last = _events(user)[-1]
        assert last["scope"] == REMEMBERED, last
        assert last["initiator"] == "internal_api"

    def test_a_full_first_erasure_names_the_profile_and_the_rest(self, user) -> None:  # noqa: F811
        UserPersonalContext.objects.create(user=user, diet_type="keto")
        _seed_everything(user)

        _erase_via_bot(user)

        assert _events(user)[-1]["scope"] == ["personal_context", *REMEMBERED]

    def test_only_what_was_there_is_named(self, user) -> None:  # noqa: F811
        """Без целей и плана — их и нет в scope: словарь не раздувается пустыми группами.

        Засев дневника несёт ещё кэш профиля (``nutrition_profile``) и историю
        подсказок (``shown_hints``) — они и названы, в порядке словаря.
        """
        _tombstoned(user)
        _seed_diary(user)
        assert _diary_counts(user)["FoodLog"] >= 1
        assert _remembered_counts(user)["ClientGoal"] == 0

        _erase_via_bot(user)

        assert _events(user)[-1]["scope"] == ["nutrition_profile", "food_diary", "shown_hints"]

    def test_a_repeat_is_still_nothing(self, user) -> None:  # noqa: F811
        """Контракт идемпотентности C5.2 жив: повтор — ``scope=[]``."""
        UserPersonalContext.objects.create(user=user, diet_type="keto")
        _seed_everything(user)
        _erase_via_bot(user)
        assert _events(user)[-1]["scope"]  # наличие: первый раз стёрто непустое

        _erase_via_bot(user)

        assert _events(user)[-1]["scope"] == []

    def test_the_app_path_names_it_too(self, user) -> None:  # noqa: F811
        _tombstoned(user)
        _seed_everything(user)

        resp = _app(user).delete(APP_PC_URL)
        assert resp.status_code == 204

        last = _events(user)[-1]
        assert last["scope"] == REMEMBERED, last
        assert last["initiator"] == "app"

    def test_no_value_reaches_the_journal(self, user) -> None:  # noqa: F811
        """AMD-010: состав стёртого — да, сами слова человека — нет."""
        _seed_everything(user)

        _erase_via_bot(user)

        payloads = repr(_events(user))
        assert "goals" in payloads  # наличие: событие о целях есть
        assert GOAL_TEXT not in payloads


class TestALinkedProxyIsJournalled:
    def _proxy(self, owner, name: str) -> User:
        return User.objects.create(
            username=f"bot:max:{name}", role="client", is_proxy=True, linked_user=owner
        )

    def test_a_proxy_without_a_profile_row_gets_an_event(self, user) -> None:  # noqa: F811
        """Дыра: дневник прокси стирался без события аудита вовсе."""
        proxy = self._proxy(user, "truth-proxy")
        _seed_diary(proxy, tag="tp")
        assert not UserPersonalContext.objects.filter(user=proxy).exists()
        assert _events(proxy) == []

        _erase_via_bot(user)

        events = _events(proxy)
        assert len(events) == 1, events
        assert "food_diary" in events[0]["scope"]
        # И надгробия ему по-прежнему не создано (DRF-1038).
        assert not UserPersonalContext.objects.filter(user=proxy).exists()

    def test_a_proxy_with_nothing_gets_no_event(self, user) -> None:  # noqa: F811
        """Положительная пара: пустой прокси — ни надгробия, ни события, как было."""
        proxy = self._proxy(user, "truth-empty")
        _seed_diary(user)
        assert _diary_counts(user)["FoodLog"] >= 1

        _erase_via_bot(user)

        assert _events(user)  # наличие: у аккаунта событие есть
        assert _events(proxy) == []


class TestTheResponseNamesWhatWasErased:
    def test_c52_deleted_names_the_goals_and_diary(self, user) -> None:  # noqa: F811
        _tombstoned(user)
        _seed_everything(user)

        data = _erase_via_bot(user)

        assert data["deleted"] == REMEMBERED


class TestErasureStatusSeesTheRest:
    def test_a_tombstone_over_remaining_goals_is_not_erased(self, user) -> None:  # noqa: F811
        """Главный узел C5.3: строка профиля — надгробие, но цели лежат → не «стёрто»."""
        _tombstoned(user)
        assert _status(user)["erased"] is True  # наличие: одно надгробие — «стёрто»
        _seed_remembered(user)

        status = _status(user)

        assert status["erased"] is False
        account = status["identities"][0]
        assert account["context_row"] == "tombstone"
        assert account["erased"] is False
        assert account["remembered_rows"] > 0

    def test_remaining_diary_on_a_proxy_is_not_erased(self, user) -> None:  # noqa: F811
        proxy = User.objects.create(
            username="bot:max:truth-status", role="client", is_proxy=True, linked_user=user
        )
        _tombstoned(user)
        _seed_diary(proxy, tag="ts")

        status = _status(user)

        assert status["erased"] is False
        by_kind = [(i["kind"], i["erased"]) for i in status["identities"]]
        assert ("account", True) in by_kind
        assert ("linked_identity", False) in by_kind

    def test_after_a_full_erasure_it_is_erased(self, user) -> None:  # noqa: F811
        UserPersonalContext.objects.create(user=user, diet_type="keto")
        _seed_everything(user)
        assert _status(user)["erased"] is False  # наличие: до стирания — не стёрто

        _erase_via_bot(user)

        status = _status(user)
        assert status["erased"] is True
        assert status["identities"][0]["remembered_rows"] == 0

    def test_only_a_count_no_values(self, user) -> None:  # noqa: F811
        _seed_everything(user)

        status = _status(user)

        assert status["identities"][0]["remembered_rows"] > 0  # наличие: есть что считать
        assert GOAL_TEXT not in repr(status)
        assert "Борщ" not in repr(status)
