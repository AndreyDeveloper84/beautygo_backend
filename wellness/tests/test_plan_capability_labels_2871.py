"""Подпись способности по ключу — для экрана плана (DRF-2871).

У шага плана текста нет по контракту Plan Engine (PE-2): он несёт ключ
способности. Экрану нужна формулировка для человека — и она берётся только из
ПОДТВЕРЖДЁННОГО знания каталога. Что держат узлы:

* подпись есть только у подтверждённой, поддержанной, не истёкшей возможности;
* ключ в общем словаре уникален (DRF-2743): у ключа одна запись и одна
  формулировка, сколько бы процедур к ней ни было привязано;
* (до словаря) один ключ у двух процедур с разными текстами — подписи нет (``ambiguous``):
  выбирать формулировку за владельца код не вправе; одинаковые тексты — одна;
* «подписи нет» всегда с именем причины, а не пустой строкой;
* ручка под тем же флагом, что и остальной Plan Engine.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from services.capabilities import LabelState, capability_labels
from services.models import ClaimEvidence, ProcedureCapability, ServiceCategory, ServiceTemplate
from users.models import User

pytestmark = pytest.mark.django_db

VALID_TOKEN = "test-ayla-internal-token-plan-labels"
URL = "/api/v1/internal/me/plan/capability-labels/"
OWNER = "bot:plan-labels-owner"


@pytest.fixture(autouse=True)
def _token_and_flag(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.PLAN_ENGINE_ENABLED = True


@pytest.fixture
def owner(db) -> User:
    return User.objects.create_user(
        username=OWNER, password="x", role="client", phone="+79995028721", is_proxy=True,
    )


@pytest.fixture
def curator(db) -> User:
    return User.objects.create_user(
        username="plan_labels_curator", password="x", role="client", phone="+79995028722",
    )


@pytest.fixture
def templates(db):
    category = ServiceCategory.objects.create(name="Массаж labels", slug="massage-labels-2871")
    return (
        ServiceTemplate.objects.create(category=category, name="Массаж спины labels"),
        ServiceTemplate.objects.create(category=category, name="Массаж тела labels"),
    )


def _capability(template, key: str, text: str, curator, **over) -> ProcedureCapability:
    """Запись словаря, привязанная к одной процедуре или к нескольким (список)."""
    fields = dict(
        status=ClaimEvidence.Status.APPROVED,
        claim_type=ClaimEvidence.ClaimType.PRODUCT,
        evidence_kind=ClaimEvidence.EvidenceKind.PROFESSIONAL_CONSENSUS,
        claim_scope=ClaimEvidence.ClaimScope.SUPPORTED,
        confirmed_by=curator,
        confirmed_at=timezone.now() - timedelta(days=1),
        source_ref="owner-review-labels",
    )
    fields.update(over)
    templates = list(template) if isinstance(template, (list, tuple)) else [template]
    return ProcedureCapability.objects.create(templates=templates, key=key, text_client=text, **fields)


def _api() -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = OWNER
    return c


class TestReader:
    def test_a_confirmed_capability_has_its_client_text(self, templates, curator) -> None:
        _capability(templates[0], "tension-relief", "Снимает мышечное напряжение", curator)
        label = capability_labels(["tension-relief"])["tension-relief"]
        assert (label.state, label.label) == (LabelState.LABELLED, "Снимает мышечное напряжение")

    def test_one_capability_bound_to_two_procedures_is_one_label(self, templates, curator) -> None:
        """Общий словарь (DRF-2743): одна запись у двух процедур — одна подпись."""
        _capability(list(templates), "relaxation", "Помогает расслабиться", curator)
        label = capability_labels(["relaxation"])["relaxation"]
        assert (label.state, label.label) == (LabelState.LABELLED, "Помогает расслабиться")

    def test_a_key_cannot_carry_a_second_text(self, templates, curator) -> None:
        """Ключ уникален: вторую запись с тем же ключом база не даёт завести, поэтому двух
        разных формулировок у ключа не бывает и ``ambiguous`` при словаре недостижимо."""
        _capability(templates[0], "relaxation", "Помогает расслабиться", curator)
        with pytest.raises(IntegrityError), transaction.atomic():
            _capability(templates[1], "relaxation", "Снимает стресс", curator)
        label = capability_labels(["relaxation"])["relaxation"]
        assert (label.state, label.label) == (LabelState.LABELLED, "Помогает расслабиться")

    @pytest.mark.parametrize(
        "override",
        [
            {"status": ClaimEvidence.Status.SYSTEM_INFERENCE, "confirmed_by": None, "confirmed_at": None},
            {"claim_scope": ClaimEvidence.ClaimScope.NOT_SUPPORTED},
            {"valid_until": "PAST"},
        ],
    )
    def test_an_unconfirmed_unsupported_or_expired_text_is_not_a_label(self, templates, curator, override) -> None:
        override = {k: (timezone.now() - timedelta(days=1) if v == "PAST" else v) for k, v in override.items()}
        _capability(templates[0], "draft", "Черновая формулировка", curator, **override)
        label = capability_labels(["draft"])["draft"]
        assert (label.state, label.label) == (LabelState.UNKNOWN, None)

    def test_ambiguous_stays_declared_on_the_wire_though_nothing_produces_it(self) -> None:
        """Объявлено, но при общем словаре недостижимо: экран строится на четырёх состояниях,
        и значение не должно исчезнуть из провода молча."""
        assert {state.value for state in LabelState} == {"labelled", "unknown", "no_text", "ambiguous"}

    def test_a_confirmed_capability_without_text_says_so(self, templates, curator) -> None:
        _capability(templates[0], "silent", "", curator)
        assert capability_labels(["silent"])["silent"].state is LabelState.NO_TEXT

    def test_every_asked_key_gets_an_answer_once(self, templates, curator) -> None:
        _capability(templates[0], "known", "Известная", curator)
        out = capability_labels(["known", "missing", "known"])
        assert list(out) == ["known", "missing"]
        assert out["missing"].state is LabelState.UNKNOWN


class TestEndpoint:
    def test_labels_come_back_by_key_with_a_named_state(self, owner, templates, curator) -> None:
        _capability(templates[0], "tension-relief", "Снимает мышечное напряжение", curator)
        _capability(list(templates), "relaxation", "Помогает расслабиться", curator)
        _capability(templates[1], "silent", "", curator)
        resp = _api().post(URL, {"keys": ["tension-relief", "relaxation", "silent", "nope"]}, format="json")
        assert resp.status_code == 200, resp.content
        assert resp.json()["data"] == {
            "labels": {
                "tension-relief": {"state": "labelled", "label": "Снимает мышечное напряжение"},
                "relaxation": {"state": "labelled", "label": "Помогает расслабиться"},
                "silent": {"state": "no_text", "label": None},
                "nope": {"state": "unknown", "label": None},
            }
        }

    @pytest.mark.parametrize("keys", [None, "tension-relief", [], [""], [1], ["k"] * 51])
    def test_malformed_keys_are_refused(self, owner, keys) -> None:
        resp = _api().post(URL, {"keys": keys}, format="json")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_engine_off_is_404(self, owner, settings) -> None:
        settings.PLAN_ENGINE_ENABLED = False
        resp = _api().post(URL, {"keys": ["tension-relief"]}, format="json")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "PLAN_ENGINE_DISABLED"
