"""Чтение записи Recommendation для бота и события — под субъектом (DRF-1666 / DRF-1617).

Что стережётся:

* набор читается только своим субъектом: чужой субъект → 404; субъект в URL
  ≠ актор, без заголовка, чужой bearer → 403 (как на всей поверхности DRF-1617);
* ``actionable`` считается на момент ответа: до 2 ч ``true``, после — ``false``,
  запись на месте;
* ``why`` — только ``user_visible_reasons`` и только при ``displayable``;
  **internal-only текст не встречается нигде в теле ответа** (сторож по
  сырому JSON, не по одному полю) — ни у варианта, ни у исхода набора;
* primary + alternatives с ``parent`` / ``rerank_reason`` (код), ``execution_mode``;
* услуги/мастера/цены/слота в ответе нет;
* события: четыре канальных → 201 и append; ``accepted``/``declined`` → 400
  ``EVENT_NOT_IN_TAXONOMY`` (B8); ``created`` и ``booking_intent.created`` этой
  ручкой не пишутся; чужой субъект → 404.

DRF-1905: исход, готовность и вердикт безопасности — в ``outcome`` набора;
набор без NBA (``SAFETY_BOUNDARY``) читается с ``primary = null`` и пустыми
``alternatives``; правило показа ``why`` / ``evidence_refs`` (DRF-1889) —
одно на обоих уровнях.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from recommendation.models import RecommendationEvent
from recommendation.records import (
    ContextSnapshotInput,
    PolicyVersions,
    RecommendationInput,
    RecommendationSetInput,
    persist,
)
from recommendation.snapshots import content_digest
from users.models import User
from users.tests.conftest import name_subject

pytestmark = pytest.mark.django_db

#: internal_only — только коды (DRF-1906): метки уникальны, чтобы поиск по сырому телу не зеленел по совпадению.
INTERNAL_ONLY = "INTERNAL_ONLY_SIGNAL_PROVIDER_SCORE_7F3"
SET_INTERNAL_ONLY = "SET_INTERNAL_ONLY_POLICY_THRESHOLD_7F3"
VERSIONS = PolicyVersions("dp", "tx", "sp", "cm", "pp")
SAFETY = {"state": "NORMAL", "rule_id": "r", "policy_version": "sp", "evidence_ref": "e",
          "activated_at": "2026-09-12T10:00:00Z"}
SNAPSHOT_CONTENT = {
    "snapshot_version": "turn-context-v1",
    "decision_readiness": {"state_revision": 1, "readiness_state": "ready"},
    "said": [{"key": "visit_context", "value": "weekend", "origin": "conversation", "said_on": "2026-09-12"}],
    "answered_question": {"question_id": "said.visit_context"},
}
SNAPSHOT = ContextSnapshotInput("turn-context-v1", content_digest(SNAPSHOT_CONTENT), SNAPSHOT_CONTENT)


@pytest.fixture
def bearer(settings):
    settings.AYLA_INTERNAL_API_TOKEN = "test-bearer-1666"
    return "test-bearer-1666"


@pytest.fixture
def subject(db):
    return User.objects.create_user(username="rec-subject", password="x", role="client", phone="+79992221666")


@pytest.fixture
def stranger(db):
    return User.objects.create_user(username="rec-stranger", password="x", role="client", phone="+79992221667")


def _api(bearer, user) -> APIClient:
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}", HTTP_X_EXTERNAL_USER_ID=name_subject(user))
    return c


def _rec(role="primary", **over) -> RecommendationInput:
    base = dict(
        role=role, direction_code="REDUCE_MUSCLE_TENSION_BACK", family="ADDRESS",
        target_outcomes=["REDUCE(MUSCLE_TENSION)"],
        reason_codes=["ELIG_CAPABILITY_VERIFIED"],
        evidence_refs=[{"source": "conversation", "ref": "msg-1"}],
        explanation={"displayable": True, "user_visible_reasons": ["ты сказала, что ноет спина после работы"],
                     "internal_only": [INTERNAL_ONLY]},
    )
    if role == "alternative":
        base["rerank_reason"] = "ALTERNATIVE_REQUESTED"
    base.update(over)
    return RecommendationInput(**base)


def _set_in(subject, **over) -> RecommendationSetInput:
    base = dict(
        subject_ref=str(subject.pk), intent_id="i", versions=VERSIONS,
        result_status="CLEAR_PRIMARY", readiness_state="READY",
        reason_codes=["CLEAR_PRIMARY_BY_POLICY"],
        evidence_refs=[{"source": "conversation", "ref": "msg-set"}],
        explanation={"displayable": True, "user_visible_reasons": ["подходит под твою цель"],
                     "internal_only": [SET_INTERNAL_ONLY]},
        safety_evaluation_ref=SAFETY, context_snapshot=SNAPSHOT, primary=_rec(),
    )
    base.update(over)
    return RecommendationSetInput(**base)


@pytest.fixture
def rset(subject):
    now = timezone.now()
    alt = _rec("alternative", direction_code="IMPROVE_RELAXATION", family="SUPPORT",
               explanation={"displayable": True, "user_visible_reasons": ["или просто расслабиться"],
                            "internal_only": [INTERNAL_ONLY]})
    return persist(_set_in(
        subject, intent_id="intent-1", execution_mode="LIVE",
        conversation_ref={"conversation_id": "c-1", "trace_id": "t-1"}, alternatives=(alt,),
    ), now=now)


def _set_url(user, rset) -> str:
    return f"/api/v1/internal/users/{user.pk}/recommendations/{rset.pk}/"


def _events_url(user, rec) -> str:
    return f"/api/v1/internal/users/{user.pk}/recommendations/{rec.pk}/events/"


# ---------------------------------------------------------------- чтение

def test_read_returns_outcome_direction_why_lineage_and_actionable(bearer, subject, rset):
    resp = _api(bearer, subject).get(_set_url(subject, rset))
    assert resp.status_code == 200, resp.content[:300]
    data = resp.json()["data"]
    assert data["recommendation_set_id"] == str(rset.pk) and data["subject_id"] == str(subject.pk)
    assert data["execution_mode"] == "LIVE" and data["conversation_ref"]["trace_id"] == "t-1"
    assert data["record_schema_version"] == "1.2"
    o = data["outcome"]
    assert o["result_status"] == "CLEAR_PRIMARY" and o["readiness_state"] == "READY" and o["safety_state"] == "NORMAL"
    assert o["reason_codes"] == ["CLEAR_PRIMARY_BY_POLICY"] and o["why"] == ["подходит под твою цель"]
    p = data["primary"]
    assert p["decision_subject"] == {"direction_code": "REDUCE_MUSCLE_TENSION_BACK", "family": "ADDRESS",
                                     "target_outcomes": ["REDUCE(MUSCLE_TENSION)"]}
    assert p["why"] == ["ты сказала, что ноет спина после работы"] and p["displayable"] is True
    assert p["actionable"] is True and p["role"] == "primary" and p["parent_recommendation_id"] is None
    assert p["reason_codes"] == ["ELIG_CAPABILITY_VERIFIED"]
    # исход, готовность и безопасность — только у набора (DRF-1905)
    assert not {"result_status", "readiness_state", "safety_state"} & set(p)
    a = data["alternatives"][0]
    assert a["role"] == "alternative" and a["parent_recommendation_id"] == p["recommendation_id"]
    assert a["rerank_reason"] == "ALTERNATIVE_REQUESTED" and a["why"] == ["или просто расслабиться"]


def test_internal_only_never_leaves_the_record(bearer, subject, rset):
    raw = _api(bearer, subject).get(_set_url(subject, rset)).content.decode()
    assert INTERNAL_ONLY not in raw and SET_INTERNAL_ONLY not in raw
    assert "internal_only" not in raw and "provider_score" not in raw and "policy_threshold" not in raw
    # содержимое снимка (DRF-1906) чтением не отдаётся — ни значения, ни вопрос
    assert '"weekend"' not in raw and "said.visit_context" not in raw and "context_snapshot" not in raw
    for forbidden in ("service", "specialist", "provider", "price", "slot", "distance"):
        assert f'"{forbidden}"' not in raw, forbidden


def test_not_displayable_gives_empty_why_not_internal_text(bearer, subject):
    rset = persist(_set_in(
        subject,
        explanation={"displayable": False, "user_visible_reasons": ["исход не показывать"],
                     "internal_only": [SET_INTERNAL_ONLY]},
        primary=_rec(explanation={"displayable": False, "user_visible_reasons": ["не показывать"],
                                  "internal_only": [INTERNAL_ONLY]}),
    ))
    resp = _api(bearer, subject).get(_set_url(subject, rset))
    data = resp.json()["data"]
    assert data["primary"]["displayable"] is False and data["primary"]["why"] == []
    assert data["outcome"]["displayable"] is False and data["outcome"]["why"] == []
    raw = resp.content.decode()
    assert "не показывать" not in raw and "исход не показывать" not in raw


def test_actionable_flips_after_two_hours_but_the_record_stays(bearer, subject):
    then = timezone.now() - timedelta(hours=2, minutes=1)
    rset = persist(_set_in(subject), now=then)
    resp = _api(bearer, subject).get(_set_url(subject, rset))
    assert resp.status_code == 200
    p = resp.json()["data"]["primary"]
    assert p["actionable"] is False and p["recommendation_id"]


def test_foreign_subject_gets_404_and_unnamed_caller_403(bearer, subject, stranger, rset):
    # чужой субъект — набор для него не существует
    assert _api(bearer, stranger).get(_set_url(stranger, rset)).status_code == 404
    # субъект в URL ≠ актор в заголовке — сторож DRF-1617
    assert _api(bearer, stranger).get(_set_url(subject, rset)).status_code == 403
    # без заголовка субъекта
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
    assert c.get(_set_url(subject, rset)).status_code == 403
    # чужой bearer
    c.credentials(HTTP_AUTHORIZATION="Bearer nope", HTTP_X_EXTERNAL_USER_ID=name_subject(subject))
    assert c.get(_set_url(subject, rset)).status_code == 403


def test_safety_boundary_set_reads_without_nba(bearer, subject):
    """DRF-1905: исход без NBA — primary null, alternatives пусты, исход объяснён набором."""
    rset = persist(_set_in(
        subject, result_status="SAFETY_BOUNDARY", readiness_state="BLOCKED", primary=None,
        reason_codes=["SAFETY_STOP"],
        explanation={"displayable": False, "user_visible_reasons": [], "internal_only": ["SAFETY_STOP"]},
        safety_evaluation_ref={**SAFETY, "state": "STOP"},
    ))
    resp = _api(bearer, subject).get(_set_url(subject, rset))
    assert resp.status_code == 200, resp.content[:300]
    data = resp.json()["data"]
    assert data["primary"] is None and data["alternatives"] == []
    o = data["outcome"]
    assert o["result_status"] == "SAFETY_BOUNDARY" and o["readiness_state"] == "BLOCKED"
    assert o["safety_state"] == "STOP" and o["reason_codes"] == ["SAFETY_STOP"]
    assert o["displayable"] is False and o["why"] == [] and o["evidence_refs"] == []


# ---------------------------------------------------------------- события

def test_channel_events_append_and_b8_names_are_refused(bearer, subject, rset):
    rec = rset.primary
    api = _api(bearer, subject)
    for kind in ("recommendation.presented", "recommendation.explanation_requested",
                 "recommendation.alternative_requested", "recommendation.engaged"):
        resp = api.post(_events_url(subject, rec), {"kind": kind, "channel": "max", "channel_message_id": "m-1"},
                        format="json")
        assert resp.status_code == 201, (kind, resp.content[:200])
    kinds = list(RecommendationEvent.objects.filter(recommendation=rec).order_by("occurred_at", "pk")
                 .values_list("kind", flat=True))
    assert kinds[0] == "recommendation.created" and len(kinds) == 5
    ev = RecommendationEvent.objects.get(recommendation=rec, kind="recommendation.engaged")
    assert ev.payload["channel"] == "max" and ev.producer == "Channel Delivery / Interaction"

    for bad in ("recommendation.accepted", "recommendation.declined", "accepted"):
        resp = api.post(_events_url(subject, rec), {"kind": bad}, format="json")
        assert resp.status_code == 400 and resp.json()["error"]["code"] == "EVENT_NOT_IN_TAXONOMY", bad
    for not_here in ("recommendation.created", "booking_intent.created", "recommendation.generated"):
        resp = api.post(_events_url(subject, rec), {"kind": not_here}, format="json")
        assert resp.status_code == 400 and resp.json()["error"]["code"] == "VALIDATION_ERROR", not_here
    assert RecommendationEvent.objects.filter(recommendation=rec).count() == 5


def test_events_of_a_foreign_subject_are_404(bearer, subject, stranger, rset):
    resp = _api(bearer, stranger).post(_events_url(stranger, rset.primary), {"kind": "recommendation.engaged"},
                                       format="json")
    assert resp.status_code == 404
    assert not RecommendationEvent.objects.filter(kind="recommendation.engaged").exists()


def test_response_is_plain_json_without_scores(bearer, subject, rset):
    body = json.loads(_api(bearer, subject).get(_set_url(subject, rset)).content.decode())

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                assert k not in {"score", "confidence", "rank"}, k
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(body)


# ---------------------------------------------------------------- evidence_refs (DRF-1889)

#: Метки, которые не имеют права появиться нигде в теле ответа.
HIDDEN_REFS = {
    "safety": "SAFETY-REF-7f3", "anketa": "ANKETA-REF-7f3", "policy": "POLICY-REF-7f3",
    "operator": "OPERATOR-REF-7f3", "catalog": "CATALOG-REF-7f3", "brand_new_source": "NEWSRC-REF-7f3",
}
STORED = [
    {"source": "conversation", "ref": "msg-1", "said_at": "2026-09-15T10:00:00Z"},
    *({"source": s, "ref": r} for s, r in HIDDEN_REFS.items()),
    {"source": "user_stated", "ref": "u-1"},
    {"source": "journey", "ref": "j-1", "said_at": "2026-09-15T10:05:00Z", "text": "СЫРОЙ ТЕКСТ РЕПЛИКИ"},
]
SHOWN = [
    {"source": "conversation", "ref": "msg-1", "said_at": "2026-09-15T10:00:00Z"},
    {"source": "user_stated", "ref": "u-1", "said_at": None},
    {"source": "journey", "ref": "j-1", "said_at": "2026-09-15T10:05:00Z"},
]


def _assert_hidden_absent(raw: str) -> None:
    for source, ref in HIDDEN_REFS.items():
        assert ref not in raw and f'"{source}"' not in raw, source
    assert "СЫРОЙ ТЕКСТ" not in raw and '"text"' not in raw


def test_evidence_refs_show_only_allowed_sources_and_only_three_keys(bearer, subject):
    """Вариант: разрешённые источники — в порядке хранения, ровно {source, ref, said_at}; остальное — нет."""
    rset = persist(_set_in(subject, primary=_rec(evidence_refs=list(STORED))))
    resp = _api(bearer, subject).get(_set_url(subject, rset))
    assert resp.status_code == 200, resp.content[:300]
    assert resp.json()["data"]["primary"]["evidence_refs"] == SHOWN
    _assert_hidden_absent(resp.content.decode())


def test_outcome_evidence_follows_the_same_rule(bearer, subject):
    """DRF-1905: evidence исхода набора — тот же закрытый список и та же форма."""
    rset = persist(_set_in(subject, evidence_refs=list(STORED)))
    resp = _api(bearer, subject).get(_set_url(subject, rset))
    assert resp.json()["data"]["outcome"]["evidence_refs"] == SHOWN
    _assert_hidden_absent(resp.content.decode())


def test_not_displayable_record_shows_no_evidence(bearer, subject):
    rset = persist(_set_in(
        subject,
        evidence_refs=[{"source": "conversation", "ref": "msg-set-hidden-7f3"}],
        explanation={"displayable": False, "user_visible_reasons": [], "internal_only": []},
        primary=_rec(
            evidence_refs=[{"source": "conversation", "ref": "msg-hidden-7f3"}],
            explanation={"displayable": False, "user_visible_reasons": [], "internal_only": []},
        ),
    ))
    resp = _api(bearer, subject).get(_set_url(subject, rset))
    data = resp.json()["data"]
    assert data["primary"]["evidence_refs"] == [] and data["outcome"]["evidence_refs"] == []
    raw = resp.content.decode()
    assert "msg-hidden-7f3" not in raw and "msg-set-hidden-7f3" not in raw


def test_allowlist_names_no_health_or_internal_source():
    """Положительная стража состава: расширить список можно, но не этими именами (§13, §23, OQ-REC-6)."""
    from recommendation.record_api import EVIDENCE_DISPLAYABLE_SOURCES

    assert EVIDENCE_DISPLAYABLE_SOURCES, "пустой список прячет всё — и тест выше зеленел бы по пустоте"
    assert not EVIDENCE_DISPLAYABLE_SOURCES & {"safety", "anketa", "policy", "operator", "catalog"}
