"""Вход записи Recommendation для производителя NBA — под субъектом (DRF-1888).

Что стережётся:

* набор пишется через тот же ``persist``: 201, id набора и записей, чтение
  назад GET-ручкой #428 даёт то же;
* ``execution_mode`` ставит сервер — ``SHADOW``; ``LIVE`` от вызывающего → 400
  ``LIVE_NOT_ALLOWED`` и **ноль строк** (C1);
* ``X-Idempotency-Key`` обязателен; повтор с тем же телом — тот же ответ и ноль
  новых строк; тот же ключ с другим телом → 422; запись immutable, поэтому дубль
  можно только не создать;
* неполное решение — 400 с именем поля и ноль строк; лишнее поле (``service_id``)
  — в варианте или на верхнем уровне — отказ по имени, а не молчаливый пропуск (B2);
* чужой или неназванный субъект → 403; живая заявка на удаление → 423 (§7 D2);
  в обоих случаях ноль строк;
* **писатель один**: ``persist`` вне тестов зовётся ровно из этой ручки (AST,
  с нижней границей переписи и положительным контролем).

DRF-1905 — исход на уровне набора: сегодняшний честный исход мозга без NBA
(``SAFETY_BOUNDARY``) пишется без primary и alternatives — 201 и ноль записей
варианта; исход без NBA с вариантами и строчный ``readiness_state`` — 400 по имени.
"""
from __future__ import annotations

import ast
import copy
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from recommendation.models import ContextSnapshot, Recommendation, RecommendationEvent, RecommendationSet
from recommendation.snapshots import content_digest
from users.models import DeletionRequest, User
from users.tests.conftest import name_subject

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[2]

SNAPSHOT_CONTENT = {
    "snapshot_version": "turn-context-v1",
    "decision_readiness": {"state_revision": 2, "readiness_state": "blocked"},
    "said": [{"key": "visit_context", "value": "evening", "origin": "conversation", "said_on": "2026-09-15"}],
    "answered_question": None,
}


@pytest.fixture
def bearer(settings):
    settings.AYLA_INTERNAL_API_TOKEN = "test-bearer-1888"
    return "test-bearer-1888"


@pytest.fixture
def subject(db):
    return User.objects.create_user(username="ingress-subject", password="x", role="client", phone="+79992221888")


@pytest.fixture
def stranger(db):
    return User.objects.create_user(username="ingress-stranger", password="x", role="client", phone="+79992221889")


def _api(bearer, named_user, key: str | None = "intent-1:trace-1") -> APIClient:
    c = APIClient()
    headers = {"HTTP_AUTHORIZATION": f"Bearer {bearer}", "HTTP_X_EXTERNAL_USER_ID": name_subject(named_user)}
    if key is not None:
        headers["HTTP_X_IDEMPOTENCY_KEY"] = key
    c.credentials(**headers)
    return c


def _url(user) -> str:
    return f"/api/v1/internal/users/{user.pk}/recommendation-sets/"


def _rec(**over) -> dict:
    base = {
        "role": "primary", "direction_code": "REDUCE_MUSCLE_TENSION_BACK", "family": "ADDRESS",
        "target": "BACK_COMFORT", "action_type": "PROVIDER_SESSION",
        "target_outcomes": ["REDUCE(MUSCLE_TENSION)"],
        "reason_codes": ["ELIG_CAPABILITY_VERIFIED"],
        "evidence_refs": [{"source": "conversation", "ref": "msg-1", "said_at": "2026-09-15T10:00:00Z"}],
        "explanation": {
            "displayable": True, "user_visible_reasons": ["ты сказала, что ноет спина"], "internal_only": [],
        },
    }
    base.update(over)
    return base


def _body(**over) -> dict:
    base = {
        "intent_id": "intent-1",
        "conversation_ref": {"conversation_id": "c-1", "trace_id": "trace-1"},
        "versions": {"decision_policy": "dp-1", "taxonomy": "tx-1", "safety_policy": "sp-1",
                     "catalog_mapping": "cm-1", "presentation_policy": "pp-1"},
        "result_status": "CLEAR_PRIMARY",
        "readiness_state": "READY",
        "reason_codes": ["CLEAR_PRIMARY_BY_POLICY"],
        "evidence_refs": [{"source": "conversation", "ref": "msg-1", "said_at": "2026-09-15T10:00:00Z"}],
        "explanation": {"displayable": True, "user_visible_reasons": ["подходит под твою цель"], "internal_only": []},
        "safety_evaluation_ref": {"state": "NORMAL", "rule_id": "r-0", "policy_version": "sp-1",
                                  "evidence_ref": "ev-0", "activated_at": "2026-09-15T10:00:00Z"},
        "context_snapshot": {"snapshot_version": "turn-context-v1", "content_digest": content_digest(SNAPSHOT_CONTENT),
                             "content": copy.deepcopy(SNAPSHOT_CONTENT)},
        "primary": _rec(),
        "alternatives": [_rec(role="alternative", direction_code="IMPROVE_RELAXATION", family="SUPPORT",
                              rerank_reason="ALTERNATIVE_REQUESTED")],
    }
    base.update(over)
    return base


