"""Outcome Progress — производная над рядом наблюдений (DRF-1343).

Контракт: docs/PROPOSAL_GOALS_MODEL_FINAL.md §5 (baseline, amendment A)
и §9 (состояния); docs/SPEC_CARE_CONTRACT.md §3.1 (что разрешено говорить).

- **Производная, не сущность.** Ничего не хранит: всё пересчитывается из
  наблюдений на каждый вызов. Удалили наблюдение — прогресс исчез сам.
  Результат помечен `computed_at` и не является источником истины.
- **Только наблюдения результата.** Ряд — записи `ProgressObservation`,
  допустимые для `outcome.target` по Evidence Registry (тройка
  тип × origin × instrument, amendment B), не superseded.
- **Baseline — не раньше `outcome.created_at`** (amendment A): история
  человека до появления результата не становится его прогрессом. Baseline
  привязан к outcome, не к связи: новый Personal Plan его НЕ сбрасывает.
- **Никогда не читает `NutritionProfile`** — ни `weight_kg`, ни
  `weight_range`, ни через сериализатор. Ноль наблюдений — это
  `no_observations`, а не подстановка из профиля (О-3).
- **Ни темпа, ни экстраполяции, ни оценочных формулировок.** Модуль
  возвращает структуру, не текст.
- **Линии не сходятся.** Здесь нет и не может быть поля, куда входит
  выполнение действий (Plan Adherence — отдельная линия, AYLA-DEC-0082).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db.models import Q
from django.utils import timezone

from .models import EvidenceRegistryEntry, ProgressObservation

if TYPE_CHECKING:
    from .models import DesiredOutcome

# Состояния прогресса (§9.2 proposal; экран получает их готовыми —
# «тупой отрисовщик» ничего не вычисляет сам).
STATE_NO_MEASURE = "no_measure"
STATE_NO_OBSERVATIONS = "no_observations"
STATE_BASELINE_ONLY = "baseline_only"
STATE_DERIVED = "derived"


@dataclass(frozen=True)
class OutcomeProgress:
    """Снапшот прогресса результата. Числа — только из наблюдений.

    `desired_state` — не вычислено, а перенесено из DesiredOutcome как
    есть (это заявленное человеком желаемое состояние, не прогресс).
    """

    state: str
    computed_at: datetime
    desired_state: Decimal | None
    baseline_value: Decimal | None = None
    baseline_at: datetime | None = None
    latest_value: Decimal | None = None
    latest_at: datetime | None = None
    delta: Decimal | None = None


def _series_query(outcome: DesiredOutcome):
    """Допустимый ряд наблюдений для результата.

    Фильтр строится из Evidence Registry (не хардкод): на каждую запись
    реестра — пара (observation_type, instrument). Реестра нет — вызывающий
    получает `no_measure`, а не пустой ряд.
    """
    entries = EvidenceRegistryEntry.objects.filter(
        outcome_target=outcome.target,
        origin=ProgressObservation.Origin.USER_STATED,
    )
    allowed = Q(pk__in=[])  # пустое условие; реестр пуст -> ряд пуст
    for entry in entries:
        allowed |= Q(
            observation_type=entry.observation_type,
            instrument=entry.instrument or None,
        )
    return (
        ProgressObservation.objects.filter(
            user_id=outcome.user_id,
            origin=ProgressObservation.Origin.USER_STATED,
            superseded_by__isnull=True,
            # amendment A: история до появления результата — не его прогресс
            observed_at__gte=outcome.created_at,
        )
        .filter(allowed)
        .order_by("observed_at", "created_at"),
        entries.exists(),
    )


def _value(row: ProgressObservation) -> Decimal:
    """Значение наблюдения как Decimal — по типу, из своей колонки (§4)."""
    if row.observation_type == ProgressObservation.ObservationType.WEIGHT:
        return row.value_numeric  # type: ignore[return-value]
    return Decimal(row.value_ordinal)  # type: ignore[arg-type]


def compute_outcome_progress(outcome: DesiredOutcome) -> OutcomeProgress:
    """Вычислить прогресс результата из его ряда наблюдений.

    Состояния: `no_measure` (в реестре нет допустимого свидетельства —
    нормальное состояние по OD-GOAL-3), `no_observations`, `baseline_only`
    (одна точка — старт, не прогресс), `derived` (старт · последнее ·
    разность — только арифметика, SPEC §3.1).
    """
    now = timezone.now()
    rows, has_measure = _series_query(outcome)
    if not has_measure:
        return OutcomeProgress(
            state=STATE_NO_MEASURE,
            computed_at=now,
            desired_state=outcome.desired_state_numeric,
        )

    series = list(rows)
    if not series:
        return OutcomeProgress(
            state=STATE_NO_OBSERVATIONS,
            computed_at=now,
            desired_state=outcome.desired_state_numeric,
        )

    baseline = series[0]
    if len(series) == 1:
        return OutcomeProgress(
            state=STATE_BASELINE_ONLY,
            computed_at=now,
            desired_state=outcome.desired_state_numeric,
            baseline_value=_value(baseline),
            baseline_at=baseline.observed_at,
        )

    latest = series[-1]
    return OutcomeProgress(
        state=STATE_DERIVED,
        computed_at=now,
        desired_state=outcome.desired_state_numeric,
        baseline_value=_value(baseline),
        baseline_at=baseline.observed_at,
        latest_value=_value(latest),
        latest_at=latest.observed_at,
        delta=_value(latest) - _value(baseline),
    )
