"""DRF-2724 — внутренняя ручка чтения знания о процедуре (контракт DRF-2719).

``GET /api/v1/internal/knowledge/procedures/`` отдаёт боту только то, что о
процедуре можно сказать человеку. Свойства хранения держит
``test_procedure_capability_2606.py``; здесь — что они ДОЕЗЖАЮТ до провода,
и то, чего на уровне хранения нет: два независимых условия при входе от
услуги салона, форма ответа, что не покидает каталог.

Каждому «не отдаётся» рядом стоит «отдаётся» на тех же данных: иначе отказ
зеленел бы и на ручке, которая не отдаёт ничего. Ожидаемые ключи ответа
записаны литералом — узел, берущий их из кода ручки, не заметил бы её
правки. Данные синтетические.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from services.models import (
    CapabilityGoalLink,
    ClaimEvidence,
    GoalOption,
    ProcedureCapability,
    SalonService,
    ServiceCategory,
    ServiceTemplate,
)
from tenants.models import Tenant

pytestmark = pytest.mark.django_db

URL = "/api/v1/internal/knowledge/procedures/"
VALID_TOKEN = "test-ayla-internal-token-2724"

S = ClaimEvidence.Status
C = ClaimEvidence.ClaimScope
M = SalonService.MappingStatus

CLIENT_TEXT = "Выравнивает тон кожи"
PRO_TEXT = "Кератолитическое действие АНА-кислот"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def api() -> APIClient:
    client = APIClient()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    return client


@pytest.fixture
def category() -> ServiceCategory:
    return ServiceCategory.objects.create(name="Уход за лицом 2724", slug="face-2724")


@pytest.fixture
def template(category) -> ServiceTemplate:
    return ServiceTemplate.objects.create(
        category=category, name="Пилинг 2724", name_short="Пилинг", duration_default=45
    )


@pytest.fixture
def curator():
    return get_user_model().objects.create_user(username="curator-2724", password="x")


@pytest.fixture
def goal() -> GoalOption:
    return GoalOption.objects.create(key="skin-care-2724", label="Уход за кожей")


@pytest.fixture
def salon() -> Tenant:
    return Tenant.objects.create(slug="t2724", name="Салон 2724")


def _capability(template, *, key="even-tone", **kw) -> ProcedureCapability:
    fields = {
        "text_client": CLIENT_TEXT,
        "text_professional": PRO_TEXT,
        "expected_effect": "Кожа выглядит ровнее",
        "result_timeframe": "после 3–5 сеансов",
        "limitations": "Не проводят при активных высыпаниях",
        "evidence_source": "Протокол салона",
        "evidence_kind": "practice",
        **kw,
    }
    return ProcedureCapability.objects.create(template=template, key=key, **fields)


def _approve(row, curator, **extra) -> None:
    for name, value in {
        "status": S.APPROVED,
        "claim_scope": C.SUPPORTED,
        "confirmed_by": curator,
        "confirmed_at": timezone.now(),
        "source_ref": "owner-review-2724",
        **extra,
    }.items():
        setattr(row, name, value)
    row.save()


def _salon_service(salon, template, category, *, status, curator=None, name="Пилинг в салоне"):
    fields = {}
    # База требует основание у обоих РЕШЁННЫХ статусов: «подтверждена» и
    # «решено, что связи не будет».
    if status in (M.VERIFIED, M.NOT_RECOMMENDABLE):
        fields = {
            "mapping_confirmed_by": curator,
            "mapping_confirmed_at": timezone.now(),
            "mapping_source_ref": "owner-list-2724",
        }
    return SalonService.objects.create(
        tenant=salon, template=template, category=category, name=name, mapping_status=status, **fields
    )


def _read(api, **params) -> dict:
    response = api.get(URL, params)
    assert response.status_code == 200, response.content[:300]
    return response.json()["data"]


def _atoms(value):
    """Все ключи и значения ответа — чтобы искать утечку где угодно."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _atoms(v)
    elif isinstance(value, list):
        for v in value:
            yield from _atoms(v)
    else:
        yield value


# --- дверь ---------------------------------------------------------------


class TestTheDoor:
    def test_without_the_internal_token_nothing_is_read(self, template, curator) -> None:
        _approve(_capability(template), curator)

        assert APIClient().get(URL, {"template_id": str(template.id)}).status_code in (401, 403)
        stranger = APIClient()
        stranger.defaults["HTTP_AUTHORIZATION"] = "Bearer not-the-token"
        assert stranger.get(URL, {"template_id": str(template.id)}).status_code in (401, 403)

    def test_exactly_one_subject_is_required(self, api, template) -> None:
        assert api.get(URL).status_code == 400
        both = {"template_id": str(template.id), "salon_service_id": str(template.id)}
        assert api.get(URL, both).status_code == 400
        assert api.get(URL, {"template_id": "not-a-uuid"}).status_code == 400
        assert api.get(URL, {"salon_service_id": "not-a-uuid"}).status_code == 400


