"""Снимок контекста решения (DRF-1906, часть 1) — хранит каталог, пишет persist вместе с набором.

Что стережётся:

* **digest** — формула, сверенная с ботом побайтно: эталон (317 байт, sha256)
  первым тестом; порядок ключей на digest не влияет; содержимое после jsonb
  даёт тот же digest;
* снимок и набор с FK на него — одна запись; несовпавший digest — отказ по
  имени и ноль строк;
* **схема закрыта** по версии — лишний или недостающий ключ на любом уровне,
  неизвестная версия, чужой тип — отказ по имени;
* **said[].value — из закрытого словаря своего ключа**: city — города каталога
  (``Tenant.city``), visit_context — коды сборщика; регистр не приводится;
  длина и число слов — вторая стена, держит и значение словаря; отказ не
  повторяет значение;
* **класс здоровья** в ``said[].key`` и ``answered_question.question_id`` — по
  префиксу любого сегмента, без учёта регистра;
* **неизменяемость, кроме стирания**: save/update/bulk_update/delete отказывают;
  ``erase_for_subject`` пустит ``content`` и поставит ``erased_at``, id/версия/
  digest на месте, чужой субъект не тронут, повтор не сдвигает момент; стёртый
  снимок с содержимым не пропускает база (CHECK);
* ``explanation.internal_only`` — только коды формы reason_codes (набор и вариант);
  все члены ``ReasonCode`` этой формы;
* миграция 0003 отказывает на непустой таблице; JSON-ссылки на наборе больше нет.
"""
from __future__ import annotations

import copy
import importlib
import json
from datetime import timedelta

import pytest
from django.apps import apps as django_apps
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from recommendation._reason_codes import ReasonCode
from recommendation.models import ContextSnapshot, ImmutableRecordError, Recommendation, RecommendationSet
from recommendation.records import (
    CODE_FORM,
    ContextSnapshotInput,
    PolicyVersions,
    RecommendationInput,
    RecommendationSetInput,
    RecordInvalid,
    persist,
)
from recommendation.snapshots import SnapshotInvalid, check_content, content_digest, health_prefix, known_cities
from tenants.models import Tenant

pytestmark = pytest.mark.django_db

#: Эталон, сверенный с ботом 15.09: ``apps.orchestrator.context_snapshot.content_digest``
#: на этом содержимом даёт ту же каноническую строку (317 байт) и тот же hexdigest.
#: Эталон проверяет формулу, не регистр: ``"BLOCKED"`` здесь — из переписки, не из сборщика.
REFERENCE_CONTENT = {
    "snapshot_version": "turn-context-v1",
    "decision_readiness": {"state_revision": 7, "readiness_state": "BLOCKED"},
    "said": [
        {"key": "city", "value": "Пенза", "origin": "conversation", "said_on": "2026-09-14"},
        {"key": "visit_context", "value": "after_work", "origin": "conversation", "said_on": "2026-09-14"},
    ],
    "answered_question": None,
}
REFERENCE_CANONICAL_BYTES = 317
REFERENCE_DIGEST = "06f80f9f91e34c46a63917cc5a75ace0ddf440c57bec38c23ef84b48253659ff"  # pragma: allowlist secret

VERSIONS = PolicyVersions("dp-1", "tx-1", "sp-1", "cm-1", "pp-1")
SAFETY = {"state": "STOP", "rule_id": "r-stop", "policy_version": "sp-1", "evidence_ref": "ev-1",
          "activated_at": "2026-09-15T10:00:00Z"}


def _content(**over) -> dict:
    """Содержимое, как его пишет сборщик turn-context-v1: readiness_state строчный."""
    base = copy.deepcopy(REFERENCE_CONTENT)
    base["decision_readiness"]["readiness_state"] = "blocked"
    base.update(over)
    return base


def _said(key: str, value, *, origin="conversation", said_on="2026-09-14") -> dict:
    return {"key": key, "value": value, "origin": origin, "said_on": said_on}


