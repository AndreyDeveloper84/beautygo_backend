"""Цель в промпте ИИ-комментария — только названная; маршруты WaterLog — устаревшие (DRF-2269).

1. ``ai_comment_service._build_prompt``: ``goal or "maintain"`` отправлял
   модели «Цель пользователя: maintain» за человека, который цели не
   называл, — выдуманная цель, из которой модель делает выводы.
   * g1 — нет профиля / пустая цель → в промпте нет ни «Цель пользователя»,
     ни «maintain», тон нейтральный;
   * g2 — названная цель (``lose``) → строка цели есть и тон её (обе стороны
     узла — присутствие рядом с отсутствием).
2. Публичные ``water/``, ``water/<id>/``, ``water/today/`` (кнопочный
   ``WaterLog``) открыты только мобильному клиенту BeautyGO; бот и Mini App
   ходят в ``internal/water/*``. Вызывающих в видимых репозиториях нет, но
   репозиторий мобильного клиента не виден — маршруты НЕ снимаются, а
   помечаются устаревшими:
   * d1 — каждый ответ несёт ``Deprecation: true`` и ``Sunset`` (HTTP-date);
   * d2 — каждый вызов — строка ``deprecated_endpoint_called path=…`` в лог,
     без идентификатора человека;
   * d3 — OpenAPI помечает все три операции ``deprecated``;
   * d4 — внутренние ``internal/water/*`` не помечены (присутствие рядом).
"""

from __future__ import annotations

import logging

import pytest
from rest_framework.test import APIClient

from nutrition.models import NutritionProfile
from nutrition.services.ai_comment_service import SummaryFacts, _build_prompt
from users.models import User

_FACTS = SummaryFacts(
    calories_total=1500,
    calories_goal=None,
    protein_g=80,
    fat_g=50,
    carbs_g=150,
    water_ml=1200,
    water_goal_ml=None,
    entries_count=3,
)


class TestG1NoGoalNoGoalLine:
    def test_no_profile(self) -> None:
        prompt = _build_prompt(None, _FACTS)
        assert "Факты дня" in prompt  # присутствие: промпт собран
        assert "Цель пользователя" not in prompt
        assert "maintain" not in prompt

    @pytest.mark.django_db
    def test_profile_without_a_goal(self) -> None:
        user = User.objects.create_user(username="drf2269_nogoal", password="x", role="client")
        profile = NutritionProfile.objects.create(user=user, goal="")
        prompt = _build_prompt(profile, _FACTS)
        assert "Факты дня" in prompt
        assert "Цель пользователя" not in prompt
        assert "maintain" not in prompt


class TestG2NamedGoalIsKept:
    @pytest.mark.django_db
    def test_named_goal_and_its_tone(self) -> None:
        user = User.objects.create_user(username="drf2269_lose", password="x", role="client")
        profile = NutritionProfile.objects.create(user=user, goal="lose")
        prompt = _build_prompt(profile, _FACTS)
        assert "Цель пользователя: lose" in prompt
        assert "мягко поддержи дефицит" in prompt


LEGACY = [
    ("post", "/api/v1/nutrition/water/", {"amount_ml": 250}),
    ("get", "/api/v1/nutrition/water/today/", None),
    ("delete", "/api/v1/nutrition/water/00000000-0000-0000-0000-000000000000/", None),
]


def _client() -> APIClient:
    user = User.objects.create_user(username="drf2269_mobile", password="x", role="client")
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=user)
    return c


@pytest.mark.django_db
class TestD1D2LegacyWaterRoutesAreDeprecated:
    @pytest.mark.parametrize(("method", "url", "body"), LEGACY, ids=["create", "today", "delete"])
    def test_headers_and_a_log_line_without_the_person(self, method, url, body, caplog) -> None:
        client = _client()
        with caplog.at_level(logging.WARNING, logger="core.deprecation"):
            resp = getattr(client, method)(url, body, format="json") if body else getattr(client, method)(url)
        assert resp["Deprecation"] == "true"
        assert "GMT" in resp["Sunset"]
        lines = [r.getMessage() for r in caplog.records if r.name == "core.deprecation"]
        assert len(lines) == 1 and "deprecated_endpoint_called" in lines[0]
        assert "drf2269_mobile" not in lines[0]


@pytest.mark.django_db
class TestD3OpenApiMarksThemDeprecated:
    def test_three_operations_are_deprecated_and_internal_ones_are_not(self) -> None:
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]
        assert paths["/api/v1/nutrition/water/"]["post"].get("deprecated") is True
        assert paths["/api/v1/nutrition/water/today/"]["get"].get("deprecated") is True
        assert paths["/api/v1/nutrition/water/{id}/"]["delete"].get("deprecated") is True
        # d4 — присутствие рядом: внутренний маршрут в схеме есть и не помечен.
        internal = paths["/api/v1/nutrition/internal/water/"]["post"]
        assert internal.get("deprecated") in (None, False)
