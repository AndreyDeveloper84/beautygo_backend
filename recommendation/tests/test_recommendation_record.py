"""Объект Recommendation (контракт v1.0 §3, Final Reconciliation §5) — immutable, с провенансом, expires ≠ delete.

Что стережётся:

* запись пишется только через ``records.persist``: набор с исходом + (при NBA)
  primary и ≤2 alternatives + событие ``created``; минимум §5 и версии политик
  обязательны (отказ по имени поля);
* **immutable**: ``save()`` существующей строки, ``QuerySet.update()``,
  ``bulk_update()``, ``delete()`` — ``ImmutableRecordError``; ничего не меняется;
* **expires ≠ delete**: через 2 ч ``is_actionable`` ложь, запись на месте;
* decision_subject = NBA/WHAT (B2): у модели **нет** полей execution-слоя
  (service/provider/price/slot/distance/rank/candidate) — список запрещённых имён;
* три снимка варианта — ссылки (B10): копия содержимого вместо ref — отказ;
* lineage: alternative без parent/rerank_reason — отказ; > 2 alternatives — отказ;
  одна primary на set (constraint);
* события: ``accepted``/``declined`` не существуют (B8); ``created`` пишет только
  persist; append-only.

DRF-1905 — исход на уровне набора (§32):

* исход, готовность, вердикт безопасности, снимок контекста и версии — у набора;
  ``SAFETY_BOUNDARY`` / ``INSUFFICIENT_CONTEXT`` записываются **без** primary и
  alternatives, NBA-исход без primary — отказ; значения проверяются, строчные — отказ;
* то же держит **база**: CHECK на строке набора (``persist`` в обход — ``IntegrityError``);
* FK ``primary`` отложен до COMMIT — иначе порядок «набор с id primary, потом primary»
  невозможен при immutable-наборе; сторож читает SQL миграции 0002;
* миграция 0002 отказывает на непустой таблице.
"""
from __future__ import annotations

import importlib
import io
import uuid
from datetime import timedelta

import pytest
from django.apps import apps as django_apps
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from recommendation.models import (
    ACTIONABILITY_TTL,
    RECORD_SCHEMA_VERSION,
    ContextSnapshot,
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
    RecordInvalid,
    persist,
    record_event,
)
from recommendation.snapshots import content_digest

pytestmark = pytest.mark.django_db

VERSIONS = PolicyVersions(
    decision_policy="dp-2026-09-12", taxonomy="tx-2026-07", safety_policy="sp-1",
    catalog_mapping="cm-2026-09-12", presentation_policy="pp-1",
)
#: Снимок без города — словарь городов каталога здесь не нужен (он — в test_context_snapshot.py).
SNAPSHOT_CONTENT = {
    "snapshot_version": "turn-context-v1",
    "decision_readiness": {"state_revision": 3, "readiness_state": "ready"},
    "said": [{"key": "visit_context", "value": "after_work", "origin": "conversation", "said_on": "2026-09-12"}],
    "answered_question": None,
}
SNAPSHOT = ContextSnapshotInput(
    snapshot_version="turn-context-v1", content_digest=content_digest(SNAPSHOT_CONTENT), content=SNAPSHOT_CONTENT,
)
SAFETY = {"state": "NORMAL", "rule_id": "r-0", "policy_version": "sp-1", "evidence_ref": "ev-0",
          "activated_at": "2026-09-12T10:00:00Z"}
EVIDENCE = [{"source": "conversation", "ref": "msg-1", "said_at": "2026-09-12T10:00:00Z"}]


def _rec(role="primary", **over) -> RecommendationInput:
    base = dict(
        role=role, direction_code="REDUCE_MUSCLE_TENSION_BACK", family="ADDRESS",
        target_outcomes=["REDUCE(MUSCLE_TENSION)"],
        reason_codes=["ELIG_CAPABILITY_VERIFIED"], evidence_refs=list(EVIDENCE),
        explanation={"displayable": True, "user_visible_reasons": ["ты сказала, что ноет спина"], "internal_only": []},
    )
    if role == "alternative":
        base["rerank_reason"] = "ALTERNATIVE_REQUESTED"
    base.update(over)
    return RecommendationInput(**base)