def _snap(content: dict | None = None, *, digest: str | None = None,
          version: str = "turn-context-v1") -> ContextSnapshotInput:
    content = _content() if content is None else content
    return ContextSnapshotInput(
        snapshot_version=version, content_digest=content_digest(content) if digest is None else digest,
        content=content,
    )


def _rec(**over) -> RecommendationInput:
    base = dict(
        role="primary", direction_code="REDUCE_MUSCLE_TENSION_BACK", family="ADDRESS",
        target_outcomes=["REDUCE(MUSCLE_TENSION)"], reason_codes=["ELIG_CAPABILITY_VERIFIED"],
        evidence_refs=[{"source": "conversation", "ref": "msg-1"}],
        explanation={"displayable": False, "user_visible_reasons": [], "internal_only": []},
    )
    base.update(over)
    return RecommendationInput(**base)


def _set(**over) -> RecommendationSetInput:
    """Сегодняшний исход мозга без NBA — SAFETY_BOUNDARY."""
    base = dict(
        subject_ref="user:snap-1", intent_id="intent-1", versions=VERSIONS,
        result_status="SAFETY_BOUNDARY", readiness_state="BLOCKED", reason_codes=["SAFETY_STOP"],
        evidence_refs=[],
        explanation={"displayable": False, "user_visible_reasons": [], "internal_only": ["SAFETY_STOP"]},
        safety_evaluation_ref=SAFETY, context_snapshot=_snap(), primary=None,
    )
    base.update(over)
    return RecommendationSetInput(**base)


def _counts() -> tuple[int, int, int]:
    return ContextSnapshot.objects.count(), RecommendationSet.objects.count(), Recommendation.objects.count()


@pytest.fixture
def penza(db):
    return Tenant.all_objects.create(slug="snap-penza", name="Салон снимка", city="Пенза")


# ---------------------------------------------------------------- digest

