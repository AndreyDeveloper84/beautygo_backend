"""DRF-2227 + DT-1 (CD §67) — AI-комментарий к дневному отчёту.

DT-1 (решение владельца, CD §67): «при любом флаге здоровья внешняя LLM не
вызывается». До правки ``AICommentService`` глушил модель только при
``eating_disorder``; беременность, кормление, диабет, гипертония, ЖКТ уходили
в gpt-4o-mini как «подсказки тона» в промпте.

DRF-2227: кэш комментария — 6 ч на (человек, день) без сброса. Записал еду /
воду, поправил, удалил — отчёт до шести часов показывал комментарий к старым
цифрам. Запасной текст при отказе модели говорил «почти в норме» — оценка без
расчёта, ровно то, чего запасной путь делать не вправе.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_tz
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from django.core.cache import cache

from nutrition.models import FoodLog, NutritionProfile, WaterEntry
from nutrition.services.ai_comment_service import AICommentService, SummaryFacts
from users.models import User

FACTS = SummaryFacts(1450, 1500, 100, 50, 150, 1500, 2000, 3)


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user(db):
    return User.objects.create(username="bot:2227", role="client", is_proxy=True)


def _llm(text: str = "Хороший день."):
    client = MagicMock()
    msg = MagicMock()
    msg.content = text
    client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=msg)])
    return client


def _today():
    return datetime.now(dt_tz.utc).date()


def _food(user):
    return FoodLog.objects.create(
        user=user,
        dish_name="гречка",
        portion_multiplier=1.0,
        calories=300,
        protein_g=10,
        fat_g=5,
        carbs_g=50,
        meal_type="lunch",
        logged_at=datetime.now(dt_tz.utc),
        idempotency_key=str(uuid4()),
    )


def _water(user):
    return WaterEntry.objects.create(
        user=user, ts=datetime.now(dt_tz.utc), ml=250, water_ml=250.0
    )


# ── DT-1 — при любом флаге здоровья модель не зовётся ────────────────


class TestDT1NoLlmOnAnyHealthFlag:
    @pytest.mark.parametrize(
        "flag",
        [
            "pregnant",
            "breastfeeding",
            "eating_disorder",
            "diabetes_t1",
            "diabetes_t2",
            "prediabetes",
            "hypertension",
            "gi_problems",
        ],
    )
    def test_no_llm_call_and_a_neutral_text(self, user, flag) -> None:
        NutritionProfile.objects.create(user=user, goal="lose", health_flags={flag: True})
        factory = MagicMock(side_effect=AssertionError("LLM must not be called"))

        out = AICommentService(llm_client_factory=factory).comment_for(
            user_id=user.id, day=_today(), facts=FACTS
        )

        assert factory.call_count == 0
        assert out  # положительно: человек получает текст
        assert not any(ch.isdigit() for ch in out)  # без цифр и оценки
        assert "норм" not in out.lower()

    def test_an_unknown_future_flag_also_blocks_the_llm(self, user) -> None:
        """«Любой флаг» — не перечень: новый флаг каталога не открывает модель."""
        NutritionProfile.objects.create(user=user, health_flags={"thyroid": True})
        factory = MagicMock(side_effect=AssertionError("LLM must not be called"))
        AICommentService(llm_client_factory=factory).comment_for(
            user_id=user.id, day=_today(), facts=FACTS
        )
        assert factory.call_count == 0

    def test_no_flags_still_calls_the_llm(self, user) -> None:
        """Ложный вход: без флагов вызов есть."""
        NutritionProfile.objects.create(user=user, goal="lose", health_flags={})
        client = _llm()
        out = AICommentService(llm_client_factory=lambda: client).comment_for(
            user_id=user.id, day=_today(), facts=FACTS
        )
        assert client.chat.completions.create.call_count == 1
        assert out == "Хороший день."

    def test_false_valued_flags_are_no_flags(self, user) -> None:
        """``{"pregnant": false}`` — флага нет: анкета записывает «нет» явно."""
        NutritionProfile.objects.create(user=user, health_flags={"pregnant": False})
        client = _llm()
        AICommentService(llm_client_factory=lambda: client).comment_for(
            user_id=user.id, day=_today(), facts=FACTS
        )
        assert client.chat.completions.create.call_count == 1


# ── DRF-2227 — кэш сбрасывается при изменении дневника ───────────────


class TestDRF2227CacheInvalidation:
    @staticmethod
    def _comment(user, client):
        return AICommentService(llm_client_factory=lambda: client).comment_for(
            user_id=user.id, day=_today(), facts=FACTS
        )

    def test_cache_holds_without_changes(self, user) -> None:
        """Положительная пара: без изменений второй запрос — из кэша."""
        NutritionProfile.objects.create(user=user, goal="lose")
        client = _llm()
        self._comment(user, client)
        self._comment(user, client)
        assert client.chat.completions.create.call_count == 1

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda u: _food(u), id="food_create"),
            pytest.param(lambda u: _water(u), id="water_create"),
        ],
    )
    def test_create_invalidates(self, user, mutate) -> None:
        NutritionProfile.objects.create(user=user, goal="lose")
        client = _llm()
        self._comment(user, client)
        mutate(user)
        self._comment(user, client)
        assert client.chat.completions.create.call_count == 2

    def test_food_edit_and_delete_invalidate(self, user) -> None:
        NutritionProfile.objects.create(user=user, goal="lose")
        log = _food(user)
        client = _llm()
        self._comment(user, client)

        log.calories = 500
        log.save()
        self._comment(user, client)
        assert client.chat.completions.create.call_count == 2

        log.delete()
        self._comment(user, client)
        assert client.chat.completions.create.call_count == 3

    def test_water_edit_and_delete_invalidate(self, user) -> None:
        NutritionProfile.objects.create(user=user, goal="lose")
        entry = _water(user)
        client = _llm()
        self._comment(user, client)

        entry.ml = 500
        entry.save()
        self._comment(user, client)
        assert client.chat.completions.create.call_count == 2

        entry.delete()
        self._comment(user, client)
        assert client.chat.completions.create.call_count == 3

    def test_another_persons_change_keeps_my_cache(self, user) -> None:
        other = User.objects.create(username="bot:2227-b", role="client", is_proxy=True)
        NutritionProfile.objects.create(user=user, goal="lose")
        client = _llm()
        self._comment(user, client)
        _food(other)
        self._comment(user, client)
        assert client.chat.completions.create.call_count == 1


# ── DRF-2227 — запасной текст без оценки ─────────────────────────────


class TestDRF2227NeutralFallback:
    @pytest.mark.parametrize(
        "facts",
        [
            SummaryFacts(1450, 1500, 100, 50, 150, 1500, 2000, 3),  # вода ниже 70 %
            SummaryFacts(1450, 1500, 100, 50, 150, 2000, 2000, 3),  # вода в норме
            SummaryFacts(1450, None, 100, 50, 150, 500, None, 3),  # ориентиров нет
        ],
    )
    def test_fallback_carries_no_assessment(self, user, facts) -> None:
        NutritionProfile.objects.create(user=user, goal="lose")
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("boom")
        out = AICommentService(llm_client_factory=lambda: client).comment_for(
            user_id=user.id, day=_today(), facts=facts
        )
        assert out  # положительно: текст есть
        low = out.lower()
        for evaluative in ("норм", "хороший", "попробуй", "больше", "меньше"):
            assert evaluative not in low, (evaluative, out)

    def test_empty_day_fallback_is_unchanged(self, user) -> None:
        NutritionProfile.objects.create(user=user, goal="lose")
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("boom")
        out = AICommentService(llm_client_factory=lambda: client).comment_for(
            user_id=user.id, day=_today(), facts=SummaryFacts(0, 1500, 0, 0, 0, 0, 2000, 0)
        )
        assert "записей пока нет" in out