def _boundary_body(**over) -> dict:
    """Сегодняшний честный исход мозга без NBA (6.4, в тени) — SAFETY_BOUNDARY."""
    base = _body(
        result_status="SAFETY_BOUNDARY", readiness_state="BLOCKED", reason_codes=["SAFETY_STOP"],
        explanation={"displayable": False, "user_visible_reasons": [], "internal_only": ["SAFETY_STOP"]},
        safety_evaluation_ref={"state": "STOP", "rule_id": "r-stop", "policy_version": "sp-1",
                               "evidence_ref": "ev-1", "activated_at": "2026-09-15T10:00:00Z"},
        primary=None, alternatives=[],
    )
    base.update(over)
    return base


def _rows() -> tuple[int, int, int]:
    return RecommendationSet.objects.count(), Recommendation.objects.count(), RecommendationEvent.objects.count()


# ---------------------------------------------------------------- запись

def test_creates_a_shadow_set_and_reads_back(bearer, subject):
    resp = _api(bearer, subject).post(_url(subject), _body(), format="json")
    assert resp.status_code == 201, resp.content[:400]
    data = resp.json()["data"]
    assert data["execution_mode"] == "SHADOW" and data["result_status"] == "CLEAR_PRIMARY"
    assert data["primary_recommendation_id"] and len(data["alternative_recommendation_ids"]) == 1
    assert _rows() == (1, 2, 2)                                   # set + primary + alternative, два created

    read = _api(bearer, subject).get(
        f"/api/v1/internal/users/{subject.pk}/recommendations/{data['recommendation_set_id']}/",
    )
    assert read.status_code == 200, read.content[:300]
    got = read.json()["data"]
    assert got["execution_mode"] == "SHADOW" and got["subject_id"] == str(subject.pk)
    assert got["outcome"]["result_status"] == "CLEAR_PRIMARY"
    assert got["primary"]["recommendation_id"] == data["primary_recommendation_id"]
    assert got["alternatives"][0]["parent_recommendation_id"] == data["primary_recommendation_id"]


def test_snapshot_is_stored_with_the_set_and_the_ref_is_built_by_the_catalog(bearer, subject):
    """DRF-1906: снимок приходит содержимым, строка снимка и набор — одна запись; ссылку отдаёт каталог."""
    resp = _api(bearer, subject).post(_url(subject), _boundary_body(), format="json")
    assert resp.status_code == 201, resp.content[:400]
    snap = ContextSnapshot.objects.get()
    assert RecommendationSet.objects.get().context_snapshot_id == snap.pk and snap.subject_ref == str(subject.pk)
    assert resp.json()["data"]["context_snapshot_ref"] == {
        "snapshot_id": str(snap.pk), "snapshot_version": "turn-context-v1",
        "content_digest": content_digest(SNAPSHOT_CONTENT),
    }
    assert snap.content == SNAPSHOT_CONTENT


def test_a_caller_made_snapshot_ref_is_refused_by_name(bearer, subject):
    body = _boundary_body()
    body["context_snapshot_ref"] = {"snapshot_id": "ctx-1", "snapshot_version": 1, "content_digest": "sha256:abc"}
    resp = _api(bearer, subject).post(_url(subject), body, format="json")
    assert resp.status_code == 400, resp.content[:300]
    assert "context_snapshot_ref: ссылку на снимок строит каталог" in resp.json()["error"]["message"]
    assert _rows() == (0, 0, 0) and ContextSnapshot.objects.count() == 0


def test_snapshot_id_inside_the_snapshot_is_refused_by_name(bearer, subject):
    body = _boundary_body()
    body["context_snapshot"]["snapshot_id"] = "ctx-1"
    resp = _api(bearer, subject).post(_url(subject), body, format="json")
    assert resp.status_code == 400, resp.content[:300]
    assert "context_snapshot: поля ['snapshot_id']" in resp.json()["error"]["message"]
    assert ContextSnapshot.objects.count() == 0