# --- форма ответа ----------------------------------------------------------


class TestTheShapeOnTheWire:
    def test_a_confirmed_capability_with_a_confirmed_goal_link(
        self, api, template, curator, goal
    ) -> None:
        capability = _capability(template)
        _approve(capability, curator)
        link = CapabilityGoalLink.objects.create(
            capability=capability,
            goal=goal,
            course_pattern="Обычно рассматривается как курс сеансов",
            variability_note="Зависит от исходного состояния кожи",
            result_horizon="в течение курса",
            evidence_source="Протокол салона",
            source_ref="owner-review-2724",
        )
        _approve(link, curator)

        data = _read(api, template_id=str(template.id))

        assert set(data) == {"state", "unknown_reason", "subject", "as_of", "claims"}
        assert data["state"] == "known"
        assert data["unknown_reason"] is None
        assert data["subject"] == {"template_id": str(template.id), "salon_service_id": None}
        (claim,) = data["claims"]
        assert set(claim) == {
            "claim_id",
            "kind",
            "key",
            "text_client",
            "expected_effect",
            "limitations",
            "provenance",
            "goal_links",
        }
        assert claim["claim_id"] == str(capability.id)
        assert claim["kind"] == "capability"
        assert claim["key"] == "even-tone"
        assert claim["text_client"] == "Выравнивает тон кожи"
        # Блок B решения 02.10: подтверждённый эффект отдаётся.
        assert claim["expected_effect"] == "Кожа выглядит ровнее"
        assert claim["limitations"] == "Не проводят при активных высыпаниях"
        assert set(claim["provenance"]) == {
            "confirmed_kind",
            "confirmed_rule",
            "rule_version",
            "confirmed_at",
            "source_ref",
            "evidence_source",
            "evidence_kind",
            "valid_until",
        }
        assert claim["provenance"]["confirmed_kind"] == "human"
        assert claim["provenance"]["source_ref"] == "owner-review-2724"
        assert claim["provenance"]["confirmed_at"] is not None
        assert claim["provenance"]["valid_until"] is None
        (wire_link,) = claim["goal_links"]
        assert set(wire_link) == {
            "claim_id",
            "kind",
            "goal_key",
            "course_pattern",
            "result_horizon",
            "variability_note",
            "limitations",
            "provenance",
        }
        assert wire_link["claim_id"] == str(link.id)
        assert wire_link["kind"] == "goal_link"
        assert wire_link["goal_key"] == "skin-care-2724"
        assert wire_link["course_pattern"] == "Обычно рассматривается как курс сеансов"
        assert wire_link["variability_note"] == "Зависит от исходного состояния кожи"
        assert wire_link["result_horizon"] == "в течение курса"

    def test_confirmation_by_a_named_rule_is_told_apart_from_a_person(self, api, template) -> None:
        capability = _capability(template)
        _approve(
            capability, None, confirmed_by=None, confirmed_rule="owner-table", rule_version="v1"
        )

        (claim,) = _read(api, template_id=str(template.id))["claims"]

        assert claim["provenance"]["confirmed_kind"] == "rule"
        assert claim["provenance"]["confirmed_rule"] == "owner-table"
        assert claim["provenance"]["rule_version"] == "v1"


# --- N1–N4: что не считается знанием ----------------------------------------


