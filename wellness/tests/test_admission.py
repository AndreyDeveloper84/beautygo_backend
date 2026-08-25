"""Табличные тесты допуска weight-loss Personal Plan (DRF-1340).

Проверяют:
- каждая из четырёх причин лестницы -> отказ со своим кодом;
- выбранный человеком `maintain` (goal_overridden_by пуст) -> допуск;
- допущенный вес (маркер в assumed_inputs) -> отказ, даже при goal="lose";
- не-весовые цели не ограничиваются;
- чистота модуля: нет импортов nutrition и нет путей к значению веса.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from wellness.admission import (
    SAFETY_OVERRIDE_REASONS,
    AdmissionDecision,
    NutritionVerdict,
    OutcomeTarget,
    PlanDirection,
    personal_plan_admission,
)

ADMISSION_PATH = Path(__file__).resolve().parent.parent / "admission.py"


def _admit(verdict: NutritionVerdict) -> AdmissionDecision:
    return personal_plan_admission(
        verdict,
        outcome_target=OutcomeTarget.BODY_WEIGHT,
        direction=PlanDirection.REDUCE,
    )


# ---------------------------------------------------------------------------
# Четыре причины лестницы -> отказ со своим кодом
# ---------------------------------------------------------------------------


class TestSafetyOverrideRefusal:
    @pytest.mark.parametrize("reason", SAFETY_OVERRIDE_REASONS)
    def test_each_override_refused_with_own_code(self, reason):
        verdict = NutritionVerdict(goal="maintain", goal_overridden_by=reason)
        decision = _admit(verdict)
        assert decision.allowed is False
        assert decision.reason_code == reason

    @pytest.mark.parametrize("reason", SAFETY_OVERRIDE_REASONS)
    def test_override_refused_even_when_goal_is_lose(self, reason):
        verdict = NutritionVerdict(goal="lose", goal_overridden_by=reason)
        assert _admit(verdict).allowed is False


# ---------------------------------------------------------------------------
# Выбранный человеком maintain -> допуск
# ---------------------------------------------------------------------------


class TestHumanChosenGoalsAllowed:
    def test_maintain_without_override_allowed(self):
        """Коэрцированный лестницей maintain отличим: у него непустой
        goal_overridden_by. Выбранный человеком — пустой."""
        decision = _admit(NutritionVerdict(goal="maintain", goal_overridden_by=""))
        assert decision.allowed is True
        assert decision.reason_code == "admitted"

    def test_lose_without_override_allowed(self):
        decision = _admit(NutritionVerdict(goal="lose"))
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# Допущенный вес -> отказ (даже при goal == "lose")
# ---------------------------------------------------------------------------


class TestAssumedWeightRefusal:
    def test_assumed_weight_refused_despite_lose_goal(self):
        verdict = NutritionVerdict(goal="lose", assumed_inputs=("weight_kg",))
        decision = _admit(verdict)
        assert decision.allowed is False
        assert decision.reason_code == "assumed_weight"

    def test_assumed_weight_marker_among_others(self):
        verdict = NutritionVerdict(
            goal="lose",
            assumed_inputs=("height_cm", "weight_kg", "age"),
        )
        assert _admit(verdict).reason_code == "assumed_weight"

    def test_other_assumed_inputs_do_not_block(self):
        verdict = NutritionVerdict(goal="lose", assumed_inputs=("height_cm",))
        assert _admit(verdict).allowed is True


# ---------------------------------------------------------------------------
# Не-весовые цели не ограничиваются
# ---------------------------------------------------------------------------


class TestNonWeightLossNotGated:
    def test_other_target_allowed_despite_override(self):
        verdict = NutritionVerdict(goal="maintain", goal_overridden_by="pregnancy")
        decision = personal_plan_admission(
            verdict, outcome_target="edema", direction=PlanDirection.REDUCE,
        )
        assert decision.allowed is True
        assert decision.reason_code == "non_weight_loss_target"

    def test_other_direction_allowed_despite_assumed_weight(self):
        verdict = NutritionVerdict(goal="lose", assumed_inputs=("weight_kg",))
        decision = personal_plan_admission(
            verdict,
            outcome_target=OutcomeTarget.BODY_WEIGHT,
            direction=PlanDirection.INCREASE,
        )
        assert decision.allowed is True
        assert decision.reason_code == "non_weight_loss_target"


# ---------------------------------------------------------------------------
# Чистота модуля: нет nutrition-импортов, нет путей к значению веса
# ---------------------------------------------------------------------------


class TestModulePurity:
    def test_no_nutrition_imports_in_ast(self):
        """AST-разбор: ни один import не ссылается на nutrition."""
        tree = ast.parse(ADMISSION_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name != "nutrition" and not name.startswith("nutrition.")

    def test_import_does_not_load_nutrition(self):
        """Свежий интерпретатор: после импорта wellness.admission
        в sys.modules нет nutrition (надёжнее проверки в общей сессии,
        где nutrition мог быть импортирован другими тестами)."""
        code = (
            "import sys, wellness.admission; "
            "assert not any(m == 'nutrition' or m.startswith('nutrition.') "
            "for m in sys.modules)"
        )
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=ADMISSION_PATH.parent.parent,
            check=True,
        )

    def test_no_weight_value_access_paths(self):
        """Модуль не читает вес: нет attribute/subscript доступа к weight_kg
        и нет предиката на None. Допустимо единственное объявление маркера
        (строка-константа для сравнения с assumed_inputs)."""
        source = ADMISSION_PATH.read_text(encoding="utf-8")
        forbidden = (
            ".weight_kg",
            '["weight_kg"]',
            "['weight_kg']",
            "weight_kg is",
            "getattr",
        )
        for pattern in forbidden:
            assert pattern not in source
        assert source.count('"weight_kg"') == 1
