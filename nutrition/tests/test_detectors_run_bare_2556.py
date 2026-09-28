"""DRF-2556 (б) — каждый детектор паттернов прогоняется НАПРЯМУЮ, без обёртки пайплайна.

``detect_patterns`` оборачивает каждый детектор в ``except Exception`` с логом
(``pattern_detection_service.py``): решение «один сломанный детектор не роняет
остальные» — в бою это защита, и её поведение здесь не меняется. Но та же обёртка
превращает ошибку КОДА детектора (неверное имя поля, опечатку в атрибуте) в
«паттерна нет», и тест пайплайна её не увидит — ровно как буст избранного мастера
тонул в ``except Exception: pass`` у ``_score``.

Узел разводит «защиту в бою» и «обнаружение в тестах»: список детекторов берётся из
исходника ``detect_patterns`` (новый детектор попадает сюда сам), и каждый зовётся
без обёртки на двух наборах данных — пустом и насыщенном. Любое исключение — красно
с именем детектора.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date, datetime, time, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model

from nutrition.models import NutritionProfile
from nutrition.services import pattern_detection_service as pds

User = get_user_model()

pytestmark = pytest.mark.django_db

TODAY = date(2026, 9, 20)


def _detector_names() -> list[str]:
    """Имена из кортежа ``detectors = (...)`` внутри ``detect_patterns`` — по исходнику."""
    tree = ast.parse(inspect.getsource(pds.detect_patterns))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "detectors" for t in node.targets)
            and isinstance(node.value, ast.Tuple)
        ):
            return [e.id for e in node.value.elts if isinstance(e, ast.Name)]
    raise AssertionError("кортеж detectors в detect_patterns не найден — прибор ослеп")


@pytest.fixture
def profile():
    user = User.objects.create_user(
        username="detectors_2556", password="x", role="client", phone="+79990825562"
    )
    return NutritionProfile.objects.create(
        user=user, gender="female", age=34, height_cm=165, weight_kg=62.0,
        goal="maintain", pace="moderate",
    )


def _rich_stats() -> tuple[dict, dict]:
    """30 дней, где есть всё, что детекторы умеют читать: поздние ужины, сладкое
    вечером, пропуски, кофеин, алкоголь и низкие микроэлементы при посчитанных строках."""
    food: dict = {}
    water: dict = {}
    for i in range(30):
        day = TODAY - timedelta(days=i)
        skipped = i % 5 == 0
        food[day] = pds.DailyFoodStats(
            day=day,
            total_kcal=0.0 if skipped else 1400.0,
            total_protein_g=0.0 if skipped else 40.0,
            last_meal_dt=None if skipped else datetime.combine(day, time(22, 30), tzinfo=timezone.utc),
            has_evening_sweets=i % 2 == 0,
            weekday=day.weekday(),
            total_vitamin_d_iu=50.0,
            total_iron_mg=2.0,
            total_omega3_g=0.1,
            total_calcium_mg=150.0,
            total_vitamin_b12_mcg=0.3,
            total_rows=0 if skipped else 4,
            rows_with_vitamin_d=0 if skipped else 4,
            rows_with_iron=0 if skipped else 4,
            rows_with_omega3=0 if skipped else 4,
            rows_with_calcium=0 if skipped else 4,
            rows_with_b12=0 if skipped else 4,
        )
        water[day] = pds.DailyWaterStats(
            day=day,
            total_water_ml=600.0,
            total_caffeine_mg=380.0,
            has_late_caffeine=i % 2 == 0,
            has_alcohol=i % 3 == 0,
        )
    return food, water


class TestEveryDetectorRunsBare:
    def test_the_detector_list_is_read_and_complete(self) -> None:
        names = _detector_names()
        # Наличие впереди: прибор видит список, и каждое имя — функция модуля.
        assert len(names) >= 10, names
        missing = [n for n in names if not callable(getattr(pds, n, None))]
        assert not missing, missing

    @pytest.mark.parametrize("dataset", ["empty", "rich"])
    def test_every_detector_runs_without_the_pipeline_guard(self, profile, dataset) -> None:
        food, water = ({}, {}) if dataset == "empty" else _rich_stats()
        failures: list[str] = []
        found: list[str] = []
        for name in _detector_names():
            try:
                result = getattr(pds, name)(
                    food_stats=food, water_stats=water, profile=profile, today=TODAY
                )
            except Exception as exc:  # noqa: BLE001 — узел собирает ВСЕ падения, не первое
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            if result is not None:
                assert isinstance(result, pds.DetectedPattern), name
                found.append(result.slug)
        assert not failures, "детекторы упали без обёртки:\n  " + "\n  ".join(failures)
        if dataset == "rich":
            # Насыщенные данные обязаны пройти глубже ранних возвратов: иначе
            # «не упал» доказывало бы только, что детектор вернулся на первой строке.
            print(f"DRF-2556: паттернов на насыщенных данных {len(found)}: {sorted(found)}")
            assert len(found) >= 5, found

    def test_a_broken_detector_is_named(self, profile, monkeypatch) -> None:
        # Подмена: детектор с ошибкой кода — узел обязан назвать его, а пайплайн в
        # бою по-прежнему не падает (его обёртка не тронута).
        name = _detector_names()[0]

        def broken(**_kwargs):
            raise AttributeError("'DailyFoodStats' object has no attribute 'total_iron'")

        monkeypatch.setattr(pds, name, broken)
        with pytest.raises(AssertionError, match=name):
            self.test_every_detector_runs_without_the_pipeline_guard(profile, "empty")