class TestUnknownIsSaidOutLoud:
    def test_n1_a_procedure_nobody_described_is_unknown_not_an_empty_answer(
        self, api, template
    ) -> None:
        data = _read(api, template_id=str(template.id))

        assert data["state"] == "unknown"
        assert data["unknown_reason"] == "no_confirmed_knowledge"
        assert data["claims"] == []
        assert data["subject"]["template_id"] == str(template.id)

    def test_n2_an_imported_or_inferred_row_is_not_knowledge_until_confirmed(
        self, api, template, curator
    ) -> None:
        capability = _capability(template)  # system_inference по умолчанию — как после импорта

        before = _read(api, template_id=str(template.id))
        _approve(capability, curator)
        after = _read(api, template_id=str(template.id))

        assert before["state"] == "unknown"
        assert CLIENT_TEXT not in list(_atoms(before))
        assert after["state"] == "known"
        assert [c["text_client"] for c in after["claims"]] == [CLIENT_TEXT]

    @pytest.mark.parametrize("scope", [C.PROHIBITED_CLAIM, C.NOT_SUPPORTED])
    def test_n3_a_confirmed_prohibited_or_unsupported_claim_never_leaves(
        self, api, template, curator, scope
    ) -> None:
        said = _capability(template, key="even-tone")
        _approve(said, curator)
        unsaid = _capability(
            template, key="wrinkles-permanent", text_client="Убирает морщины навсегда"
        )
        _approve(unsaid, curator, claim_scope=scope)

        data = _read(api, template_id=str(template.id))

        assert [c["key"] for c in data["claims"]] == ["even-tone"]
        atoms = [str(a) for a in _atoms(data)]
        assert "wrinkles-permanent" not in atoms
        assert "Убирает морщины навсегда" not in atoms

    def test_n4_an_expired_confirmation_is_not_said(self, api, template, curator) -> None:
        capability = _capability(template)
        _approve(capability, curator, valid_until=timezone.now() + timedelta(days=1))
        live = _read(api, template_id=str(template.id))

        ProcedureCapability.objects.filter(pk=capability.pk).update(
            valid_until=timezone.now() - timedelta(seconds=1)
        )
        expired = _read(api, template_id=str(template.id))

        assert live["state"] == "known"
        assert live["claims"][0]["provenance"]["valid_until"] is not None
        assert expired["state"] == "unknown"
        assert expired["claims"] == []


# --- N5, N6: два независимых условия (блок D) -------------------------------


class TestMappingAndClaimAreTwoConditions:
    @pytest.mark.parametrize("status", [M.REVIEW_REQUIRED, M.UNMAPPED, M.NOT_RECOMMENDABLE])
    def test_n5_confirmed_knowledge_is_not_said_of_a_service_nobody_matched(
        self, api, salon, template, category, curator, status
    ) -> None:
        _approve(_capability(template), curator)
        unmatched = _salon_service(salon, template, category, status=status, curator=curator)
        matched = _salon_service(
            salon, template, category, status=M.VERIFIED, curator=curator, name="Пилинг подтверждённый"
        )

        refused = _read(api, salon_service_id=str(unmatched.id))
        told = _read(api, salon_service_id=str(matched.id))

        assert refused["state"] == "unknown"
        assert refused["claims"] == []
        assert refused["subject"]["salon_service_id"] == str(unmatched.id)
        # Тот же шаблон, то же знание — связь подтверждена, и оно отдаётся.
        assert told["state"] == "known"
        assert [c["text_client"] for c in told["claims"]] == [CLIENT_TEXT]
        assert told["subject"] == {
            "template_id": str(template.id),
            "salon_service_id": str(matched.id),
        }

    def test_n6_a_confirmed_match_gives_no_knowledge_by_itself(
        self, api, salon, template, category, curator
    ) -> None:
        capability = _capability(template)  # не подтверждено
        matched = _salon_service(salon, template, category, status=M.VERIFIED, curator=curator)

        before = _read(api, salon_service_id=str(matched.id))
        _approve(capability, curator)
        after = _read(api, salon_service_id=str(matched.id))

        assert before["state"] == "unknown"
        assert before["claims"] == []
        assert after["state"] == "known"

    def test_a_salon_service_without_a_template_is_unknown(self, api, salon, category) -> None:
        own = SalonService.objects.create(tenant=salon, category=category, name="Своя услуга")

        data = _read(api, salon_service_id=str(own.id))

        assert data["state"] == "unknown"
        assert data["subject"] == {"template_id": None, "salon_service_id": str(own.id)}

    def test_the_wire_does_not_tell_why_the_service_is_unknown(
        self, api, salon, template, category, curator
    ) -> None:
        """Решение 11.09 (§1 п. 6): статус связи виден только в очереди проверки."""
        _approve(_capability(template), curator)
        in_review = _salon_service(salon, template, category, status=M.REVIEW_REQUIRED)
        described = _read(api, template_id=str(template.id))

        data = _read(api, salon_service_id=str(in_review.id))

        # Положительный контроль: знание у шаблона есть, отказ — именно из-за связи.
        assert described["state"] == "known"
        assert data["unknown_reason"] == "no_confirmed_knowledge"
        atoms = [str(a) for a in _atoms(data)]
        for leak in ("mapping_status", "review_required", "mapping_not_verified", "unmapped"):
            assert leak not in atoms


# --- N10, N11: связь с целью -------------------------------------------------