def _set(**over) -> RecommendationSetInput:
    base = dict(
        subject_ref="user:pseudo-1", intent_id="intent-1", versions=VERSIONS,
        result_status="CLEAR_PRIMARY", readiness_state="READY",
        reason_codes=["CLEAR_PRIMARY_BY_POLICY"], evidence_refs=list(EVIDENCE),
        explanation={"displayable": True, "user_visible_reasons": ["ты сказала, что ноет спина"], "internal_only": []},
        safety_evaluation_ref=SAFETY, context_snapshot=SNAPSHOT, primary=_rec(),
    )
    base.update(over)
    return RecommendationSetInput(**base)


def _boundary(**over) -> RecommendationSetInput:
    """Сегодняшний честный исход без NBA, который мозг пишет в тени (6.4): SAFETY_BOUNDARY."""
    base = dict(
        result_status="SAFETY_BOUNDARY", readiness_state="BLOCKED", primary=None,
        reason_codes=["SAFETY_STOP"],
        explanation={"displayable": False, "user_visible_reasons": [], "internal_only": ["SAFETY_STOP"]},
        safety_evaluation_ref={**SAFETY, "state": "STOP"},
    )
    base.update(over)
    return _set(**base)


def _counts() -> tuple[int, int, int]:
    return RecommendationSet.objects.count(), Recommendation.objects.count(), RecommendationEvent.objects.count()


# ---------------------------------------------------------------- запись

def test_persist_writes_set_primary_alternatives_and_created_events():
    now = timezone.now()
    alt_in = _rec("alternative", direction_code="IMPROVE_RELAXATION", family="SUPPORT")
    rset = persist(_set(alternatives=(alt_in,)), now=now)
    assert _counts() == (1, 2, 2)
    primary = rset.primary
    assert primary.role == "primary" and primary.parent is None and primary.rerank_reason == ""
    assert primary.recommendation_set_id == rset.pk
    alt = rset.recommendations.get(role="alternative")
    assert alt.parent_id == primary.pk and alt.rerank_reason == "ALTERNATIVE_REQUESTED"
    assert primary.actionable_until == now + ACTIONABILITY_TTL == now + timedelta(hours=2)
    # провенанс и исход — у набора; схема 1.1 — у обоих
    assert rset.result_status == "CLEAR_PRIMARY" and rset.readiness_state == "READY"
    assert rset.decision_policy_version == "dp-2026-09-12"
    assert rset.context_snapshot.content_digest == SNAPSHOT.content_digest == content_digest(SNAPSHOT_CONTENT)
    assert rset.record_schema_version == primary.record_schema_version == RECORD_SCHEMA_VERSION == "1.2"
    # WHY варианта остаётся на варианте (C04.2)
    assert alt.reason_codes == ["ELIG_CAPABILITY_VERIFIED"] and alt.explanation["displayable"] is True
    assert list(RecommendationEvent.objects.values_list("kind", flat=True)) == ["recommendation.created"] * 2


def test_set_is_shadow_by_default_and_live_only_when_declared_with_conversation_ref():
    """C1: теневые записи отличимы от живых; LIVE — только явно."""
    shadow = persist(_set())
    assert shadow.execution_mode == "SHADOW" and shadow.conversation_ref == {}
    live = persist(_set(execution_mode="LIVE", conversation_ref={"conversation_id": "c-1", "trace_id": "t-1"}))
    assert live.execution_mode == "LIVE" and live.conversation_ref["trace_id"] == "t-1"
    with pytest.raises(RecordInvalid, match="execution_mode"):
        persist(_set(execution_mode="REAL"))
    with pytest.raises(RecordInvalid, match="conversation_ref"):
        persist(_set(conversation_ref={"conversation_id": "c-1"}))


def test_no_action_is_a_valid_recorded_result():
    """NO_ACTION — до размещения в таксономии (OQ-R11) — записывается как NBA, с primary."""
    rset = persist(_set(result_status="NO_ACTION", readiness_state="INSUFFICIENT_EVIDENCE",
                        primary=_rec(family="OBSERVE")))
    assert rset.result_status == "NO_ACTION" and rset.primary.family == "OBSERVE"


