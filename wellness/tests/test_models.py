"""Модельные инварианты домена wellness (PROPOSAL_GOALS_MODEL_FINAL §2–§6).

Coverage:
- DesiredOutcome: CheckConstraint OD-DC-1 (direction или desired_state_numeric);
  НЕТ ограничения «одна активная» — несколько OPEN легальны (§2);
- PersonalPlan: partial unique — не более одного ACTIVE на пользователя;
- PlanOutcomeLink: partial unique — не более одной ACTIVE на outcome (GOALS-R4);
  horizon_status — вычислимое property (none/upcoming/elapsed), не колонка;
- ProgressObservation: CheckConstraint «заполнена ровно колонка своего типа» (§4);
- EvidenceRegistryEntry: unique_together по четвёрке (amendment B).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError
from django.utils import timezone

from users.models import User
from wellness.models import (
    DesiredOutcome,
    EvidenceRegistryEntry,
    PersonalPlan,
    PlanOutcomeLink,
    ProgressObservation,
)


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username="bot:wellness-models", password="x", role="client",
        phone="+79995000011", is_proxy=True,
    )


@pytest.fixture
def outcome(db, customer):
    return DesiredOutcome.objects.create(
        user=customer, target="body_weight",
        statement_text="хочу весить 65 кг",
        direction=DesiredOutcome.Direction.REDUCE,
    )


@pytest.fixture
def plan(db, customer):
    return PersonalPlan.objects.create(user=customer)


# ---------------------------------------------------------------------------
# DesiredOutcome
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDesiredOutcome:
    def test_direction_and_numeric_both_null_rejected(self, customer):
        """OD-DC-1: хотя бы одно из direction / desired_state_numeric."""
        with pytest.raises(IntegrityError):
            DesiredOutcome.objects.create(
                user=customer, target="edema", statement_text="меньше отёков",
            )

    def test_direction_only_ok(self, customer):
        DesiredOutcome.objects.create(
            user=customer, target="edema", statement_text="меньше отёков",
            direction=DesiredOutcome.Direction.REDUCE,
        )

    def test_numeric_only_ok(self, customer):
        DesiredOutcome.objects.create(
            user=customer, target="body_weight", statement_text="ровно 65",
            desired_state_numeric=Decimal("65.00"),
        )

    def test_many_open_outcomes_allowed(self, customer):
        """§2: 0..N активных с первого дня — аналога one_active нет."""
        for i in range(3):
            DesiredOutcome.objects.create(
                user=customer, target=f"target-{i}", statement_text=f"цель {i}",
                direction=DesiredOutcome.Direction.MAINTAIN,
            )
        assert DesiredOutcome.objects.filter(
            user=customer, status=DesiredOutcome.Status.OPEN,
        ).count() == 3


# ---------------------------------------------------------------------------
# PersonalPlan
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPersonalPlan:
    def test_second_active_rejected(self, customer):
        PersonalPlan.objects.create(user=customer)
        with pytest.raises(IntegrityError):
            PersonalPlan.objects.create(user=customer)

    def test_second_active_allowed_after_close(self, customer):
        first = PersonalPlan.objects.create(user=customer)
        first.status = PersonalPlan.Status.CLOSED_BY_USER
        first.closed_at = timezone.now()
        first.save()
        PersonalPlan.objects.create(user=customer)
        assert PersonalPlan.objects.filter(user=customer).count() == 2


# ---------------------------------------------------------------------------
# PlanOutcomeLink
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPlanOutcomeLink:
    def test_second_active_link_on_outcome_rejected(self, plan, outcome):
        PlanOutcomeLink.objects.create(plan=plan, outcome=outcome)
        with pytest.raises(IntegrityError):
            PlanOutcomeLink.objects.create(plan=plan, outcome=outcome)

    def test_second_link_allowed_after_close(self, plan, outcome):
        """GOALS-R4 + amendment A: продолжение — новая строка, старая история."""
        first = PlanOutcomeLink.objects.create(plan=plan, outcome=outcome)
        first.status = PlanOutcomeLink.Status.CLOSED_BY_USER
        first.closed_at = timezone.now()
        first.save()
        PlanOutcomeLink.objects.create(plan=plan, outcome=outcome)
        assert PlanOutcomeLink.objects.filter(outcome=outcome).count() == 2

    def test_target_date_null_legal(self, plan, outcome):
        link = PlanOutcomeLink.objects.create(plan=plan, outcome=outcome)
        assert link.target_date is None

    def test_horizon_status_is_computed_property(self, plan, outcome):
        """§6: horizon_status вычисляется, колонкой не хранится."""
        assert not any(
            f.name == "horizon_status" for f in PlanOutcomeLink._meta.fields
        )
        today = timezone.localdate()
        cases = [
            (None, PlanOutcomeLink.HorizonStatus.NONE),
            (today + timedelta(days=10), PlanOutcomeLink.HorizonStatus.UPCOMING),
            (today, PlanOutcomeLink.HorizonStatus.UPCOMING),
            (today - timedelta(days=1), PlanOutcomeLink.HorizonStatus.ELAPSED),
        ]
        for target_date, expected in cases:
            link = PlanOutcomeLink(plan=plan, outcome=outcome, target_date=target_date)
            assert link.horizon_status == expected


# ---------------------------------------------------------------------------
# ProgressObservation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProgressObservation:
    def test_weight_valid(self, customer):
        ProgressObservation.objects.create(
            user=customer,
            observation_type=ProgressObservation.ObservationType.WEIGHT,
            value_numeric=Decimal("70.50"),
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},  # weight без value_numeric
            {"value_ordinal": 2},  # weight с ordinal
            {"instrument": "SOME_SCALE_V1"},  # weight с instrument
        ],
    )
    def test_weight_invalid_shape_rejected(self, customer, kwargs):
        with pytest.raises(IntegrityError):
            ProgressObservation.objects.create(
                user=customer,
                observation_type=ProgressObservation.ObservationType.WEIGHT,
                **kwargs,
            )

    def test_self_assessment_valid(self, customer):
        ProgressObservation.objects.create(
            user=customer,
            observation_type=ProgressObservation.ObservationType.SELF_ASSESSMENT,
            value_ordinal=2,
            instrument=ProgressObservation.INSTRUMENT_NOTICEABILITY_0_3_V1,
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"value_ordinal": 2},  # без instrument (amendment B)
            {"instrument": ProgressObservation.INSTRUMENT_NOTICEABILITY_0_3_V1},  # без ordinal
            {"value_numeric": Decimal("2.0"), "value_ordinal": 2,
             "instrument": ProgressObservation.INSTRUMENT_NOTICEABILITY_0_3_V1},  # numeric запрещён
        ],
    )
    def test_self_assessment_invalid_shape_rejected(self, customer, kwargs):
        with pytest.raises(IntegrityError):
            ProgressObservation.objects.create(
                user=customer,
                observation_type=ProgressObservation.ObservationType.SELF_ASSESSMENT,
                **kwargs,
            )

    def test_origin_only_user_stated(self):
        """Структурный запрет выводимого: inferred/derived нет в перечислении."""
        values = [c.value for c in ProgressObservation.Origin]
        assert values == ["user_stated"]

    def test_supersede_self_fk(self, customer):
        """§8: исправление — append-only supersede, старая строка хранится."""
        first = ProgressObservation.objects.create(
            user=customer,
            observation_type=ProgressObservation.ObservationType.WEIGHT,
            value_numeric=Decimal("70.0"),
        )
        correction = ProgressObservation.objects.create(
            user=customer,
            observation_type=ProgressObservation.ObservationType.WEIGHT,
            value_numeric=Decimal("70.5"),
        )
        first.superseded_by = correction
        first.save()
        assert ProgressObservation.objects.count() == 2


# ---------------------------------------------------------------------------
# EvidenceRegistryEntry
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEvidenceRegistryEntry:
    def _entry(self, **overrides):
        defaults = {
            "outcome_target": "body_weight",
            "observation_type": ProgressObservation.ObservationType.WEIGHT,
            "origin": ProgressObservation.Origin.USER_STATED,
            "instrument": "",
            "approved_by": "owner",
            "approved_at": timezone.now(),
        }
        defaults.update(overrides)
        return EvidenceRegistryEntry.objects.create(**defaults)

    def test_first_slice_entries(self):
        """§5: (BODY_WEIGHT, WEIGHT, user_stated, NULL) и
        (EDEMA, SELF_ASSESSMENT, user_stated, NOTICEABILITY_0_3_V1)."""
        self._entry()
        self._entry(
            outcome_target="edema",
            observation_type=ProgressObservation.ObservationType.SELF_ASSESSMENT,
            instrument=ProgressObservation.INSTRUMENT_NOTICEABILITY_0_3_V1,
        )
        assert EvidenceRegistryEntry.objects.count() == 2

    def test_duplicate_quadruple_rejected(self):
        self._entry()
        with pytest.raises(IntegrityError):
            self._entry()

    def test_instrument_distinguishes_entries(self):
        """Пустой instrument ("") и код шкалы — разные записи реестра."""
        self._entry()
        self._entry(instrument="SOME_SCALE_V1")
        assert EvidenceRegistryEntry.objects.count() == 2
