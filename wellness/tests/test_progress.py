"""Приёмка DRF-1343 — вычислитель Outcome Progress.

Доказывает:
- четыре состояния (no_measure / no_observations / baseline_only / derived);
- amendment A: наблюдение до `outcome.created_at` не входит в baseline;
  новый план (новая связь) baseline НЕ сбрасывает;
- superseded-наблюдения исключены; удаление аннулирует прогресс само
  (производная, ничего не хранит);
- property: посторонние факты («выполненные действия») не меняют прогресс
  ни на одно значение (AYLA-DEC-0082);
- чистота модуля: `wellness/progress.py` не импортирует nutrition и не
  имеет пути к NutritionProfile;
- в OutcomeProgress нет поля, куда сходятся обе линии (нет процента,
  adherence, цвета, шкалы).
"""
from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import asdict, fields
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.utils import timezone

from users.models import User
from wellness.models import (
    DesiredOutcome,
    EvidenceRegistryEntry,
    PersonalPlan,
    PlanOutcomeLink,
    ProgressObservation,
)
from wellness.progress import (
    STATE_BASELINE_ONLY,
    STATE_DERIVED,
    STATE_NO_MEASURE,
    STATE_NO_OBSERVATIONS,
    OutcomeProgress,
    compute_outcome_progress,
)

WEIGHT_KG_1 = Decimal("96.0")
WEIGHT_KG_2 = Decimal("94.0")


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username="bot:wellness-progress", password="x", role="client",
        phone="+79995000022", is_proxy=True,
    )


@pytest.fixture
def registry_weight(db):
    return EvidenceRegistryEntry.objects.create(
        outcome_target="body_weight",
        observation_type=ProgressObservation.ObservationType.WEIGHT,
        origin=ProgressObservation.Origin.USER_STATED,
        instrument="",
        approved_by="owner",
        approved_at=timezone.now(),
    )


@pytest.fixture
def outcome(db, customer, registry_weight):
    outcome = DesiredOutcome.objects.create(
        user=customer, target="body_weight",
        statement_text="хочу сбросить 10 кг",
        direction=DesiredOutcome.Direction.REDUCE,
        desired_state_numeric=Decimal("90.0"),
    )
    # Ряд наблюдений в тестах живёт последние недели; amendment A требует
    # observed_at >= outcome.created_at, поэтому «результат заявлен месяц
    # назад». auto_now_add действует только на insert — отматываем update'ом.
    DesiredOutcome.objects.filter(pk=outcome.pk).update(
        created_at=timezone.now() - timedelta(days=31),
    )
    outcome.refresh_from_db()
    return outcome


def _weight_row(user, value, *, days_ago=0):
    return ProgressObservation.objects.create(
        user=user,
        observation_type=ProgressObservation.ObservationType.WEIGHT,
        value_numeric=value,
        observed_at=timezone.now() - timedelta(days=days_ago),
    )


def _without_computed_at(progress: OutcomeProgress) -> dict:
    data = asdict(progress)
    data.pop("computed_at")
    return data


@pytest.mark.django_db
class TestStates:
    def test_no_measure_when_target_not_in_registry(self, customer):
        outcome = DesiredOutcome.objects.create(
            user=customer, target="edema",
            statement_text="хочу уменьшить отёчность",
            direction=DesiredOutcome.Direction.REDUCE,
        )
        progress = compute_outcome_progress(outcome)
        assert progress.state == STATE_NO_MEASURE

    def test_no_observations(self, outcome):
        progress = compute_outcome_progress(outcome)
        assert progress.state == STATE_NO_OBSERVATIONS
        assert progress.baseline_value is None
        assert progress.delta is None

    def test_baseline_only(self, customer, outcome):
        _weight_row(customer, WEIGHT_KG_1)
        progress = compute_outcome_progress(outcome)
        assert progress.state == STATE_BASELINE_ONLY
        assert progress.baseline_value == WEIGHT_KG_1
        assert progress.latest_value is None
        assert progress.delta is None

    def test_derived_is_plain_arithmetic(self, customer, outcome):
        _weight_row(customer, WEIGHT_KG_1, days_ago=14)
        _weight_row(customer, WEIGHT_KG_2, days_ago=1)
        progress = compute_outcome_progress(outcome)
        assert progress.state == STATE_DERIVED
        assert progress.baseline_value == WEIGHT_KG_1
        assert progress.latest_value == WEIGHT_KG_2
        assert progress.delta == WEIGHT_KG_2 - WEIGHT_KG_1
        assert progress.desired_state == Decimal("90.0")