@pytest.mark.parametrize("bad, match", [
    ({"direction_code": ""}, "direction_code"),
    ({"family": "INTERVENE"}, "family"),
    ({"reason_codes": []}, "primary: reason_codes"),
    ({"explanation": {"user_visible_reasons": []}}, "primary: explanation.displayable"),
    ({"memory_snapshot_ref": {"entries": ["copied value"]}}, "memory_snapshot_ref"),
    # DRF-1909: форма ссылки закрыта — лишний ключ рядом с тремя ключами ссылки отказывает по имени
    ({"memory_snapshot_ref": {"snapshot_id": "m-1", "snapshot_version": 1, "content_digest": "sha256:m",
                              "subject_ref": "u-1"}}, "memory_snapshot_ref"),
    ({"execution_mapping_snapshot_ref": {"snapshot_id": "e-1", "snapshot_version": 1, "content_digest": "sha256:e",
                                         "client_ref": "c-1"}}, "execution_mapping_snapshot_ref"),
    ({"transaction_snapshot_ref": {"snapshot_id": "t-1", "snapshot_version": 1, "content_digest": "sha256:t",
                                   "booking_owner": "c-1"}}, "transaction_snapshot_ref"),
    # ссылка — объект, а не перечень имён её ключей: set(список) совпал бы с ключами ссылки
    ({"transaction_snapshot_ref": ["snapshot_id", "snapshot_version", "content_digest"]}, "transaction_snapshot_ref"),
    ({"evidence_refs": [{"kind": "user_stated"}]}, r"primary: evidence_refs\[0\]"),
    # DRF-1921: элемент evidence закрыт — незнакомый источник, лишний ключ, признак не-bool
    ({"evidence_refs": [{"source": "brand_new_source", "ref": "x"}]}, r"primary: evidence_refs\[0\]\.source"),
    ({"evidence_refs": [{"source": "conversation", "ref": "x", "text": "ноет спина"}]},
     r"primary: evidence_refs\[0\] — лишние ключи"),
    ({"evidence_refs": [{"source": "journey", "ref": "x", "user_confirmed": "yes"}]},
     r"primary: evidence_refs\[0\]\.user_confirmed"),
])
def test_incomplete_variant_is_refused_by_field_name(bad, match):
    with pytest.raises(RecordInvalid, match=match):
        persist(_set(primary=_rec(**bad)))
    assert _counts() == (0, 0, 0)


@pytest.mark.parametrize("bad, match", [
    ({"safety_evaluation_ref": {"state": "NORMAL"}}, "safety_evaluation_ref"),
    ({"safety_evaluation_ref": {**SAFETY, "state": "normal"}}, "safety_evaluation_ref.state"),
    ({"context_snapshot": {"facts": {"age": 30}}}, "набор: context_snapshot"),
    ({"result_status": "ACCEPTED"}, "result_status"),
    ({"readiness_state": "MAYBE"}, "readiness_state"),
    ({"readiness_state": "blocked"}, "readiness_state"),       # движок бота отдаёт строчные — каталог не нормализует
    ({"reason_codes": []}, "набор: reason_codes"),
    ({"explanation": {"user_visible_reasons": []}}, "набор: explanation.displayable"),
    ({"evidence_refs": [{"source": "", "ref": "x"}]}, r"набор: evidence_refs\[0\]"),
])
def test_incomplete_set_is_refused_by_field_name(bad, match):
    with pytest.raises(RecordInvalid, match=match):
        persist(_set(**bad))
    assert _counts() == (0, 0, 0)


def test_missing_or_too_long_policy_version_is_refused_by_name():
    versions = PolicyVersions(
        decision_policy="dp", taxonomy="", safety_policy="sp", catalog_mapping="", presentation_policy="pp",
    )
    with pytest.raises(RecordInvalid, match="taxonomy, catalog_mapping"):
        persist(_set(versions=versions))
    too_long = PolicyVersions(
        decision_policy="none:" + "x" * 28, taxonomy="none:OQ-R11-open", safety_policy="sp",
        catalog_mapping="none", presentation_policy="none",
    )
    with pytest.raises(RecordInvalid, match="длиннее 32 знаков: decision_policy"):
        persist(_set(versions=too_long))
    ok = PolicyVersions(
        decision_policy="dp-v0-shadow", taxonomy="none:OQ-R11-open", safety_policy="sp",
        catalog_mapping="none", presentation_policy="none",
    )
    assert persist(_set(versions=ok)).taxonomy_version == "none:OQ-R11-open"


