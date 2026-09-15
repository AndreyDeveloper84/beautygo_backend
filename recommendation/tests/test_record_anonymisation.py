"""Обезличивание записи Recommendation (DRF-1909): переход и перепись полей.

Что стережётся:

* **перепись**: каждое concrete-поле набора, варианта и события названо в
  ``ANONYMISATION_FIELDS`` с классом ``clear`` / ``keep`` / ``keep-ref`` и причиной —
  ровно, в обе стороны, нижняя граница поимённо; событий набора без варианта модель
  не допускает (FK NOT NULL);
* **переход по полю**: каждое ``clear``-поле после перехода — ожидаемое пустое
  (параметр на поле; до перехода оно не пустое — положительная стража);
  ``keep``-поля прежние; ``keep-ref`` — ровно три ключа ссылки, в том числе у строки,
  пронёсшей лишний ключ до закрытия входа;
* **повтор** ничего не меняет; вызов с tombstone или пустым субъектом — отказ до записи;
* чужой набор не тронут; прочие ``update`` по-прежнему отказывают.
"""
from __future__ import annotations

import copy
import re
import uuid

import pytest
from django.db import models

from recommendation.models import (
    ANONYMISATION_FIELDS,
    SNAPSHOT_REF_KEYS,
    ImmutableRecordError,
    Recommendation,
    RecommendationEvent,
    RecommendationSet,
)
from recommendation.records import (
    ContextSnapshotInput,
    PolicyVersions,
    RecommendationInput,
    RecommendationSetInput,
    persist,
    record_event,
)
from recommendation.snapshots import content_digest

pytestmark = pytest.mark.django_db

SUBJECT = "anon-subject-1"
STRANGER = "anon-stranger-1"
TOMBSTONE = "anon-tombstone-pk"
OUTCOME_ID = str(uuid.uuid4())
MODELS = {
    "recommendation.RecommendationSet": RecommendationSet,
    "recommendation.Recommendation": Recommendation,
    "recommendation.RecommendationEvent": RecommendationEvent,
}
CONTENT = {
    "snapshot_version": "turn-context-v1",
    "decision_readiness": {"state_revision": 3, "readiness_state": "ready"},
    "said": [{"key": "visit_context", "value": "after_work", "origin": "conversation", "said_on": "2026-09-15"}],
    "answered_question": None,
}


def _ref(prefix: str) -> dict:
    return {"snapshot_id": f"{prefix}-1", "snapshot_version": 1, "content_digest": f"sha256:{prefix}"}


def _variant(role: str = "primary", **over) -> RecommendationInput:
    base = dict(
        role=role, direction_code="REDUCE_MUSCLE_TENSION_BACK", family="ADDRESS",
        target_outcomes=[OUTCOME_ID], reason_codes=["ELIG_CAPABILITY_VERIFIED"],
        evidence_refs=[{"source": "conversation", "ref": "msg-7"}],
        explanation={"displayable": True, "user_visible_reasons": ["ты сказала, что ноет спина"],
                     "internal_only": ["ELIG_CAPABILITY_VERIFIED"]},
        consent_evaluation_ref={"consent_id": "consent-7", "state": "GRANTED"},
        memory_snapshot_ref=_ref("mem"), execution_mapping_snapshot_ref=_ref("exec"),
        transaction_snapshot_ref=_ref("tx"),
    )
    if role == "alternative":
        base["rerank_reason"] = "ALTERNATIVE_REQUESTED"
    base.update(over)
    return RecommendationInput(**base)