def test_digest_mismatch_and_text_in_snapshot_are_refused_by_name(bearer, subject):
    body = _boundary_body()
    body["context_snapshot"]["content_digest"] = "0" * 64
    resp = _api(bearer, subject).post(_url(subject), body, format="json")
    assert resp.status_code == 400 and "content_digest не совпадает" in resp.json()["error"]["message"]

    body = _boundary_body()
    body["context_snapshot"]["content"]["said"][0]["value"] = "болит спина после работы"
    body["context_snapshot"]["content_digest"] = content_digest(body["context_snapshot"]["content"])
    resp = _api(bearer, subject, key="intent-1:trace-2").post(_url(subject), body, format="json")
    assert resp.status_code == 400, resp.content[:300]
    message = resp.json()["error"]["message"]
    assert "context_snapshot: content.said[0].value" in message and "болит" not in message
    assert _rows() == (0, 0, 0) and ContextSnapshot.objects.count() == 0


def test_internal_only_text_is_refused_at_the_ingress(bearer, subject):
    body = _boundary_body(explanation={"displayable": False, "user_visible_reasons": [],
                                       "internal_only": ["provider_score=0.91"]})
    resp = _api(bearer, subject).post(_url(subject), body, format="json")
    assert resp.status_code == 400, resp.content[:300]
    assert "explanation.internal_only[0]" in resp.json()["error"]["message"]
    assert _rows() == (0, 0, 0)


def test_safety_boundary_is_written_without_any_variant(bearer, subject):
    resp = _api(bearer, subject).post(_url(subject), _boundary_body(), format="json")
    assert resp.status_code == 201, resp.content[:400]
    data = resp.json()["data"]
    assert data["result_status"] == "SAFETY_BOUNDARY"
    assert data["primary_recommendation_id"] is None and data["alternative_recommendation_ids"] == []
    assert _rows() == (1, 0, 0)


def test_no_nba_outcome_with_a_primary_is_refused_by_name(bearer, subject):
    resp = _api(bearer, subject).post(_url(subject), _boundary_body(primary=_rec()), format="json")
    assert resp.status_code == 400, resp.content[:300]
    assert "не является NBA" in resp.json()["error"]["message"]
    assert _rows() == (0, 0, 0)


def test_lowercase_readiness_is_refused_by_name(bearer, subject):
    """Движок бота отдаёт строчные; каталог не нормализует за вызывающего."""
    resp = _api(bearer, subject).post(_url(subject), _boundary_body(readiness_state="blocked"), format="json")
    assert resp.status_code == 400, resp.content[:300]
    assert "readiness_state" in resp.json()["error"]["message"]
    assert _rows() == (0, 0, 0)


def test_live_from_the_caller_is_refused_and_nothing_is_written(bearer, subject):
    resp = _api(bearer, subject).post(_url(subject), _body(execution_mode="LIVE"), format="json")
    assert resp.status_code == 400, resp.content[:300]
    assert resp.json()["error"]["code"] == "LIVE_NOT_ALLOWED"
    assert _rows() == (0, 0, 0)


def test_idempotency_key_is_required_before_anything(bearer, subject):
    resp = _api(bearer, subject, key=None).post(_url(subject), _body(), format="json")
    assert resp.status_code == 400, resp.content[:300]
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert _rows() == (0, 0, 0)


def test_replay_returns_the_same_set_and_writes_nothing_new(bearer, subject):
    first = _api(bearer, subject).post(_url(subject), _body(), format="json")
    again = _api(bearer, subject).post(_url(subject), _body(), format="json")
    assert first.status_code == again.status_code == 201, (first.content[:200], again.content[:200])
    assert again.json()["data"]["recommendation_set_id"] == first.json()["data"]["recommendation_set_id"]
    assert _rows() == (1, 2, 2)


def test_same_key_with_a_different_decision_is_a_conflict(bearer, subject):
    assert _api(bearer, subject).post(_url(subject), _body(), format="json").status_code == 201
    other = _body(primary=_rec(direction_code="IMPROVE_SLEEP"))
    resp = _api(bearer, subject).post(_url(subject), other, format="json")
    assert resp.status_code == 422, resp.content[:300]
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert _rows() == (1, 2, 2)


def test_incomplete_decision_is_refused_by_field_name(bearer, subject):
    resp = _api(bearer, subject).post(_url(subject), _body(primary=_rec(reason_codes=[])), format="json")
    assert resp.status_code == 400, resp.content[:300]
    assert "primary: reason_codes" in resp.json()["error"]["message"]
    assert _rows() == (0, 0, 0)


