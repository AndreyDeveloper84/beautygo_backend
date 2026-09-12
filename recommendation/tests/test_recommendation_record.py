"""Объект Recommendation (контракт v1.0 §3, Final Reconciliation §5) — immutable, с провенансом, expires ≠ delete.

Что стережётся:

* запись пишется только через ``records.persist``: set + primary + ≤2 alternatives
  + событие ``created``; минимум §5 и версии политик обязательны (отказ по имени поля);
* **immutable**: ``save()`` существующей строки, ``QuerySet.update()``,
  ``bulk_update()``, ``delete()`` — ``ImmutableRecordError``; ничего не меняется;
* **expires ≠ delete**: через 2 ч ``is_actionable`` ложь, запись на месте;
* decision_subject = NBA/WHAT (B2): у модели **нет** полей execution-слоя
  (service/provider/price/slot/distance/rank/candidate) — список запрещённых имён;
* три снимка — ссылки (B10): копия содержимого вместо ref — отказ;
* lineage: alternative без parent/rerank_reason — отказ; > 2 alternatives — отказ;
  одна primary на set (constraint);
* события: ``accepted``/``declined`` не существуют (B8); ``created`` пишет только
  persist; append-only.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from recommendation.models import (
    ACTIONABILITY_TTL,
    ImmutableRecordError,
    Recommendation,
    RecommendationEvent,
    RecommendationSet,
)
from recommendation.records import (
    PolicyVersions,
    RecommendationInput,
    RecommendationSetInput,
    RecordInvalid,
    persist,
    record_event,
)

pytestmark = pytest.mark.django_db

VERSIONS = PolicyVersions(
    decision_policy="dp-2026-09-12", taxonomy="tx-2026-07", safety_policy="sp-1",
    catalog_mapping="cm-2026-09-12", presentation_policy="pp-1",
)
SNAPSHOT = {"snapshot_id": "ctx-1", "snapshot_version": 1, "content_digest": "sha256:abc"}
SAFETY = {"state": "NORMAL", "rule_id": "r-0", "policy_version": "sp-1", "evidence_ref": "ev-0",
          "activated_at": "2026-09-12T10:00:00Z"}


def _rec(role="primary", **over) -> RecommendationInput:
    base = dict(
        role=role, direction_code="REDUCE_MUSCLE_TENSION_BACK", family="ADDRESS",
        target_outcomes=["REDUCE(MUSCLE_TENSION)"], result_status="CLEAR_PRIMARY", readiness_state="READY",
        reason_codes=["ELIG_CAPABILITY_VERIFIED"], evidence_refs=[{"kind": "user_stated", "ref": "msg-1"}],
        explanation={"displayable": True, "user_visible_reasons": ["ты сказала, что ноет спина"], "internal_only": []},
        safety_evaluation_ref=SAFETY, context_snapshot_ref=SNAPSHOT,
    )
    if role == "alternative":
        base["rerank_reason"] = "ALTERNATIVE_REQUESTED"
    base.update(over)
    return RecommendationInput(**base)


def _set(**over) -> RecommendationSetInput:
    base = dict(subject_ref="user:pseudo-1", intent_id="intent-1", versions=VERSIONS, primary=_rec())
    base.update(over)
    return RecommendationSetInput(**base)


# ---------------------------------------------------------------- запись

def test_persist_writes_set_primary_alternatives_and_created_events():
    now = timezone.now()
    alt_in = _rec("alternative", direction_code="IMPROVE_RELAXATION", family="SUPPORT")
    rset = persist(_set(alternatives=(alt_in,)), now=now)
    assert RecommendationSet.objects.count() == 1 and Recommendation.objects.count() == 2
    primary = rset.primary
    assert primary.role == "primary" and primary.parent is None and primary.rerank_reason == ""
    alt = rset.recommendations.get(role="alternative")
    assert alt.parent_id == primary.pk and alt.rerank_reason == "ALTERNATIVE_REQUESTED"
    assert primary.actionable_until == now + ACTIONABILITY_TTL == now + timedelta(hours=2)
    assert primary.decision_policy_version == "dp-2026-09-12" and primary.record_schema_version == "1.0"
    assert list(RecommendationEvent.objects.values_list("kind", flat=True)) == ["recommendation.created"] * 2


def test_no_action_is_a_valid_recorded_result():
    primary = _rec(result_status="NO_ACTION", family="OBSERVE", readiness_state="INSUFFICIENT_EVIDENCE")
    rset = persist(_set(primary=primary))
    assert rset.primary.result_status == "NO_ACTION"


@pytest.mark.parametrize("bad, match", [
    ({"direction_code": ""}, "direction_code"),
    ({"family": "INTERVENE"}, "family"),
    ({"reason_codes": []}, "reason_codes"),
    ({"explanation": {"user_visible_reasons": []}}, "displayable"),
    ({"safety_evaluation_ref": {"state": "NORMAL"}}, "safety_evaluation_ref"),
    ({"context_snapshot_ref": {"facts": {"age": 30}}}, "context_snapshot_ref"),
    ({"memory_snapshot_ref": {"entries": ["copied value"]}}, "memory_snapshot_ref"),
    ({"result_status": "ACCEPTED"}, "result_status"),
    ({"readiness_state": "MAYBE"}, "readiness_state"),
])
def test_incomplete_decision_is_refused_by_field_name(bad, match):
    with pytest.raises(RecordInvalid, match=match):
        persist(_set(primary=_rec(**bad)))
    assert Recommendation.objects.count() == 0


def test_missing_policy_version_is_refused_by_name():
    versions = PolicyVersions(
        decision_policy="dp", taxonomy="", safety_policy="sp", catalog_mapping="", presentation_policy="pp",
    )
    with pytest.raises(RecordInvalid, match="taxonomy, catalog_mapping"):
        persist(_set(versions=versions))


def test_alternative_needs_parent_and_reason_and_no_more_than_two():
    with pytest.raises(RecordInvalid, match="rerank_reason"):
        persist(_set(alternatives=(_rec("alternative", rerank_reason=""),)))
    with pytest.raises(RecordInvalid, match="> 2"):
        persist(_set(alternatives=tuple(_rec("alternative") for _ in range(3))))
    with pytest.raises(RecordInvalid, match="primary.role"):
        persist(_set(primary=_rec("alternative")))
    assert Recommendation.objects.count() == 0


def test_one_primary_per_set_is_a_schema_constraint():
    rset = persist(_set())
    second = Recommendation(
        recommendation_set=rset, role="primary", direction_code="X", family="ADDRESS", result_status="CLEAR_PRIMARY",
        readiness_state="READY", reason_codes=["r"], explanation={"displayable": False},
        safety_evaluation_ref=SAFETY, context_snapshot_ref=SNAPSHOT,
        decision_policy_version="d", taxonomy_version="t", safety_policy_version="s",
        catalog_mapping_version="c", presentation_policy_version="p",
        created_at=timezone.now(), actionable_until=timezone.now() + ACTIONABILITY_TTL,
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            second.save()


# ---------------------------------------------------------------- immutable

def test_record_cannot_be_updated_or_deleted_by_any_path():
    rset = persist(_set())
    rec = rset.primary
    rec.direction_code = "SOMETHING_ELSE"
    with pytest.raises(ImmutableRecordError):
        rec.save()
    with pytest.raises(ImmutableRecordError):
        Recommendation.objects.filter(pk=rec.pk).update(direction_code="SOMETHING_ELSE")
    with pytest.raises(ImmutableRecordError):
        Recommendation.objects.bulk_update([rec], ["direction_code"])
    with pytest.raises(ImmutableRecordError):
        rec.delete()
    with pytest.raises(ImmutableRecordError):
        Recommendation.objects.filter(pk=rec.pk).delete()
    with pytest.raises(ImmutableRecordError):
        RecommendationSet.objects.all().delete()
    with pytest.raises(ImmutableRecordError):
        RecommendationEvent.objects.all().update(kind="recommendation.engaged")
    rec.refresh_from_db()
    assert rec.direction_code == "REDUCE_MUSCLE_TENSION_BACK" and Recommendation.objects.filter(pk=rec.pk).exists()


def test_expiry_is_not_deletion():
    now = timezone.now()
    rec = persist(_set(), now=now).primary
    assert rec.is_actionable(now + timedelta(minutes=119))
    assert not rec.is_actionable(now + timedelta(hours=2))
    assert Recommendation.objects.filter(pk=rec.pk).exists()          # запись на месте (B13)
    assert not hasattr(rec, "retention_until")                          # retention — политика, не поле


def test_supersession_is_a_new_record_not_an_edit():
    old = persist(_set()).primary
    replacement = _rec(supersedes_id=old.pk, direction_code="IMPROVE_RELAXATION", family="SUPPORT")
    new = persist(_set(primary=replacement)).primary
    assert new.supersedes_id == old.pk and old.superseded_by.get().pk == new.pk
    old.refresh_from_db()
    assert old.direction_code == "REDUCE_MUSCLE_TENSION_BACK"


# ---------------------------------------------------------------- WHAT, не HOW/WHO

def test_the_record_carries_no_execution_fields():
    forbidden = {"candidate_id", "rank", "service_ref", "service", "provider_ref", "provider", "specialist",
                 "price", "price_snapshot", "availability_ref", "slot", "distance_meters", "distance_km", "score"}
    names = {f.name for f in Recommendation._meta.get_fields()}
    assert not names & forbidden, names & forbidden


def test_snapshots_are_refs_not_copies():
    rec = persist(_set(primary=_rec(
        execution_mapping_snapshot_ref={"snapshot_id": "exec-1", "snapshot_version": 1, "content_digest": "sha256:e"},
        transaction_snapshot_ref={"snapshot_id": "tx-1", "snapshot_version": 1, "content_digest": "sha256:t"},
    ))).primary
    for ref in (rec.context_snapshot_ref, rec.execution_mapping_snapshot_ref, rec.transaction_snapshot_ref):
        assert set(ref) == {"snapshot_id", "snapshot_version", "content_digest"}


# ---------------------------------------------------------------- события

def test_events_without_accepted_and_append_only():
    rec = persist(_set()).primary
    for kind in ("recommendation.presented", "recommendation.explanation_requested",
                 "recommendation.alternative_requested", "recommendation.engaged", "booking_intent.created"):
        ev = record_event(rec, kind, payload={"channel": "max"})
        assert ev.payload["recommendation_id"] == str(rec.pk) and ev.producer
    assert RecommendationEvent.objects.filter(recommendation=rec).count() == 6   # + created
    for bad in ("recommendation.accepted", "recommendation.declined", "accepted"):
        with pytest.raises(RecordInvalid, match="B8"):
            record_event(rec, bad)
    with pytest.raises(RecordInvalid, match="persist"):
        record_event(rec, "recommendation.created")
    with pytest.raises(RecordInvalid, match="неизвестное"):
        record_event(rec, "recommendation.generated")
    assert "recommendation.accepted" not in RecommendationEvent.Kind.values
    ev = RecommendationEvent.objects.filter(recommendation=rec).first()
    with pytest.raises(ImmutableRecordError):
        ev.delete()