def _record(subject_ref: str) -> RecommendationSet:
    """NBA-исход со всеми полями, которые чистит переход, заполненными не пусто, и событием канала."""
    rset = persist(RecommendationSetInput(
        subject_ref=subject_ref, intent_id=str(uuid.uuid4()), versions=PolicyVersions("dp", "tx", "sp", "cm", "pp"),
        result_status="CLEAR_PRIMARY", readiness_state="READY", reason_codes=["CLEAR_PRIMARY_BY_POLICY"],
        evidence_refs=[{"source": "conversation", "ref": "msg-set"}],
        explanation={"displayable": True, "user_visible_reasons": ["подходит под твою цель"],
                     "internal_only": ["CLEAR_PRIMARY_BY_POLICY"]},
        safety_evaluation_ref={"state": "NORMAL", "rule_id": "r-0", "policy_version": "sp",
                               "evidence_ref": "ev-msg-7", "activated_at": "2026-09-15T10:00:00Z"},
        context_snapshot=ContextSnapshotInput("turn-context-v1", content_digest(CONTENT), copy.deepcopy(CONTENT)),
        primary=_variant(),
        alternatives=(_variant("alternative", direction_code="IMPROVE_RELAXATION", family="SUPPORT"),),
        semantic_resolution_ref="sem-7", conversation_ref={"conversation_id": "conv-7", "trace_id": "trace-7"},
    ))
    for rec in rset.recommendations.all():
        record_event(rec, "recommendation.presented", payload={"channel": "max", "channel_message_id": "max-msg-7"})
    return rset


def _row(obj) -> dict:
    return {f.name: getattr(obj, f.attname) for f in obj._meta.get_fields() if getattr(f, "concrete", False)}


def _snapshot(rset_pk) -> dict[str, list[dict]]:
    return {
        "recommendation.RecommendationSet": [_row(RecommendationSet.objects.get(pk=rset_pk))],
        "recommendation.Recommendation": [
            _row(r) for r in Recommendation.objects.filter(recommendation_set_id=rset_pk).order_by("pk")
        ],
        "recommendation.RecommendationEvent": [
            _row(e) for e in RecommendationEvent.objects.filter(recommendation__recommendation_set_id=rset_pk)
            .order_by("pk")
        ],
    }


def _expected(label: str, field: str, row: dict):
    """Значение ``clear``-поля после перехода. Нет строки — новое clear-поле без ожидания: KeyError."""
    wiped = {**(row.get("explanation") or {}), "user_visible_reasons": [], "internal_only": []}
    if label == "recommendation.RecommendationSet":
        return {
            "subject_ref": TOMBSTONE, "intent_id": "", "semantic_resolution_ref": "", "conversation_ref": {},
            "evidence_refs": [], "explanation": wiped,
            "safety_evaluation_ref": {**row["safety_evaluation_ref"], "evidence_ref": ""},
        }[field]
    if label == "recommendation.Recommendation":
        return {
            "target_outcomes": [], "evidence_refs": [], "explanation": wiped,
            "consent_evaluation_ref": {}, "memory_snapshot_ref": None,
        }[field]
    return {"payload": {k: v for k, v in row["payload"].items() if k != "channel_message_id"}}[field]


# ---------------------------------------------------------------- перепись

def test_every_concrete_field_is_classified_exactly():
    assert set(ANONYMISATION_FIELDS) == set(MODELS)
    for label, model in MODELS.items():
        concrete = {f.name for f in model._meta.get_fields() if getattr(f, "concrete", False)}
        named = set(ANONYMISATION_FIELDS[label])
        assert named == concrete, (label, "без решения:", sorted(concrete - named), "лишние:", sorted(named - concrete))


def test_census_lower_bound_by_name():
    """Перепись, нашедшая меньше или не те поля, смотрит не туда."""
    s, r, e = (ANONYMISATION_FIELDS[k] for k in MODELS)
    assert len(s) >= 20 and {"subject_ref", "conversation_ref", "intent_id", "safety_evaluation_ref"} <= set(s)
    assert len(r) >= 20 and {"target_outcomes", "memory_snapshot_ref", "consent_evaluation_ref"} <= set(r)
    assert len(e) >= 6 and {"payload", "recommendation"} <= set(e)


def test_every_field_has_a_class_and_a_reason():
    for label, fields in ANONYMISATION_FIELDS.items():
        for field, why in fields.items():
            assert re.match(r"^(clear|keep|keep-ref): \S", why), (label, field, why)


def test_execution_mapping_ref_names_the_missing_object():
    why = ANONYMISATION_FIELDS["recommendation.Recommendation"]["execution_mapping_snapshot_ref"]
    assert why.startswith("keep-ref:") and "ExecutionOption не существует" in why


