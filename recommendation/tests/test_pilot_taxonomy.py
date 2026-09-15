"""Таксономия пилота (DRF-1922): коды H5 / B9 — байт в байт с решением владельца и с ботом.

Что стережётся:

* **коды** ``Target`` / ``ActionType`` / ``Recommendation.Family`` / ``Recommendation.Role`` —
  строковым пином в порядке решения владельца. Пара с
  ``ai-bot-platform/apps/orchestrator/tests/test_nba_taxonomy.py::TestCodesAreTheOwnersBytes`` —
  менять вместе;
* значение = имя (коды видны в логах и в grep);
* **оси независимы** (I1 (а)): сочетания, которых «нет в таблице», запись принимает — таблицы нет;
* ``target`` и ``action_type`` обязательны на входе (вариант = NBA-исход, тройка обязательна);
* миграция 0004 отказывает на непустой таблице вариантов.
"""
from __future__ import annotations

import copy
import importlib

import pytest
from django.apps import apps as django_apps

from recommendation.models import ActionType, Recommendation, Target
from recommendation.records import (
    ContextSnapshotInput,
    PolicyVersions,
    RecommendationInput,
    RecommendationSetInput,
    RecordInvalid,
    persist,
)
from recommendation.snapshots import content_digest

pytestmark = pytest.mark.django_db

CONTENT = {
    "snapshot_version": "turn-context-v1",
    "decision_readiness": {"state_revision": 1, "readiness_state": "ready"},
    "said": [],
    "answered_question": None,
}


class TestCodesAreTheOwnersBytes:
    """Пара с ai-bot-platform/apps/orchestrator/tests/test_nba_taxonomy.py::TestCodesAreTheOwnersBytes.

    Менять вместе: коды — байты решения владельца (H5, B9), бот сверяет те же кортежи.
    """

    def test_targets_h5(self):
        # пара с ботом: test_nba_taxonomy.py::TestCodesAreTheOwnersBytes — менять вместе
        assert tuple(Target.values) == ("FACE_FRESHNESS", "PUFFINESS_REDUCTION", "RELAXATION", "BACK_COMFORT")

    def test_action_types_h5(self):
        # пара с ботом: test_nba_taxonomy.py::TestCodesAreTheOwnersBytes — менять вместе
        assert tuple(ActionType.values) == ("PROVIDER_SESSION", "SELF_CARE", "OBSERVE", "PLAN")

    def test_families_b9(self):
        # пара с ботом: test_nba_taxonomy.py::TestCodesAreTheOwnersBytes — менять вместе
        assert tuple(Recommendation.Family.values) == ("ADDRESS", "SUPPORT", "RECOVER", "OBSERVE")

    def test_roles_are_the_catalog_spelling(self):
        # пара с ботом: test_nba_taxonomy.py::TestCodesAreTheOwnersBytes — менять вместе
        assert tuple(Recommendation.Role.values) == ("primary", "alternative")


@pytest.mark.parametrize("enum", [Target, ActionType, Recommendation.Family])
def test_value_is_the_name(enum):
    for member in enum:
        assert member.value == member.name == member.label


def test_model_aliases_are_the_module_enums():
    assert Recommendation.Target is Target and Recommendation.ActionType is ActionType


def _set(primary: RecommendationInput) -> RecommendationSetInput:
    return RecommendationSetInput(
        subject_ref="taxonomy-subject", intent_id="intent-tax", versions=PolicyVersions("dp", "tx", "sp", "cm", "pp"),
        result_status="CLEAR_PRIMARY", readiness_state="READY", reason_codes=["CLEAR_PRIMARY_BY_POLICY"],
        evidence_refs=[], explanation={"displayable": False, "user_visible_reasons": [], "internal_only": []},
        safety_evaluation_ref={"state": "NORMAL", "rule_id": "r-0", "policy_version": "sp", "evidence_ref": "ev",
                               "activated_at": "2026-09-15T10:00:00Z"},
        context_snapshot=ContextSnapshotInput("turn-context-v1", content_digest(CONTENT), copy.deepcopy(CONTENT)),
        primary=primary,
    )


def _variant(**over) -> RecommendationInput:
    base = dict(
        role="primary", direction_code="REDUCE_MUSCLE_TENSION_BACK", family="ADDRESS",
        target="BACK_COMFORT", action_type="PROVIDER_SESSION", target_outcomes=[],
        reason_codes=["ELIG_CAPABILITY_VERIFIED"], evidence_refs=[],
        explanation={"displayable": False, "user_visible_reasons": [], "internal_only": []},
    )
    base.update(over)
    return RecommendationInput(**base)


@pytest.mark.parametrize("target, family, action_type", [
    ("BACK_COMFORT", "SUPPORT", "OBSERVE"),          # I1: пример из вопроса владельцу
    ("FACE_FRESHNESS", "OBSERVE", "PROVIDER_SESSION"),  # family.OBSERVE × action_type.PROVIDER_SESSION
    ("PUFFINESS_REDUCTION", "RECOVER", "PLAN"),
    ("RELAXATION", "ADDRESS", "SELF_CARE"),
])
def test_axes_are_independent_and_the_triple_is_stored(target, family, action_type):
    """I1 (а): таблицы сочетаний нет — каждая ось по своему словарю; тройка хранится у варианта."""
    rset = persist(_set(_variant(target=target, family=family, action_type=action_type)))
    stored = Recommendation.objects.get(pk=rset.primary_id)
    assert (stored.target, stored.family, stored.action_type) == (target, family, action_type)


def test_target_and_action_type_are_required_on_the_ingress_shape():
    """Вход без тройки — отказ по имени поля: вариант = NBA-исход, тройка обязательна."""
    from recommendation.record_api import _record_input

    raw = {
        "role": "primary", "direction_code": "REDUCE_MUSCLE_TENSION_BACK", "family": "ADDRESS",
        "target_outcomes": [], "reason_codes": ["ELIG_CAPABILITY_VERIFIED"], "evidence_refs": [],
        "explanation": {"displayable": False, "user_visible_reasons": [], "internal_only": []},
    }
    with pytest.raises(RecordInvalid, match="target"):
        _record_input(dict(raw, action_type="PROVIDER_SESSION"), "primary")
    with pytest.raises(RecordInvalid, match="action_type"):
        _record_input(dict(raw, target="BACK_COMFORT"), "primary")


def test_migration_0004_refuses_a_non_empty_variant_table():
    migration = importlib.import_module("recommendation.migrations.0004_pilot_taxonomy")
    migration.refuse_if_records_exist(django_apps, None)         # пустая таблица — проходит
    persist(_set(_variant()))
    with pytest.raises(RuntimeError, match="безопасна только на пустой"):
        migration.refuse_if_records_exist(django_apps, None)