def test_execution_fields_are_refused_by_name_not_dropped(bearer, subject):
    resp = _api(bearer, subject).post(_url(subject), _body(primary=_rec(service_id="svc-1")), format="json")
    assert resp.status_code == 400, resp.content[:300]
    assert "service_id" in resp.json()["error"]["message"]
    assert _rows() == (0, 0, 0)


def test_unknown_top_level_field_is_refused_by_name_not_dropped(bearer, subject):
    """Сериализатор DRF выбросил бы ключ молча — одно правило «лишнее поле → отказ» на оба уровня."""
    resp = _api(bearer, subject).post(_url(subject), _body(service_id="svc-1"), format="json")
    assert resp.status_code == 400, resp.content[:300]
    assert "service_id" in resp.json()["error"]["message"]
    assert _rows() == (0, 0, 0)


def test_foreign_or_unnamed_subject_is_refused(bearer, subject, stranger):
    as_stranger = _api(bearer, stranger).post(_url(subject), _body(), format="json")
    assert as_stranger.status_code == 403, as_stranger.content[:300]
    unnamed = APIClient()
    unnamed.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}", HTTP_X_IDEMPOTENCY_KEY="k")
    assert unnamed.post(_url(subject), _body(), format="json").status_code == 403
    assert _rows() == (0, 0, 0)


def test_open_deletion_request_stops_the_write(bearer, subject):
    DeletionRequest.objects.create(
        user=subject, status=DeletionRequest.Status.REQUESTED, initiator="bot",
        deadline_at=timezone.now() + timedelta(days=30),
    )
    resp = _api(bearer, subject).post(_url(subject), _body(), format="json")
    assert resp.status_code == 423, resp.content[:300]
    assert _rows() == (0, 0, 0)


def test_the_body_is_not_mutated_into_live_by_the_server_either(bearer, subject):
    """Положительная стража: SHADOW явно — тоже 201 и SHADOW (значение принимается, не переписывается)."""
    body = _body(execution_mode="SHADOW")
    resp = _api(bearer, subject).post(_url(subject), copy.deepcopy(body), format="json")
    assert resp.status_code == 201, resp.content[:300]
    assert RecommendationSet.objects.get().execution_mode == "SHADOW"


def test_idempotency_helper_still_defaults_to_request_user(subject):
    """``user=`` добавлен для субъектной поверхности; запись/отмена визита его не передают.

    Положительная стража умолчания: без ``user`` ключ принадлежит ``request.user`` —
    иначе четыре вызова в appointments молча сменили бы владельца ключа.
    """
    from rest_framework.parsers import JSONParser
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory

    from appointments.infrastructure.idempotency import lookup_or_open_idempotency
    from appointments.models import IdempotencyKey

    django_request = APIRequestFactory().post("/x/", {"a": 1}, format="json", HTTP_X_IDEMPOTENCY_KEY="k-default")
    # Помощник читает request.data (хеш тела) — без JSON-парсера Request падает UnsupportedMediaType.
    request = Request(django_request, parsers=[JSONParser()])
    request.user = subject
    cached, record = lookup_or_open_idempotency(request, operation_name="booking.cancel", target_id="t")
    assert cached is None and record is not None
    assert IdempotencyKey.objects.get(pk=record.pk).user_id == subject.pk


# ---------------------------------------------------------------- писатель один

#: Нижняя граница переписи: рабочих .py-файлов на dev больше 400. Меньше — скан не того корня.
MIN_SCANNED = 400
_SKIP_PARTS = frozenset({"tests", "migrations", "venv", ".venv", "node_modules"})


def _persist_calls() -> tuple[int, dict[str, int]]:
    scanned, calls = 0, {}
    for path in sorted(ROOT.rglob("*.py")):
        parts = path.relative_to(ROOT).parts
        if any(p in _SKIP_PARTS or p.startswith(".") for p in parts):
            continue
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"))
        n = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1] == "persist"
        )
        if n:
            calls["/".join(parts)] = n
    return scanned, calls


def test_persist_has_exactly_one_caller_outside_tests():
    """Запись immutable и её пишет один вход: второй писатель — второй набор правил."""
    scanned, calls = _persist_calls()
    assert scanned >= MIN_SCANNED, f"просканировано {scanned} файлов — корень не тот"
    assert calls == {"recommendation/record_api.py": 1}, calls