def test_an_event_always_belongs_to_a_variant():
    """Событий набора без варианта модель не допускает; разрешат null — нужно решение для перехода."""
    assert RecommendationEvent._meta.get_field("recommendation").null is False


# ---------------------------------------------------------------- переход по полю

CLEARED = [
    (label, field) for label, fields in ANONYMISATION_FIELDS.items()
    for field, why in fields.items() if why.startswith("clear:")
]


@pytest.mark.parametrize("label, field", CLEARED, ids=[f"{lbl.split('.')[-1]}.{fld}" for lbl, fld in CLEARED])
def test_every_cleared_field_is_cleared(label, field):
    rset = _record(SUBJECT)
    before = _snapshot(rset.pk)
    # Положительная стража: до перехода поле не равно ожидаемому пустому — иначе «пусто после» ничего не доказывает.
    assert any(row[field] != _expected(label, field, row) for row in before[label]), (label, field)

    assert RecommendationSet.objects.anonymise_for_subject(SUBJECT, TOMBSTONE) == 1

    after = _snapshot(rset.pk)
    for was, now in zip(before[label], after[label]):
        assert now[field] == _expected(label, field, was), (label, field)


def test_kept_fields_are_unchanged_and_refs_keep_exactly_three_keys():
    rset = _record(SUBJECT)
    primary = rset.recommendations.get(role="primary")
    # Строка, пронёсшая лишний ключ до закрытия формы входа (DRF-1909 (б)): мимо persist.
    models.QuerySet.update(
        Recommendation.objects.filter(pk=primary.pk),
        execution_mapping_snapshot_ref={**_ref("exec"), "client_ref": "client-7"},
    )
    before = _snapshot(rset.pk)

    RecommendationSet.objects.anonymise_for_subject(SUBJECT, TOMBSTONE)

    after = _snapshot(rset.pk)
    for label, fields in ANONYMISATION_FIELDS.items():
        for field, why in fields.items():
            if why.startswith("keep:"):
                assert [r[field] for r in after[label]] == [r[field] for r in before[label]], (label, field)
            elif why.startswith("keep-ref:"):
                assert all(
                    r[field] is None or set(r[field]) == set(SNAPSHOT_REF_KEYS) for r in after[label]
                ), (label, field)
    assert Recommendation.objects.get(pk=primary.pk).execution_mapping_snapshot_ref == _ref("exec")


def test_repeat_changes_nothing():
    """Исполнитель может повторить шаг после отката — второй проход не находит набор и ничего не пишет."""
    rset = _record(SUBJECT)
    assert RecommendationSet.objects.anonymise_for_subject(SUBJECT, TOMBSTONE) == 1
    once = _snapshot(rset.pk)
    assert RecommendationSet.objects.anonymise_for_subject(SUBJECT, TOMBSTONE) == 0
    assert _snapshot(rset.pk) == once


@pytest.mark.parametrize("subject", [TOMBSTONE, ""])
def test_tombstone_or_empty_subject_is_refused_before_any_write(subject):
    """С tombstone как субъектом переход прошёл бы по наборам всех удалённых людей."""
    rset = _record(SUBJECT)
    before = _snapshot(rset.pk)
    with pytest.raises(ValueError, match="tombstone"):
        RecommendationSet.objects.anonymise_for_subject(subject, TOMBSTONE)
    assert _snapshot(rset.pk) == before


def test_a_stranger_is_untouched():
    _record(SUBJECT)
    theirs = _record(STRANGER)
    before = _snapshot(theirs.pk)
    RecommendationSet.objects.anonymise_for_subject(SUBJECT, TOMBSTONE)
    assert _snapshot(theirs.pk) == before


def test_other_changes_are_still_refused():
    rset = _record(SUBJECT)
    with pytest.raises(ImmutableRecordError):
        RecommendationSet.objects.filter(pk=rset.pk).update(intent_id="")
    with pytest.raises(ImmutableRecordError):
        Recommendation.objects.filter(recommendation_set=rset).update(evidence_refs=[])
    with pytest.raises(ImmutableRecordError):
        RecommendationEvent.objects.all().update(payload={})
