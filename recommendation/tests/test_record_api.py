"""Чтение записи Recommendation для бота и события — под субъектом (DRF-1666 / DRF-1617).

Что стережётся:

* набор читается только своим субъектом: чужой субъект → 404; субъект в URL
  ≠ актор, без заголовка, чужой bearer → 403 (как на всей поверхности DRF-1617);
* ``actionable`` считается на момент ответа: до 2 ч ``true``, после — ``false``,
  запись на месте;
* ``why`` — только ``user_visible_reasons`` и только при ``displayable``;
  **internal-only текст не встречается нигде в теле ответа** (сторож по
  сырому JSON, не по одному полю);
* primary + alternatives с ``parent`` / ``rerank_reason`` (код), ``execution_mode``;
* услуги/мастера/цены/слота в ответе нет;
* события: четыре канальных → 201 и append; ``accepted``/``declined`` → 400
  ``EVENT_NOT_IN_TAXONOMY`` (B8); ``created`` и ``booking_intent.created`` этой
  ручкой не пишутся; чужой субъект → 404.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from recommendation.models import RecommendationEvent
from recommendation.records import PolicyVersions, RecommendationInput, RecommendationSetInput, persist
from users.models import User
from users.tests.conftest import name_subject

pytestmark = pytest.mark.django_db

INTERNAL_ONLY = "INTERNAL-ONLY-SIGNAL rating=4.8 provider_score=0.91"


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
        target_outcomes=["REDUCE(MUSCLE_TENSION)"], result_status="CLEAR_PRIMARY", readiness_state="READY",
        reason_codes=["ELIG_CAPABILITY_VERIFIED"],
        evidence_refs=[{"source": "conversation", "ref": "msg-1"}],
        explanation={"displayable": True, "user_visible_reasons": ["ты сказала, что ноет спина после работы"],
                     "internal_only": [INTERNAL_ONLY]},
        safety_evaluation_ref={"state": "NORMAL", "rule_id": "r", "policy_version": "sp", "evidence_ref": "e",
                               "activated_at": "2026-09-12T10:00:00Z"},
        context_snapshot_ref={"snapshot_id": "ctx", "snapshot_version": 1, "content_digest": "sha256:x"},
    )
    if role == "alternative":
        base["rerank_reason"] = "ALTERNATIVE_REQUESTED"
    base.update(over)
    return RecommendationInput(**base)


@pytest.fixture
def rset(subject):
    now = timezone.now()
    alt = _rec("alternative", direction_code="IMPROVE_RELAXATION", family="SUPPORT",
               explanation={"displayable": True, "user_visible_reasons": ["или просто расслабиться"],
                            "internal_only": [INTERNAL_ONLY]})
    return persist(RecommendationSetInput(
        subject_ref=str(subject.pk), intent_id="intent-1", execution_mode="LIVE",
        conversation_ref={"conversation_id": "c-1", "trace_id": "t-1"},
        versions=PolicyVersions("dp", "tx", "sp", "cm", "pp"),
        primary=_rec(), alternatives=(alt,),
    ), now=now)


def _set_url(user, rset) -> str:
    return f"/api/v1/internal/users/{user.pk}/recommendations/{rset.pk}/"


def _events_url(user, rec) -> str:
    return f"/api/v1/internal/users/{user.pk}/recommendations/{rec.pk}/events/"


# ---------------------------------------------------------------- чтение

def test_read_returns_direction_why_lineage_and_actionable(bearer, subject, rset):
    resp = _api(bearer, subject).get(_set_url(subject, rset))
    assert resp.status_code == 200, resp.content[:300]
    data = resp.json()["data"]
    assert data["recommendation_set_id"] == str(rset.pk) and data["subject_id"] == str(subject.pk)
    assert data["execution_mode"] == "LIVE" and data["conversation_ref"]["trace_id"] == "t-1"
    p = data["primary"]
    assert p["decision_subject"] == {"direction_code": "REDUCE_MUSCLE_TENSION_BACK", "family": "ADDRESS",
                                     "target_outcomes": ["REDUCE(MUSCLE_TENSION)"]}
    assert p["why"] == ["ты сказала, что ноет спина после работы"] and p["displayable"] is True
    assert p["actionable"] is True and p["role"] == "primary" and p["parent_recommendation_id"] is None
    assert p["safety_state"] == "NORMAL" and p["reason_codes"] == ["ELIG_CAPABILITY_VERIFIED"]
    a = data["alternatives"][0]
    assert a["role"] == "alternative" and a["parent_recommendation_id"] == p["recommendation_id"]
    assert a["rerank_reason"] == "ALTERNATIVE_REQUESTED" and a["why"] == ["или просто расслабиться"]


def test_internal_only_never_leaves_the_record(bearer, subject, rset):
    raw = _api(bearer, subject).get(_set_url(subject, rset)).content.decode()
    assert INTERNAL_ONLY not in raw and "internal_only" not in raw and "provider_score" not in raw
    for forbidden in ("service", "specialist", "provider", "price", "slot", "distance"):
        assert f'"{forbidden}"' not in raw, forbidden


def test_not_displayable_gives_empty_why_not_internal_text(bearer, subject):
    rset = persist(RecommendationSetInput(
        subject_ref=str(subject.pk), intent_id="i", versions=PolicyVersions("dp", "tx", "sp", "cm", "pp"),
        primary=_rec(explanation={"displayable": False, "user_visible_reasons": ["не показывать"],
                                  "internal_only": [INTERNAL_ONLY]}),
    ))
    resp = _api(bearer, subject).get(_set_url(subject, rset))
    p = resp.json()["data"]["primary"]
    assert p["displayable"] is False and p["why"] == []
    assert "не показывать" not in resp.content.decode()


def test_actionable_flips_after_two_hours_but_the_record_stays(bearer, subject):
    then = timezone.now() - timedelta(hours=2, minutes=1)
    rset = persist(RecommendationSetInput(
        subject_ref=str(subject.pk), intent_id="i", versions=PolicyVersions("dp", "tx", "sp", "cm", "pp"),
        primary=_rec(),
    ), now=then)
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