@pytest.mark.django_db
class TestBaselineRules:
    def test_observation_before_outcome_is_not_baseline(
        self, customer, outcome, registry_weight,
    ):
        """Amendment A: история до появления результата — не его прогресс."""
        _weight_row(customer, Decimal("100.0"), days_ago=40)
        # фикстура отмотана на 31 день; наблюдение 40-дневной давности —
        # до появления результата
        progress = compute_outcome_progress(outcome)
        assert progress.state == STATE_NO_OBSERVATIONS

    def test_new_plan_does_not_reset_baseline(self, customer, outcome):
        """Amendment A: baseline привязан к outcome, не к связи (GOALS-R4)."""
        _weight_row(customer, WEIGHT_KG_1, days_ago=14)
        _weight_row(customer, WEIGHT_KG_2, days_ago=1)
        before = _without_computed_at(compute_outcome_progress(outcome))

        plan1 = PersonalPlan.objects.create(user=customer)
        link1 = PlanOutcomeLink.objects.create(plan=plan1, outcome=outcome)
        link1.status = PlanOutcomeLink.Status.CLOSED_BY_USER
        link1.closed_at = timezone.now()
        link1.save()
        plan1.status = PersonalPlan.Status.CLOSED_BY_USER
        plan1.closed_at = timezone.now()
        plan1.save()

        plan2 = PersonalPlan.objects.create(user=customer)
        PlanOutcomeLink.objects.create(plan=plan2, outcome=outcome)

        after = _without_computed_at(compute_outcome_progress(outcome))
        assert after == before

    def test_superseded_rows_are_excluded(self, customer, outcome):
        old = _weight_row(customer, Decimal("97.5"), days_ago=10)
        correction = _weight_row(customer, WEIGHT_KG_1, days_ago=9)
        old.superseded_by = correction
        old.save()
        _weight_row(customer, WEIGHT_KG_2, days_ago=1)

        progress = compute_outcome_progress(outcome)
        assert progress.state == STATE_DERIVED
        assert progress.baseline_value == WEIGHT_KG_1  # не 97.5
        assert progress.delta == WEIGHT_KG_2 - WEIGHT_KG_1

    def test_deletion_annuls_progress(self, customer, outcome):
        first = _weight_row(customer, WEIGHT_KG_1, days_ago=14)
        second = _weight_row(customer, WEIGHT_KG_2, days_ago=1)
        assert compute_outcome_progress(outcome).state == STATE_DERIVED

        second.delete()
        assert compute_outcome_progress(outcome).state == STATE_BASELINE_ONLY

        first.delete()
        assert compute_outcome_progress(outcome).state == STATE_NO_OBSERVATIONS


@pytest.mark.django_db
class TestActionsNeverMoveProgress:
    """Property-тест AYLA-DEC-0082: ни одно постороннее событие не меняет
    Outcome Progress ни на одно значение."""

    @pytest.mark.parametrize("noise_count", [0, 1, 3])
    def test_unrelated_facts_do_not_change_progress(
        self, customer, outcome, noise_count, registry_weight,
    ):
        _weight_row(customer, WEIGHT_KG_1, days_ago=14)
        _weight_row(customer, WEIGHT_KG_2, days_ago=1)
        before = _without_computed_at(compute_outcome_progress(outcome))

        for i in range(noise_count):
            # «Факт активности» в терминах этого домена: наблюдение другого
            # типа, не допустимого для target по реестру (self_assessment
            # для body_weight в реестре нет).
            ProgressObservation.objects.create(
                user=customer,
                observation_type=ProgressObservation.ObservationType.SELF_ASSESSMENT,
                value_ordinal=2,
                instrument=ProgressObservation.INSTRUMENT_NOTICEABILITY_0_3_V1,
                observed_at=timezone.now() - timedelta(days=i),
            )

        after = _without_computed_at(compute_outcome_progress(outcome))
        assert after == before