def test_digest_matches_the_reference_vector_agreed_with_the_bot():
    canonical = json.dumps(REFERENCE_CONTENT, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert len(canonical.encode("utf-8")) == REFERENCE_CANONICAL_BYTES
    assert content_digest(REFERENCE_CONTENT) == REFERENCE_DIGEST


def test_digest_does_not_depend_on_key_order():
    reordered = {k: REFERENCE_CONTENT[k] for k in reversed(list(REFERENCE_CONTENT))}
    reordered["said"] = [dict(reversed(list(item.items()))) for item in REFERENCE_CONTENT["said"]]
    assert list(reordered) != list(REFERENCE_CONTENT)
    assert content_digest(reordered) == REFERENCE_DIGEST


# ---------------------------------------------------------------- запись

def test_snapshot_is_written_with_the_set_and_keeps_its_digest(penza):
    content = _content()
    rset = persist(_set(context_snapshot=_snap(content)))
    assert _counts() == (1, 1, 0)
    snap = ContextSnapshot.objects.get()
    assert rset.context_snapshot_id == snap.pk and snap.subject_ref == rset.subject_ref
    assert snap.snapshot_version == "turn-context-v1" and snap.erased_at is None
    assert snap.as_ref() == {"snapshot_id": str(snap.pk), "snapshot_version": "turn-context-v1",
                             "content_digest": content_digest(content)}
    snap.refresh_from_db()
    # jsonb переставляет ключи — digest канонического JSON от этого не меняется
    assert snap.content == content and content_digest(snap.content) == snap.content_digest


def test_snapshot_and_set_are_one_transaction(penza, monkeypatch):
    """Сбой записи набора после снимка не оставляет снимка-сироты."""
    def fail(self, *args, **kwargs):
        raise RuntimeError("набор не записался")

    monkeypatch.setattr(RecommendationSet, "save", fail)
    with pytest.raises(RuntimeError, match="набор не записался"):
        persist(_set())
    assert ContextSnapshot.objects.count() == 0


@pytest.mark.parametrize("digest", ["0" * 64, "sha256:" + "0" * 57, content_digest(REFERENCE_CONTENT).upper()])
def test_wrong_digest_is_refused_by_name_and_nothing_is_written(penza, digest):
    with pytest.raises(RecordInvalid, match="context_snapshot.content_digest"):
        persist(_set(context_snapshot=_snap(digest=digest)))
    assert _counts() == (0, 0, 0)


def test_unknown_snapshot_version_is_refused_by_name(penza):
    content = _content(snapshot_version="turn-context-v2")
    with pytest.raises(RecordInvalid, match="snapshot_version 'turn-context-v2' неизвестна"):
        persist(_set(context_snapshot=_snap(content, version="turn-context-v2")))
    assert _counts() == (0, 0, 0)


# ---------------------------------------------------------------- схема закрыта

def _drop(key):
    def mutate(c):
        del c[key]
    return mutate


@pytest.mark.parametrize("mutate, match", [
    (lambda c: c.update(raw_text="x"), r"content: лишние ключи \['raw_text'\]"),
    (_drop("said"), r"content: нет ключей \['said'\]"),
    (lambda c: c.update(snapshot_version="turn-context-v0"), "content.snapshot_version: не совпадает"),
    (lambda c: c["decision_readiness"].update(safety="STOP"), "content.decision_readiness: лишние ключи"),
    (lambda c: c["decision_readiness"].update(state_revision="7"), "state_revision"),
    (lambda c: c["decision_readiness"].update(state_revision=True), "state_revision"),
    (lambda c: c["decision_readiness"].update(readiness_state="BLOCKED"), "readiness_state"),
    (lambda c: c["said"][0].update(text="ноет спина"), r"content.said\[0\]: лишние ключи"),
    (lambda c: c["said"][1].update(origin="anketa"), r"content.said\[1\].origin"),
    (lambda c: c["said"][0].update(said_on="2026-09-14T10:00:00"), r"content.said\[0\].said_on"),
    (lambda c: c["said"][0].update(said_on="2026-02-30"), r"content.said\[0\].said_on"),
    (lambda c: c.update(said={"city": "Пенза"}), "content.said: ожидается список"),
    (lambda c: c.update(answered_question={"question_id": "said.city", "answer_text": "да"}),
     "content.answered_question: лишние ключи"),
    (lambda c: c.update(answered_question={"question_id": "где удобно"}), "answered_question.question_id"),
])
def test_closed_schema_refuses_by_field_name(penza, mutate, match):
    content = _content()
    mutate(content)
    with pytest.raises(SnapshotInvalid, match=match):
        check_content(content, snapshot_version="turn-context-v1")


def test_valid_shapes_pass(penza):
    """Положительная стража: без неё «отказывает на всё» неотличимо от «отказывает по делу»."""
    check_content(_content(), snapshot_version="turn-context-v1")
    check_content(_content(said=[], answered_question={"question_id": "said.city"},
                           decision_readiness={"state_revision": None, "readiness_state": None}),
                  snapshot_version="turn-context-v1")
    for code in ("after_work", "weekend", "evening"):
        check_content(_content(said=[_said("visit_context", code, said_on=None)]), snapshot_version="turn-context-v1")


# ---------------------------------------------------------------- said[].value по ключу

@pytest.mark.parametrize("key, value, match", [
    ("age", "30", "неизвестный ключ 'age'"),
    ("city", "Москва", "вне закрытого словаря ключа 'city'"),
    ("city", "back_pain", "вне закрытого словаря ключа 'city'"),
    ("city", "пенза", "вне закрытого словаря ключа 'city'"),                 # регистр не приводится
    ("visit_context", "morning", "вне закрытого словаря ключа 'visit_context'"),
    ("visit_context", "pregnant", "вне закрытого словаря ключа 'visit_context'"),
    ("visit_context", "Пенза", "вне закрытого словаря ключа 'visit_context'"),   # словарь — свой у ключа
    ("city", "болит спина после работы", "длиннее"),
    ("visit_context", "x" * 49, "длиннее"),
    ("city", "", "непустая строка"),
])
def test_said_value_is_checked_against_the_dictionary_of_its_key(penza, key, value, match):
    with pytest.raises(SnapshotInvalid, match=match) as exc:
        check_content(_content(said=[_said(key, value)]), snapshot_version="turn-context-v1")
    if value:
        assert value not in str(exc.value), "отказ повторил отвергнутое значение"


def test_length_wall_holds_even_for_a_dictionary_value(db):
    """Вторая стена независима от словаря: город каталога из трёх слов всё равно не проходит."""
    Tenant.all_objects.create(slug="snap-three", name="Салон", city="Город Трёх Слов")
    with pytest.raises(SnapshotInvalid, match="длиннее"):
        check_content(_content(said=[_said("city", "Город Трёх Слов")]), snapshot_version="turn-context-v1")


def test_city_dictionary_is_the_catalog_city_list(db):
    content = _content(said=[_said("city", "Пенза")])
    with pytest.raises(SnapshotInvalid, match="вне закрытого словаря ключа 'city'"):
        check_content(content, snapshot_version="turn-context-v1")           # городов в каталоге нет
    Tenant.all_objects.create(slug="snap-blank", name="Без города", city="")
    assert "" not in known_cities()                                           # пустой город — «не указан», не значение
    Tenant.all_objects.create(slug="snap-penza-2", name="Салон", city="Пенза", is_active=False)
    assert known_cities() == frozenset({"Пенза"})
    check_content(content, snapshot_version="turn-context-v1")               # неактивный салон — всё ещё город каталога


# ---------------------------------------------------------------- класс здоровья

HEALTH_CODES = ["health_screening.soft", "screening_intro", "safety_check", "wellness_goal",
                "said.health_note", "Health_Status", "open:screening-2"]


@pytest.mark.parametrize("code", HEALTH_CODES)
def test_health_class_question_is_refused_by_name(penza, code):
    with pytest.raises(SnapshotInvalid, match="answered_question.question_id: класс здоровья"):
        check_content(_content(answered_question={"question_id": code}), snapshot_version="turn-context-v1")


@pytest.mark.parametrize("code", HEALTH_CODES)
def test_health_class_said_key_is_refused_by_name_before_unknown_key(penza, code):
    with pytest.raises(SnapshotInvalid, match=r"said\[0\].key: класс здоровья"):
        check_content(_content(said=[_said(code, "x")]), snapshot_version="turn-context-v1")


def test_health_prefix_does_not_fire_on_ordinary_codes():
    for code in ("said.city", "said.visit_context", "open:budget", "city"):
        assert health_prefix(code) is None, code


# ---------------------------------------------------------------- неизменяемость и стирание

def test_snapshot_cannot_be_changed_by_any_path_but_erasure(penza):
    persist(_set())
    snap = ContextSnapshot.objects.get()
    snap.content = {}
    with pytest.raises(ImmutableRecordError):
        snap.save()
    with pytest.raises(ImmutableRecordError):
        ContextSnapshot.objects.filter(pk=snap.pk).update(content={})
    with pytest.raises(ImmutableRecordError):
        ContextSnapshot.objects.bulk_update([snap], ["content"])
    with pytest.raises(ImmutableRecordError):
        snap.delete()
    with pytest.raises(ImmutableRecordError):
        ContextSnapshot.objects.all().delete()
    snap.refresh_from_db()
    assert snap.content == _content() and snap.erased_at is None


def test_erasure_empties_content_and_keeps_id_version_and_digest(penza):
    mine = persist(_set(subject_ref="user:erase-me"))
    other = persist(_set(subject_ref="user:keep-me"))
    before = ContextSnapshot.objects.get(pk=mine.context_snapshot_id)
    now = timezone.now()

    assert ContextSnapshot.objects.erase_for_subject("user:erase-me", now) == 1
    erased = ContextSnapshot.objects.get(pk=mine.context_snapshot_id)
    assert erased.content == {} and erased.erased_at == now
    assert (erased.snapshot_version, erased.content_digest) == (before.snapshot_version, before.content_digest)
    assert RecommendationSet.objects.get(pk=mine.pk).context_snapshot_id == erased.pk     # ссылка набора цела
    kept = ContextSnapshot.objects.get(pk=other.context_snapshot_id)
    assert kept.content == _content() and kept.erased_at is None

    assert ContextSnapshot.objects.erase_for_subject("user:erase-me", now + timedelta(hours=1)) == 0
    assert ContextSnapshot.objects.get(pk=erased.pk).erased_at == now                    # повтор момент не сдвигает


def test_database_refuses_an_erased_snapshot_that_still_has_content(penza):
    persist(_set())
    qs = ContextSnapshot.objects.all()
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            models.QuerySet.update(qs, erased_at=timezone.now())                          # мимо менеджера
    with transaction.atomic():                                                            # положительная стража
        models.QuerySet.update(qs, erased_at=timezone.now(), content={})


# ---------------------------------------------------------------- internal_only — только коды

@pytest.mark.parametrize("value", [
    "INTERNAL-ONLY-SIGNAL rating=4.8", "provider_score=0.91", "ноет спина", "SAFETY STOP", "safety_stop", "",
    "S" * 65,
])
def test_internal_only_text_is_refused_on_the_set_and_on_the_variant(penza, value):
    with pytest.raises(RecordInvalid, match=r"набор: explanation.internal_only\[1\]"):
        persist(_set(explanation={"displayable": False, "user_visible_reasons": [],
                                  "internal_only": ["SAFETY_STOP", value]}))
    variant = _rec(explanation={"displayable": False, "user_visible_reasons": [], "internal_only": [value]})
    with pytest.raises(RecordInvalid, match=r"primary: explanation.internal_only\[0\]"):
        persist(_set(result_status="CLEAR_PRIMARY", readiness_state="READY", primary=variant))
    assert _counts() == (0, 0, 0)


def test_internal_only_must_be_a_list_and_codes_pass(penza):
    with pytest.raises(RecordInvalid, match="explanation.internal_only — список"):
        persist(_set(explanation={"displayable": False, "internal_only": "SAFETY_STOP"}))
    rset = persist(_set(explanation={"displayable": False, "user_visible_reasons": [],
                                     "internal_only": ["SAFETY_STOP", "ELIG_EXCLUDED_SAFETY"]}))
    assert rset.explanation["internal_only"] == ["SAFETY_STOP", "ELIG_EXCLUDED_SAFETY"]


def test_every_reason_code_has_the_internal_only_form():
    """«Форма reason_codes» — не описание, а проверка: реестр целиком проходит CODE_FORM."""
    codes = [c.value for c in ReasonCode]
    assert len(codes) >= 20, f"в реестре {len(codes)} кодов — перебор не того перечисления"
    assert [c for c in codes if not CODE_FORM.match(c)] == []


# ---------------------------------------------------------------- схема хранения

def test_the_set_carries_a_snapshot_fk_and_no_json_ref():
    names = {f.name for f in RecommendationSet._meta.get_fields()}
    assert "context_snapshot_ref" not in names
    fk = RecommendationSet._meta.get_field("context_snapshot")
    assert fk.related_model is ContextSnapshot and fk.null is False


def test_migration_0003_refuses_a_non_empty_table(penza):
    migration = importlib.import_module("recommendation.migrations.0003_context_snapshot")
    migration.refuse_if_records_exist(django_apps, None)         # пустая таблица — проходит
    persist(_set())
    with pytest.raises(RuntimeError, match="безопасна только на пустой"):
        migration.refuse_if_records_exist(django_apps, None)
