"""Промах справочника виден в логе, числа на 100 г не теряются (DRF-2335).

Замер DRF-2329: когда человек показывает фото и не получает калорий, узнать
почему нельзя ни по ответу, ни по журналу.

Два исхода выглядят снаружи одинаково — ``nutrition: null``:

* блюда нет в справочнике — чисел у нас действительно нет;
* блюдо найдено, но провайдер не назвал порцию — итогов нет, а числа
  **на 100 г посчитаны и лежат в строке**. Сериализатор выбрасывал их вместе
  с пустыми итогами. Это единственное место во всём пути, где питание не
  «не родилось», а потеряно.

Правило:

* итоги пусты, числа на 100 г есть → наружу идут числа на 100 г; итоги
  остаются пустыми — подменять ими порцию нельзя, 49 ккал на 100 г и
  49 ккал за тарелку это разные утверждения;
* чисел нет вовсе → по-прежнему ``null``;
* причина пустоты называется в логе разными словами (``dish_not_found`` /
  ``portion_unknown``), **без текста блюда и без персональных данных** —
  журнал общий. Строка скана (UUID) в логе есть: по ней находят запись,
  не называя человека.

Признака «почему нет питания» в самом ответе этот лист НЕ вводит (п. 3
DRF-2335 ждёт слова владельца) — узел ниже держит, что внутренняя кухня
(``source``, ``matched_dish``) наружу не выходит.
"""

from __future__ import annotations

import io
import logging
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient

from nutrition.models import FoodScan
from nutrition.providers.base import ScanResult
from nutrition.serializers import FoodScanResponseSerializer
from nutrition.services.food_scanner_router import RouterResult
from users.models import User

pytestmark = pytest.mark.django_db

INTERNAL_URL = "/api/v1/nutrition/internal/scan/"
SERVICE_TOKEN = "test-service-token-DRF-2335"  # pragma: allowlist secret

# Борщ из справочника: 49 ккал / 1.6 Б / 2.2 Ж / 6.7 У на 100 г.
BORSCH_PER_100G = {
    "calories": 49.0,
    "protein_g": 1.6,
    "fat_g": 2.2,
    "carbs_g": 6.7,
}


@pytest.fixture(autouse=True)
def _token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN
    settings.FOOD_SCAN_DAILY_PER_USER = 50
    settings.FOOD_SCAN_DAILY_TOTAL = 50