@pytest.mark.django_db
class TestInstrumentMatching:
    def test_self_assessment_series_with_versioned_instrument(
        self, db, customer,
    ):
        EvidenceRegistryEntry.objects.create(
            outcome_target="edema",
            observation_type=ProgressObservation.ObservationType.SELF_ASSESSMENT,
            origin=ProgressObservation.Origin.USER_STATED,
            instrument=ProgressObservation.INSTRUMENT_NOTICEABILITY_0_3_V1,
            approved_by="owner",
            approved_at=timezone.now(),
        )
        outcome = DesiredOutcome.objects.create(
            user=customer, target="edema",
            statement_text="хочу уменьшить отёчность",
            direction=DesiredOutcome.Direction.REDUCE,
        )
        DesiredOutcome.objects.filter(pk=outcome.pk).update(
            created_at=timezone.now() - timedelta(days=31),
        )
        outcome.refresh_from_db()
        ProgressObservation.objects.create(
            user=customer,
            observation_type=ProgressObservation.ObservationType.SELF_ASSESSMENT,
            value_ordinal=3,
            instrument=ProgressObservation.INSTRUMENT_NOTICEABILITY_0_3_V1,
            observed_at=timezone.now() - timedelta(days=7),
        )
        ProgressObservation.objects.create(
            user=customer,
            observation_type=ProgressObservation.ObservationType.SELF_ASSESSMENT,
            value_ordinal=1,
            instrument=ProgressObservation.INSTRUMENT_NOTICEABILITY_0_3_V1,
        )
        progress = compute_outcome_progress(outcome)
        assert progress.state == STATE_DERIVED
        assert progress.delta == Decimal(1) - Decimal(3)


PROGRESS_PATH = Path(__file__).resolve().parent.parent / "progress.py"


class TestModulePurity:
    def test_no_nutrition_imports_in_ast(self):
        """AST-разбор: ни один import не ссылается на nutrition."""
        tree = ast.parse(PROGRESS_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name != "nutrition" and not name.startswith("nutrition.")

    def test_no_path_to_profile_weight_in_ast(self):
        """Нет attribute/subscript-доступа к weight_kg/weight_range и нет
        getattr — проза в докстрингах допустима, пути к данным — нет."""
        tree = ast.parse(PROGRESS_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("weight_kg", "weight_range")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "getattr"

    def test_import_does_not_load_nutrition(self):
        """Свежий интерпретатор: импорт wellness.progress не добавляет
        ни одного модуля nutrition сверх базовой загрузки django.setup()
        (которая поднимает все INSTALLED_APPS и не зависит от нас)."""
        code = (
            "import django, os, sys; "
            "os.environ.setdefault("
            "'DJANGO_SETTINGS_MODULE', 'djangoProject.settings.test'); "
            "django.setup(); "
            "before = {m for m in sys.modules if m.startswith('nutrition')}; "
            "import wellness.progress; "
            "after = {m for m in sys.modules if m.startswith('nutrition')}; "
            "assert after - before == set(), after - before"
        )
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=PROGRESS_PATH.parent.parent,
            check=True,
        )

    def test_no_field_where_two_lines_meet(self):
        """Ни одного поля, куда входит вторая линия (adherence) или
        производная оценка (темп, прогноз, процент, цвет)."""
        names = {f.name for f in fields(OutcomeProgress)}
        assert names == {
            "state", "computed_at", "desired_state",
            "baseline_value", "baseline_at",
            "latest_value", "latest_at", "delta",
        }