class TestGoalLinks:
    def test_n10_a_capability_without_a_confirmed_link_carries_no_course(
        self, api, template, curator, goal
    ) -> None:
        capability = _capability(template)
        _approve(capability, curator)
        link = CapabilityGoalLink.objects.create(
            capability=capability,
            goal=goal,
            course_pattern="Обычно рассматривается как курс сеансов",
            variability_note="Зависит от исходного состояния кожи",
            evidence_source="Протокол салона",
            source_ref="owner-review-2724",
        )

        before = _read(api, template_id=str(template.id))
        _approve(link, curator)
        after = _read(api, template_id=str(template.id))

        assert before["state"] == "known"
        assert before["claims"][0]["goal_links"] == []
        assert "Обычно рассматривается как курс сеансов" not in [str(a) for a in _atoms(before)]
        assert [link["goal_key"] for link in after["claims"][0]["goal_links"]] == ["skin-care-2724"]

    def test_n11_a_link_to_a_goal_taken_off_publication_is_not_said(
        self, api, template, curator, goal
    ) -> None:
        capability = _capability(template)
        _approve(capability, curator)
        link = CapabilityGoalLink.objects.create(capability=capability, goal=goal)
        _approve(link, curator)
        live = _read(api, template_id=str(template.id))

        GoalOption.objects.filter(pk=goal.pk).update(is_active=False)
        hidden = _read(api, template_id=str(template.id))

        assert len(live["claims"][0]["goal_links"]) == 1
        assert hidden["claims"][0]["goal_links"] == []

    def test_a_horizon_without_a_variability_note_is_withheld(
        self, api, template, curator, goal
    ) -> None:
        """Блок B: срок — не самостоятельное число без оговорки о разбросе."""
        capability = _capability(template)
        _approve(capability, curator)
        link = CapabilityGoalLink.objects.create(
            capability=capability, goal=goal, result_horizon="через месяц"
        )
        _approve(link, curator)
        bare = _read(api, template_id=str(template.id))

        CapabilityGoalLink.objects.filter(pk=link.pk).update(variability_note="У всех по-разному")
        qualified = _read(api, template_id=str(template.id))

        assert bare["claims"][0]["goal_links"][0]["result_horizon"] == ""
        assert "через месяц" not in [str(a) for a in _atoms(bare)]
        assert qualified["claims"][0]["goal_links"][0]["result_horizon"] == "через месяц"
        assert qualified["claims"][0]["goal_links"][0]["variability_note"] == "У всех по-разному"

    def test_goal_key_narrows_the_links_and_an_unknown_key_is_not_an_empty_answer(
        self, api, template, curator, goal
    ) -> None:
        other = GoalOption.objects.create(key="relax-2724", label="Расслабиться")
        capability = _capability(template)
        _approve(capability, curator)
        for target in (goal, other):
            _approve(CapabilityGoalLink.objects.create(capability=capability, goal=target), curator)

        everything = _read(api, template_id=str(template.id))
        narrowed = _read(api, template_id=str(template.id), goal_key="relax-2724")
        typo = api.get(URL, {"template_id": str(template.id), "goal_key": "relaks"})

        assert sorted(link["goal_key"] for link in everything["claims"][0]["goal_links"]) == [
            "relax-2724",
            "skin-care-2724",
        ]
        assert [link["goal_key"] for link in narrowed["claims"][0]["goal_links"]] == ["relax-2724"]
        assert typo.status_code == 404


# --- N12, N13 ---------------------------------------------------------------


class TestWhatNeverLeavesTheCatalog:
    def test_n12_the_professional_wording_the_person_and_the_bare_timeframe_stay_inside(
        self, api, template, curator
    ) -> None:
        _approve(_capability(template), curator)

        data = _read(api, template_id=str(template.id))

        atoms = [str(a) for a in _atoms(data)]
        # Положительный контроль: строка отдана, искать есть где.
        assert CLIENT_TEXT in atoms
        for key in ("text_professional", "confirmed_by", "result_timeframe", "status", "claim_scope"):
            assert key not in atoms
        assert PRO_TEXT not in atoms
        assert "curator-2724" not in atoms
        assert str(curator.pk) not in atoms
        # Срок возможности: поля для оговорки о разбросе у неё нет (блок B).
        assert "после 3–5 сеансов" not in atoms

    def test_n13_an_unknown_subject_is_not_found_rather_than_unknown(self, api) -> None:
        missing = "00000000-0000-4000-8000-000000002724"

        assert api.get(URL, {"template_id": missing}).status_code == 404
        assert api.get(URL, {"salon_service_id": missing}).status_code == 404


# --- инвариант ответа -------------------------------------------------------


@pytest.mark.parametrize("confirmed", [False, True])
def test_known_means_claims_and_unknown_means_none(api, template, curator, confirmed) -> None:
    capability = _capability(template)
    if confirmed:
        _approve(capability, curator)

    data = _read(api, template_id=str(template.id))

    assert (data["state"] == "known") == bool(data["claims"]) == confirmed
    assert (data["unknown_reason"] is None) == confirmed
    for claim in data["claims"]:
        assert claim["provenance"]["confirmed_at"]
        assert claim["provenance"]["source_ref"]