def _jpeg() -> bytes:
    img = Image.new("RGB", (4, 4), color=(200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _result(dish: str, portion_g: float | None) -> ScanResult:
    return ScanResult(
        dish_name=dish,
        confidence=0.9,
        portion_g=portion_g,
        provider="openai",
    )


def _scan(dish: str, portion_g: float | None, external_id: str = "bot:2335"):
    router = MagicMock()
    router.scan.return_value = RouterResult(
        result=_result(dish, portion_g), primary_provider_name="openai",
    )
    with (
        patch("nutrition.views.FoodScannerRouter", return_value=router),
        patch(
            "nutrition.views.NutritionLookup",
            side_effect=lambda *a, **kw: _real_lookup(),
        ),
    ):
        return APIClient().post(
            INTERNAL_URL,
            {"image": SimpleUploadedFile("meal.jpg", _jpeg(), content_type="image/jpeg")},
            format="multipart",
            HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
            HTTP_X_EXTERNAL_USER_ID=external_id,
        )


def _real_lookup():
    from nutrition.services.nutrition_lookup import NutritionLookup

    return NutritionLookup()


class TestNumbersPer100gSurviveEmptyTotals:
    """Найденное блюдо без порции — числа на 100 г доходят до человека."""

    def test_the_wire_carries_per_100g_when_totals_are_empty(self) -> None:
        resp = _scan("борщ", portion_g=None)

        assert resp.status_code == 200
        nutrition = resp.json()["data"]["nutrition"]
        assert nutrition is not None, "числа на 100 г есть в строке — терять их нельзя"
        assert nutrition["per_100g"] == BORSCH_PER_100G

    def test_the_portion_totals_stay_empty(self) -> None:
        """Подменять итоги числами на 100 г нельзя — это разные утверждения."""
        resp = _scan("борщ", portion_g=None)

        nutrition = resp.json()["data"]["nutrition"]
        assert nutrition["calories"] is None
        assert nutrition["protein_g"] is None
        assert nutrition["fat_g"] is None
        assert nutrition["carbs_g"] is None

    def test_a_known_portion_still_carries_totals_and_per_100g(self) -> None:
        """Контроль: обычный успех не сломан, per_100g добавлен рядом."""
        resp = _scan("борщ", portion_g=300)

        nutrition = resp.json()["data"]["nutrition"]
        assert nutrition["calories"] == 147.0
        assert nutrition["per_100g"] == BORSCH_PER_100G

    def test_nothing_found_stays_null(self) -> None:
        """Контроль: чисел нет вовсе — отдавать нечего."""
        resp = _scan("лаваш с начинкой", portion_g=None)

        assert resp.status_code == 200
        assert resp.json()["data"]["nutrition"] is None

    def test_a_row_without_nutrition_stays_null(self, ) -> None:
        """Контроль: пустое поле строки — по-прежнему ``null``."""
        user = User.objects.create(username="bot:23350", role="client", is_proxy=True)
        scan = FoodScan.objects.create(
            user=user, dish_name="суши", confidence=0.9, portion_g=200,
            provider_used=FoodScan.Provider.OPENAI, nutrition=None,
        )
        assert FoodScanResponseSerializer(scan).data["nutrition"] is None


class TestTheReasonIsNamedInTheLog:
    """Почему нет калорий — видно в журнале, без текста блюда."""

    def test_a_dish_outside_the_catalogue_says_dish_not_found(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="nutrition.views"):
            _scan("лаваш с начинкой", portion_g=None)

        line = _gap_line(caplog)
        assert "reason=dish_not_found" in line

    def test_a_known_dish_without_a_portion_says_portion_unknown(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="nutrition.views"):
            _scan("борщ", portion_g=None)

        line = _gap_line(caplog)
        assert "reason=portion_unknown" in line

    def test_the_scan_row_is_named_so_the_row_can_be_found(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="nutrition.views"):
            resp = _scan("лаваш с начинкой", portion_g=None)

        assert str(resp.json()["data"]["scan_id"]) in _gap_line(caplog)

    def test_the_dish_text_never_reaches_the_log(self, caplog) -> None:
        """Журнал общий: название блюда — это то, что человек показал."""
        with caplog.at_level(logging.INFO, logger="nutrition.views"):
            _scan("лаваш с начинкой", portion_g=None)

        assert "лаваш" not in caplog.text.lower()

    def test_a_full_answer_says_nothing(self, caplog) -> None:
        """Контроль: есть калории — строки о пустоте нет."""
        with caplog.at_level(logging.INFO, logger="nutrition.views"):
            _scan("борщ", portion_g=300)

        assert not _gap_lines(caplog)


class TestTheKitchenStaysInside:
    """П. 3 листа ждёт слова владельца — наружу ничего лишнего."""

    def test_the_answer_does_not_expose_source_or_matched_dish(self) -> None:
        nutrition = _scan("борщ", portion_g=None).json()["data"]["nutrition"]

        assert "source" not in nutrition
        assert "matched_dish" not in nutrition

    def test_the_answer_carries_no_reason_field_yet(self) -> None:
        nutrition = _scan("борщ", portion_g=None).json()["data"]["nutrition"]

        assert "reason" not in nutrition


def _gap_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "no_nutrition" in r.getMessage()]


def _gap_line(caplog) -> str:
    lines = _gap_lines(caplog)
    assert lines, f"строки о пустом питании нет; журнал: {caplog.text!r}"
    return lines[0]