def test_alternative_needs_parent_and_reason_and_no_more_than_two():
    with pytest.raises(RecordInvalid, match="rerank_reason"):
        persist(_set(alternatives=(_rec("alternative", rerank_reason=""),)))
    with pytest.raises(RecordInvalid, match="> 2"):
        persist(_set(alternatives=tuple(_rec("alternative") for _ in range(3))))
    with pytest.raises(RecordInvalid, match="role должен быть primary"):
        persist(_set(primary=_rec("alternative")))
    assert _counts() == (0, 0, 0)


def test_one_primary_per_set_is_a_schema_constraint():
    rset = persist(_set())
    second = Recommendation(
        recommendation_set=rset, role="primary", direction_code="X", family="ADDRESS",
        reason_codes=["r"], explanation={"displayable": False},
        created_at=timezone.now(), actionable_until=timezone.now() + ACTIONABILITY_TTL,
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            second.save()


# ---------------------------------------------------------------- исход без NBA (DRF-1905, §32)

def test_safety_boundary_is_recorded_without_any_nba():
    rset = persist(_boundary())
    assert rset.result_status == "SAFETY_BOUNDARY" and rset.primary_id is None and rset.primary is None
    assert rset.reason_codes == ["SAFETY_STOP"] and rset.safety_evaluation_ref["state"] == "STOP"
    assert _counts() == (1, 0, 0)     # записей варианта и событий нет — их не бывает без NBA


def test_insufficient_context_is_an_allowed_value_without_nba():
    """Пока не производится (после OQ-R11), но пара «статус ⇔ primary IS NULL» верна и для него."""
    rset = persist(_boundary(result_status="INSUFFICIENT_CONTEXT", readiness_state="NEEDS_REQUIRED_CONTEXT"))
    assert rset.primary_id is None and _counts() == (1, 0, 0)


@pytest.mark.parametrize("over, match", [
    ({"primary": _rec()}, "не является NBA"),
    ({"alternatives": (_rec("alternative"),)}, "не является NBA"),
])
def test_no_nba_outcome_with_variants_is_refused_by_name(over, match):
    with pytest.raises(RecordInvalid, match=match):
        persist(_boundary(**over))
    assert _counts() == (0, 0, 0)


@pytest.mark.parametrize("status", ["CLEAR_PRIMARY", "MULTIPLE_SUITABLE", "NO_ACTION"])
def test_nba_outcome_without_primary_is_refused_by_name(status):
    with pytest.raises(RecordInvalid, match="NBA-исход без primary"):
        persist(_set(result_status=status, primary=None))
    assert _counts() == (0, 0, 0)


def _raw_set(**over) -> RecommendationSet:
    """Набор мимо persist — чтобы проверить, что условие держит БАЗА, а не только код."""
    snapshot = ContextSnapshot.objects.create(
        subject_ref="user:raw", snapshot_version=SNAPSHOT.snapshot_version, content_digest=SNAPSHOT.content_digest,
        content=SNAPSHOT_CONTENT, created_at=timezone.now(),
    )
    fields = dict(
        subject_ref="user:raw", intent_id="i", result_status="CLEAR_PRIMARY", readiness_state="READY",
        reason_codes=["r"], explanation={"displayable": False}, safety_evaluation_ref=SAFETY,
        context_snapshot=snapshot, decision_policy_version="d", taxonomy_version="t",
        safety_policy_version="s", catalog_mapping_version="c", presentation_policy_version="p",
        created_at=timezone.now(),
    )
    fields.update(over)
    return RecommendationSet(**fields)


def test_database_check_ties_outcome_to_primary_in_both_directions():
    with pytest.raises(IntegrityError):                          # NBA-исход без primary
        with transaction.atomic():
            _raw_set(result_status="CLEAR_PRIMARY", primary_id=None).save()
    existing = persist(_set()).primary
    with pytest.raises(IntegrityError):                          # исход без NBA — но с primary
        with transaction.atomic():
            _raw_set(result_status="SAFETY_BOUNDARY", primary_id=existing.pk).save()
    with transaction.atomic():                                   # положительная стража: верная пара проходит
        _raw_set(result_status="SAFETY_BOUNDARY", primary_id=None).save()


def test_primary_fk_is_checked_at_commit_not_at_insert():
    """Порядок persist («набор с id primary, потом primary») законен только при отложенном FK.

    Тест-транзакция не коммитится, поэтому проверку на COMMIT вызываем явно:
    ``check_constraints`` делает ``SET CONSTRAINTS ALL IMMEDIATE``.
    """
    persist(_set())
    connection.check_constraints()                               # целая запись — ограничения сходятся
    with transaction.atomic():
        _raw_set(primary_id=uuid.uuid4()).save()                 # висячий primary — вставка проходит (отложено)
        with pytest.raises(IntegrityError):
            connection.check_constraints()
        transaction.set_rollback(True)


def test_migration_0002_creates_the_primary_fk_deferrable():
    """Сторож на форму схемы: будущая миграция, сделавшая FK немедленным, молча сломала бы persist."""
    out = io.StringIO()
    call_command("sqlmigrate", "recommendation", "0002", stdout=out)
    # Для FK, добавленного к существующей таблице, Django пишет ограничение в строке
    # ADD COLUMN: `"primary_id" uuid NULL CONSTRAINT … REFERENCES … DEFERRABLE INITIALLY DEFERRED`
    # — без слов FOREIGN KEY. Хвост той же строки `SET CONSTRAINTS … IMMEDIATE` действует
    # только внутри транзакции миграции, не в определении ограничения.
    fk_lines = [line for line in out.getvalue().splitlines() if '"primary_id"' in line and "REFERENCES" in line]
    assert fk_lines, "в SQL миграции 0002 нет FK primary_id — сторож смотрит не туда"
    assert all("DEFERRABLE INITIALLY DEFERRED" in line for line in fk_lines), fk_lines


def test_migration_0002_refuses_a_non_empty_table():
    migration = importlib.import_module("recommendation.migrations.0002_set_level_outcome")
    migration.refuse_if_records_exist(django_apps, None)         # пустая таблица — проходит
    persist(_boundary())
    with pytest.raises(RuntimeError, match="безопасна только на пустой"):
        migration.refuse_if_records_exist(django_apps, None)


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
        RecommendationSet.objects.filter(pk=rset.pk).update(result_status="SAFETY_BOUNDARY")
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
    names = {f.name for f in Recommendation._meta.get_fields()} | {f.name for f in RecommendationSet._meta.get_fields()}
    assert not names & forbidden, names & forbidden


def test_snapshots_are_refs_not_copies():
    rset = persist(_set(primary=_rec(
        execution_mapping_snapshot_ref={"snapshot_id": "exec-1", "snapshot_version": 1, "content_digest": "sha256:e"},
        transaction_snapshot_ref={"snapshot_id": "tx-1", "snapshot_version": 1, "content_digest": "sha256:t"},
    )))
    rec = rset.primary
    for ref in (rset.context_snapshot.as_ref(), rec.execution_mapping_snapshot_ref, rec.transaction_snapshot_ref):
        assert set(ref) == {"snapshot_id", "snapshot_version", "content_digest"}


def test_decision_level_fields_live_only_on_the_set():
    """DRF-1905: одно на проход — в одном месте; на варианте их нет, чтобы не разойтись."""
    moved = {"result_status", "readiness_state", "safety_evaluation_ref", "context_snapshot",
             "decision_policy_version", "taxonomy_version", "safety_policy_version",
             "catalog_mapping_version", "presentation_policy_version"}
    record_fields = {f.name for f in Recommendation._meta.get_fields()}
    set_fields = {f.name for f in RecommendationSet._meta.get_fields()}
    assert not record_fields & moved, record_fields & moved
    assert moved <= set_fields, moved - set_fields
    # WHY — на обоих уровнях (вариант А главного окна)
    assert {"reason_codes", "evidence_refs", "explanation"} <= record_fields & set_fields


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
