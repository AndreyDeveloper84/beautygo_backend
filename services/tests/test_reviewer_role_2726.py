"""DRF-2726 (блок A) — «рецензент проверил факт» отдельно от «куратор утвердил использование».

Решение владельца 02.10: один ``confirmed_by`` не несёт роли, компетенции и
типа утверждения; медицинские и физиологические утверждения — «только
назначенным рецензентом подходящей квалификации».

Что держат узлы:

* у утверждения есть тип; подтверждённая строка без типа не хранится;
* тип, требующий рецензента, подтверждается только с отметкой проверки —
  база и форма;
* отметку ставит только назначенный рецензент под тип и область утверждения;
  право администратора и право подтверждения компетенцией не являются;
* проверка и подтверждение — две подписи, и они могут быть разных людей;
* правка содержания снимает отметку проверки, и подтверждённой строка после
  этого остаться не может;
* подтвердить утверждение БЕЗ рецензента (тип ``product``) может только
  держатель права продуктовых границ: тип объявляет тот, кого он ограничивает,
  и обычный куратор не может переименовать медицинское в продуктовое;
* правка содержания возможности снимает проверку у её связей с целями;
* из файла отметка проверки не приезжает;
* строки, подтверждённые ДО правила, миграция возвращает в черновик — на
  настоящей миграции.

Умолчания, а не решения владельца (названы в PR): список типов; «рецензент
нужен всем типам, кроме ``product``»; один человек может и проверить, и
подтвердить. Все тексты синтетические.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from services import knowledge_intake
from services.knowledge_review import may_review, requires_review
from services.models import (
    CapabilityGoalLink,
    ClaimEvidence,
    ClaimReviewer,
    GoalOption,
    ProcedureCapability,
    ServiceCategory,
    ServiceTemplate,
)
from users.models import User

pytestmark = pytest.mark.django_db

ADD = "admin:services_procedurecapability_add"
CHANGE = "admin:services_procedurecapability_change"
ADD_LINK = "admin:services_capabilitygoallink_add"
ADD_REVIEWER = "admin:services_claimreviewer_add"

EVERYDAY = [
    f"{action}_{model}"
    for model in ("procedurecapability", "capabilitygoallink")
    for action in ("add", "change", "delete", "view")
]
APPROVE = ["approve_procedurecapability", "approve_capabilitygoallink"]
BOUNDARY = ["approve_claim_without_reviewer"]


def _staff(username: str, codenames: list[str]) -> User:
    user = User.objects.create_user(
        username=username, password="pw", role="admin", is_staff=True,  # pragma: allowlist secret
    )
    found = Permission.objects.filter(content_type__app_label="services", codename__in=codenames)
    assert found.count() == len(codenames)
    user.user_permissions.set(found)
    return user


def _client(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def face() -> ServiceCategory:
    return ServiceCategory.objects.create(name="Лицо 2726-р")


@pytest.fixture
def body() -> ServiceCategory:
    return ServiceCategory.objects.create(name="Тело 2726-р")


@pytest.fixture
def template(face) -> ServiceTemplate:
    """Процедура в ПОДкатегории «лица» — область рецензента покрывает и её."""
    peels = ServiceCategory.objects.create(name="Пилинги 2726-р", parent=face)
    return ServiceTemplate.objects.create(
        category=peels, name="Процедура 2726-р", name_short="Процедура 2726-р", canonical_code="1.1.3",
    )


@pytest.fixture
def goal() -> GoalOption:
    return GoalOption.objects.create(key="relax", label="Цель 2726-р")


@pytest.fixture
def approver() -> User:
    """Куратор: право подтверждения есть; назначения рецензентом и права
    продуктовых границ нет."""
    return _staff("approver-2726-r", EVERYDAY + APPROVE)


@pytest.fixture
def boundary_holder() -> User:
    """Держатель продуктовых границ: вправе подтвердить без рецензента."""
    return _staff("boundary-2726-r", EVERYDAY + APPROVE + BOUNDARY)


@pytest.fixture
def reviewer() -> User:
    """Рецензент медицинских утверждений в любой области; права подтверждения нет."""
    user = _staff("reviewer-2726-r", EVERYDAY)
    ClaimReviewer.objects.create(user=user, claim_type="medical")
    return user


def _form(template: ServiceTemplate, **overrides) -> dict:
    data = {
        "template": str(template.pk),
        "key": "example_effect",
        "text_client": "Синтетическая формулировка",
        "text_professional": "",
        "expected_effect": "",
        "result_timeframe": "",
        "variability_note": "",
        "claim_type": "medical",
        "status": "system_inference",
        "claim_scope": "supported",
        "prohibited_statement": "",
        "limitations": "",
        "evidence_source": "",
        "evidence_kind": "",
        "source_ref": "DOC-2726",
    }
    data.update(overrides)
    return data


def _errors(response) -> dict[str, list[str]]:
    """``{поле: [коды ошибок]}`` возвращённой формы."""
    assert response.status_code == 200  # форма возвращена, а не сохранена
    data = response.context["adminform"].form.errors.as_data()
    return {field: [e.code for e in errs] for field, errs in data.items()}


def _signed(owner: User) -> dict:
    return {"status": "approved", "confirmed_by": owner, "confirmed_at": timezone.now(), "source_ref": "DOC-2726"}


def _refused_by(constraint: str, create) -> None:
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        create()
    assert constraint in str(raised.value)


# ── База ───────────────────────────────────────────────────────────────────


class TestTheDatabaseKeepsTheTwoSignaturesApart:
    def test_an_approved_product_claim_needs_no_reviewer(self, template, approver) -> None:
        """Контроль: подтверждённая строка вообще сохраняется."""
        ProcedureCapability.objects.create(
            template=template, key="product", claim_type="product", **_signed(approver),
        )

        assert ProcedureCapability.objects.get().reviewed_by_id is None

    def test_an_approved_claim_without_a_type_is_refused(self, template, approver) -> None:
        _refused_by(
            "procedurecapability_approved_has_claim_type",
            lambda: ProcedureCapability.objects.create(template=template, key="untyped", **_signed(approver)),
        )

    @pytest.mark.parametrize("claim_type", ["professional", "physiological", "medical"])
    def test_an_approved_claim_of_a_reviewed_type_needs_the_review(
        self, template, approver, reviewer, claim_type
    ) -> None:
        _refused_by(
            "procedurecapability_approved_review_when_required",
            lambda: ProcedureCapability.objects.create(
                template=template, key="unreviewed", claim_type=claim_type, **_signed(approver),
            ),
        )

        ProcedureCapability.objects.create(
            template=template, key="reviewed", claim_type=claim_type,
            reviewed_by=reviewer, reviewed_at=timezone.now(), **_signed(approver),
        )
        row = ProcedureCapability.objects.get()
        assert (row.confirmed_by_id, row.reviewed_by_id) == (approver.pk, reviewer.pk)

    def test_the_literal_set_of_reviewed_types(self) -> None:
        """Умолчание листа литералом: рецензент нужен всем типам, кроме ``product``.
        Смена набора — решение владельца, и этот узел меняется вместе с ним."""
        assert ClaimEvidence.REVIEW_REQUIRED_CLAIM_TYPES == ("professional", "physiological", "medical")
        assert {c.value for c in ClaimEvidence.ClaimType} == {
            "unclassified", "product", "professional", "physiological", "medical",
        }
        assert not requires_review("product")
        assert not requires_review("unclassified")

    def test_a_draft_is_not_judged(self, template) -> None:
        ProcedureCapability.objects.create(template=template, key="draft", claim_type="medical")
        ProcedureCapability.objects.create(template=template, key="untyped-draft")

        assert ProcedureCapability.objects.count() == 2

    def test_half_a_review_is_refused(self, template, reviewer) -> None:
        _refused_by(
            "procedurecapability_review_is_whole",
            lambda: ProcedureCapability.objects.create(
                template=template, key="half", claim_type="medical", reviewed_by=reviewer,
            ),
        )

    def test_the_goal_link_carries_the_same_rules(self, template, goal, approver) -> None:
        capability = ProcedureCapability.objects.create(template=template, key="for-link")

        _refused_by(
            "capabilitygoallink_approved_has_claim_type",
            lambda: CapabilityGoalLink.objects.create(capability=capability, goal=goal, **_signed(approver)),
        )
        _refused_by(
            "capabilitygoallink_approved_review_when_required",
            lambda: CapabilityGoalLink.objects.create(
                capability=capability, goal=goal, claim_type="medical", **_signed(approver),
            ),
        )


class TestAnAppointmentIsForAReviewedType:
    def test_nobody_is_appointed_to_a_type_that_needs_no_reviewer(self, approver) -> None:
        _refused_by(
            "claimreviewer_type_is_reviewable",
            lambda: ClaimReviewer.objects.create(user=approver, claim_type="product"),
        )

    def test_the_same_appointment_is_not_stored_twice(self, reviewer, face) -> None:
        ClaimReviewer.objects.create(user=reviewer, claim_type="physiological", category=face)

        _refused_by(
            "claimreviewer_user_type_any_category_uniq",
            lambda: ClaimReviewer.objects.create(user=reviewer, claim_type="medical"),
        )
        _refused_by(
            "claimreviewer_user_type_category_uniq",
            lambda: ClaimReviewer.objects.create(user=reviewer, claim_type="physiological", category=face),
        )

    def test_the_area_is_a_root_category(self, reviewer, template) -> None:
        appointment = ClaimReviewer(user=reviewer, claim_type="physiological", category=template.category)

        with pytest.raises(ValidationError) as raised:
            appointment.full_clean()

        assert "category" in raised.value.message_dict


# ── Компетенция ────────────────────────────────────────────────────────────


class TestWhoMayReview:
    def test_the_appointed_reviewer_may(self, reviewer, template) -> None:
        assert may_review(reviewer, claim_type="medical", template=template)

    def test_another_type_is_not_covered(self, reviewer, template) -> None:
        assert not may_review(reviewer, claim_type="physiological", template=template)

    def test_an_administrator_is_not_a_reviewer(self, template) -> None:
        """Право администратора — не компетенция: суперпользователь без назначения
        проверить не может (блок A: владелец — не единственный источник медфактов)."""
        superuser = User.objects.create_superuser(
            username="super-2726-r", password="pw",  # pragma: allowlist secret
            email="super-2726-r@example.test", role="admin",
        )

        assert not may_review(superuser, claim_type="medical", template=template)

    def test_the_right_to_approve_is_not_a_competence(self, approver, template) -> None:
        assert not may_review(approver, claim_type="medical", template=template)

    def test_a_withdrawn_appointment_and_an_inactive_account_do_not_count(self, reviewer, template) -> None:
        ClaimReviewer.objects.filter(user=reviewer).update(is_active=False)
        withdrawn = may_review(reviewer, claim_type="medical", template=template)

        ClaimReviewer.objects.filter(user=reviewer).update(is_active=True)
        reviewer.is_active = False
        inactive = may_review(reviewer, claim_type="medical", template=template)

        assert (withdrawn, inactive) == (False, False)

    def test_nobody_and_no_reviewed_type(self, reviewer, template) -> None:
        assert not may_review(None, claim_type="medical", template=template)
        assert not may_review(reviewer, claim_type="product", template=template)

    def test_an_area_covers_its_subcategories_and_nothing_else(self, template, face, body) -> None:
        """Пример владельца: «тело/массаж и кожа лица — возможно, разные люди»."""
        face_reviewer = _staff("face-2726-r", EVERYDAY)
        body_reviewer = _staff("body-2726-r", EVERYDAY)
        ClaimReviewer.objects.create(user=face_reviewer, claim_type="physiological", category=face)
        ClaimReviewer.objects.create(user=body_reviewer, claim_type="physiological", category=body)

        # ``template`` лежит в подкатегории «лица».
        assert may_review(face_reviewer, claim_type="physiological", template=template)
        assert not may_review(body_reviewer, claim_type="physiological", template=template)


# ── Форма админки ──────────────────────────────────────────────────────────


class TestTheAdminKeepsTheTwoSignaturesApart:
    def test_a_product_claim_is_approved_only_by_the_holder_of_the_product_boundary(
        self, approver, boundary_holder, template
    ) -> None:
        """Тип объявляет тот, кого он ограничивает: «рецензент не нужен» — решение
        держателя продуктовых границ, а не любого с правом подтверждения."""
        data = _form(template, claim_type="product", status="approved")

        refused = _client(approver).post(reverse(ADD), data)
        accepted = _client(boundary_holder).post(reverse(ADD), data)

        assert _errors(refused) == {"claim_type": ["product_boundary_right_required"]}
        assert accepted.status_code == 302, accepted.context["adminform"].form.errors
        row = ProcedureCapability.objects.get()
        assert (row.status, row.confirmed_by_id, row.reviewed_by_id) == ("approved", boundary_holder.pk, None)

    def test_a_product_draft_needs_no_right_at_all(self, approver, template) -> None:
        """Контроль: отказ выше вызван подтверждением, а не типом."""
        response = _client(approver).post(reverse(ADD), _form(template, claim_type="product"))

        assert response.status_code == 302, response.context["adminform"].form.errors

    def test_a_medical_claim_is_not_approved_without_a_review(self, approver, template) -> None:
        response = _client(approver).post(reverse(ADD), _form(template, status="approved"))

        assert _errors(response) == {"status": ["review_required"]}
        assert ProcedureCapability.objects.count() == 0

    def test_a_claim_without_a_type_is_not_approved(self, approver, template) -> None:
        response = _client(approver).post(reverse(ADD), _form(template, claim_type="unclassified", status="approved"))

        assert _errors(response) == {"claim_type": ["claim_type_required"]}

    def test_the_curator_cannot_tick_the_review_for_the_reviewer(self, approver, template) -> None:
        response = _client(approver).post(reverse(ADD), _form(template, status="approved", mark_reviewed="on"))

        errors = _errors(response)
        assert errors["mark_reviewed"] == ["reviewer_competence_required"]
        assert errors["status"] == ["review_required"]
        assert ProcedureCapability.objects.count() == 0

    def test_a_superuser_without_an_appointment_cannot_tick_it_either(self, template) -> None:
        superuser = User.objects.create_superuser(
            username="super-2726-r", password="pw",  # pragma: allowlist secret
            email="super-2726-r@example.test", role="admin",
        )

        response = _client(superuser).post(reverse(ADD), _form(template, mark_reviewed="on"))

        assert _errors(response) == {"mark_reviewed": ["reviewer_competence_required"]}

    def test_a_review_of_a_type_that_needs_none_is_refused(self, reviewer, template) -> None:
        response = _client(reviewer).post(reverse(ADD), _form(template, claim_type="product", mark_reviewed="on"))

        assert _errors(response) == {"mark_reviewed": ["review_not_applicable"]}

    def test_the_reviewer_reviews_and_the_curator_approves__two_people(
        self, reviewer, approver, template
    ) -> None:
        reviewed = _client(reviewer).post(reverse(ADD), _form(template, mark_reviewed="on"))
        assert reviewed.status_code == 302, reviewed.context["adminform"].form.errors
        row = ProcedureCapability.objects.get()
        assert (row.status, row.reviewed_by_id, row.confirmed_by_id) == ("system_inference", reviewer.pk, None)
        stamped_at = row.reviewed_at

        approved = _client(approver).post(reverse(CHANGE, args=[row.pk]), _form(template, status="approved"))

        assert approved.status_code == 302, approved.context["adminform"].form.errors
        row.refresh_from_db()
        assert (row.status, row.reviewed_by_id, row.confirmed_by_id) == ("approved", reviewer.pk, approver.pk)
        assert row.reviewed_at == stamped_at  # подтверждение проверку не переписывает

    def test_the_reviewer_cannot_approve(self, reviewer, template) -> None:
        """Назначение рецензентом — не право подтверждения (DRF-2726 п.2)."""
        response = _client(reviewer).post(reverse(ADD), _form(template, status="approved", mark_reviewed="on"))

        assert _errors(response) == {"status": ["approval_right_required"]}

    def test_one_person_holding_both_may_do_both(self, template) -> None:
        """Умолчание листа: запрета «четырёх глаз» нет — владелец может оказаться
        и куратором, и рецензентом в своей области."""
        both = _staff("both-2726-r", EVERYDAY + APPROVE)
        ClaimReviewer.objects.create(user=both, claim_type="medical")

        response = _client(both).post(reverse(ADD), _form(template, status="approved", mark_reviewed="on"))

        assert response.status_code == 302, response.context["adminform"].form.errors
        row = ProcedureCapability.objects.get()
        assert (row.status, row.reviewed_by_id, row.confirmed_by_id) == ("approved", both.pk, both.pk)

    def test_the_area_of_the_appointment_is_enforced_in_the_form(self, template, face, body) -> None:
        face_reviewer = _staff("face-2726-r", EVERYDAY)
        body_reviewer = _staff("body-2726-r", EVERYDAY)
        ClaimReviewer.objects.create(user=face_reviewer, claim_type="physiological", category=face)
        ClaimReviewer.objects.create(user=body_reviewer, claim_type="physiological", category=body)
        data = _form(template, claim_type="physiological", mark_reviewed="on")

        refused = _client(body_reviewer).post(reverse(ADD), data)
        accepted = _client(face_reviewer).post(reverse(ADD), data)

        assert _errors(refused) == {"mark_reviewed": ["reviewer_competence_required"]}
        assert accepted.status_code == 302, accepted.context["adminform"].form.errors
        assert ProcedureCapability.objects.get().reviewed_by_id == face_reviewer.pk

    def test_a_goal_link_is_reviewed_within_the_area_of_its_procedure(self, template, goal, face, body) -> None:
        """Область связи — область процедуры её возможности. Оба рецензента
        ограничены областью: без процедуры форма не пустила бы ни одного."""
        face_reviewer = _staff("face-2726-r", EVERYDAY)
        body_reviewer = _staff("body-2726-r", EVERYDAY)
        ClaimReviewer.objects.create(user=face_reviewer, claim_type="medical", category=face)
        ClaimReviewer.objects.create(user=body_reviewer, claim_type="medical", category=body)
        capability = ProcedureCapability.objects.create(template=template, key="for-link")
        data = {
            "capability": str(capability.pk), "goal": str(goal.pk),
            "course_pattern": "", "result_horizon": "", "variability_note": "",
            "claim_type": "medical", "mark_reviewed": "on",
            "status": "system_inference", "claim_scope": "supported", "prohibited_statement": "",
            "limitations": "", "evidence_source": "", "evidence_kind": "", "source_ref": "DOC-2726",
        }

        refused = _client(body_reviewer).post(reverse(ADD_LINK), data)
        accepted = _client(face_reviewer).post(reverse(ADD_LINK), data)

        assert _errors(refused) == {"mark_reviewed": ["reviewer_competence_required"]}
        assert accepted.status_code == 302, accepted.context["adminform"].form.errors
        assert CapabilityGoalLink.objects.get().reviewed_by_id == face_reviewer.pk


class TestAnEditAfterTheReview:
    @pytest.fixture
    def reviewed_and_approved(self, template, reviewer, approver) -> ProcedureCapability:
        return ProcedureCapability.objects.create(
            template=template, key="example_effect", text_client="Синтетическая формулировка",
            claim_type="medical", claim_scope="supported",
            reviewed_by=reviewer, reviewed_at=timezone.now(), **_signed(approver),
        )

    def test_a_new_wording_cannot_stay_approved_under_the_old_review(
        self, approver, template, reviewed_and_approved, reviewer
    ) -> None:
        response = _client(approver).post(
            reverse(CHANGE, args=[reviewed_and_approved.pk]),
            _form(template, status="approved", text_client="Новая формулировка"),
        )

        assert _errors(response) == {"status": ["review_required"]}
        reviewed_and_approved.refresh_from_db()
        assert (reviewed_and_approved.text_client, reviewed_and_approved.reviewed_by_id) == (
            "Синтетическая формулировка", reviewer.pk,
        )

    def test_a_new_wording_returned_to_draft_loses_the_review_and_says_so(
        self, approver, template, reviewed_and_approved
    ) -> None:
        response = _client(approver).post(
            reverse(CHANGE, args=[reviewed_and_approved.pk]),
            _form(template, status="system_inference", text_client="Новая формулировка"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        reviewed_and_approved.refresh_from_db()
        row = reviewed_and_approved
        assert (row.status, row.reviewed_by_id, row.reviewed_at) == ("system_inference", None, None)
        assert any("Отметка проверки рецензентом снята" in str(m) for m in get_messages(response.wsgi_request))

    def test_the_term_of_validity_is_not_content(self, approver, template, reviewed_and_approved, reviewer) -> None:
        """Срок годности подтверждения — решение куратора: проверку не снимает."""
        response = _client(approver).post(
            reverse(CHANGE, args=[reviewed_and_approved.pk]),
            _form(template, status="approved", valid_until_0="2030-01-01", valid_until_1="00:00:00"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        reviewed_and_approved.refresh_from_db()
        assert (reviewed_and_approved.status, reviewed_and_approved.reviewed_by_id) == ("approved", reviewer.pk)
        assert reviewed_and_approved.valid_until is not None

    def test_the_curator_cannot_relabel_a_medical_claim_as_product_and_keep_it_approved(
        self, approver, template, reviewed_and_approved, reviewer
    ) -> None:
        """Обход, найденный ревью: сменить тип на тот, которому рецензент не
        нужен, и оставить «подтверждено»."""
        response = _client(approver).post(
            reverse(CHANGE, args=[reviewed_and_approved.pk]),
            _form(template, status="approved", claim_type="product"),
        )

        assert _errors(response) == {"claim_type": ["product_boundary_right_required"]}
        row = reviewed_and_approved
        row.refresh_from_db()
        assert (row.claim_type, row.status, row.reviewed_by_id) == ("medical", "approved", reviewer.pk)

    def test_the_holder_of_the_product_boundary_may_relabel__and_signs_it(
        self, boundary_holder, template, reviewed_and_approved
    ) -> None:
        """Предел, литералом: держатель продуктовых границ может объявить
        продуктовым и проверенное медицинское. Отметка рецензента при этом
        снимается, а решение подписано его именем."""
        response = _client(boundary_holder).post(
            reverse(CHANGE, args=[reviewed_and_approved.pk]),
            _form(template, status="approved", claim_type="product"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        row = reviewed_and_approved
        row.refresh_from_db()
        assert (row.claim_type, row.status, row.reviewed_by_id, row.confirmed_by_id) == (
            "product", "approved", None, boundary_holder.pk,
        )

    def test_saving_untouched_keeps_both_signatures(
        self, approver, template, reviewed_and_approved, reviewer
    ) -> None:
        row = reviewed_and_approved
        before = (row.reviewed_at, row.confirmed_at, row.updated_at)

        response = _client(approver).post(reverse(CHANGE, args=[row.pk]), _form(template, status="approved"))

        assert response.status_code == 302, response.context["adminform"].form.errors
        row.refresh_from_db()
        assert (row.reviewed_at, row.confirmed_at, row.updated_at) == before
        assert (row.reviewed_by_id, row.confirmed_by_id) == (reviewer.pk, approver.pk)


class TestAnEditOfTheCapabilityUnderAReviewedLink:
    """Связь с целью проверена как утверждение об ЭТОЙ возможности."""

    @pytest.fixture
    def capability(self, template) -> ProcedureCapability:
        return ProcedureCapability.objects.create(
            template=template, key="example_effect", text_client="Синтетическая формулировка",
            claim_type="product", claim_scope="supported", source_ref="DOC-2726",
        )

    @pytest.fixture
    def approved_link(self, capability, goal, reviewer, approver) -> CapabilityGoalLink:
        return CapabilityGoalLink.objects.create(
            capability=capability, goal=goal, claim_type="medical", claim_scope="supported",
            reviewed_by=reviewer, reviewed_at=timezone.now(), **_signed(approver),
        )

    def test_a_new_wording_of_the_capability_returns_its_reviewed_link_to_draft(
        self, approver, template, capability, approved_link, goal, reviewer
    ) -> None:
        other = GoalOption.objects.create(key="other-2726-r", label="Другая цель")
        reviewed_draft = CapabilityGoalLink.objects.create(
            capability=capability, goal=other, claim_type="medical",
            reviewed_by=reviewer, reviewed_at=timezone.now(),
        )

        response = _client(approver).post(
            reverse(CHANGE, args=[capability.pk]),
            _form(template, claim_type="product", text_client="Новая формулировка"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        approved_link.refresh_from_db()
        reviewed_draft.refresh_from_db()
        assert (approved_link.status, approved_link.reviewed_by_id, approved_link.reviewed_at) == (
            "system_inference", None, None,
        )
        assert approved_link.confirmed_by_id == approver.pk  # след подтверждения остаётся
        assert (reviewed_draft.status, reviewed_draft.reviewed_by_id) == ("system_inference", None)
        told = [str(m) for m in get_messages(response.wsgi_request)]
        assert any("снята проверка рецензентом — 2; из них возвращено в черновик — 1" in m for m in told)

    def test_without_the_right_to_change_approved_links_the_edit_is_refused(
        self, template, capability, approved_link, reviewer
    ) -> None:
        """Иначе правка возможности была бы обходом права подтверждения связей."""
        editor = _staff("editor-2726-r", EVERYDAY)

        response = _client(editor).post(
            reverse(CHANGE, args=[capability.pk]),
            _form(template, claim_type="product", text_client="Новая формулировка"),
        )

        assert _errors(response) == {"__all__": ["linked_review_would_be_dropped"]}
        approved_link.refresh_from_db()
        capability.refresh_from_db()
        assert (approved_link.status, approved_link.reviewed_by_id) == ("approved", reviewer.pk)
        assert capability.text_client == "Синтетическая формулировка"

    def test_a_change_that_is_not_content_leaves_the_link_alone(
        self, boundary_holder, template, capability, approved_link, reviewer
    ) -> None:
        """Контроль: связь трогает только правка содержания — подтверждение
        самой возможности её не касается."""
        response = _client(boundary_holder).post(
            reverse(CHANGE, args=[capability.pk]), _form(template, claim_type="product", status="approved"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        approved_link.refresh_from_db()
        assert (approved_link.status, approved_link.reviewed_by_id) == ("approved", reviewer.pk)


class TestAppointingAReviewerInTheAdmin:
    @pytest.fixture
    def owner_client(self) -> Client:
        owner = User.objects.create_superuser(
            username="owner-2726-r", password="pw",  # pragma: allowlist secret
            email="owner-2726-r@example.test", role="admin",
        )
        return _client(owner)

    def test_the_owner_appoints_a_reviewer_for_an_area(self, owner_client, approver, face) -> None:
        response = owner_client.post(reverse(ADD_REVIEWER), {
            "user": str(approver.pk), "claim_type": "medical", "category": str(face.pk), "is_active": "on",
        })

        assert response.status_code == 302, response.context["adminform"].form.errors
        appointment = ClaimReviewer.objects.get()
        assert (appointment.user_id, appointment.claim_type, appointment.category_id) == (
            approver.pk, "medical", face.pk,
        )

    def test_the_one_who_appoints_may_appoint_themselves__a_named_limit(self, owner_client, template) -> None:
        """Предел, литералом: самоназначение не запрещено — владелец может быть
        единственным рецензентом в своей области. «Суперпользователь не
        рецензент» верно, только пока он не назначил себя; след — в журнале."""
        owner = User.objects.get(username="owner-2726-r")
        before = may_review(owner, claim_type="medical", template=template)

        response = owner_client.post(reverse(ADD_REVIEWER), {
            "user": str(owner.pk), "claim_type": "medical", "is_active": "on",
        })

        assert response.status_code == 302, response.context["adminform"].form.errors
        assert (before, may_review(owner, claim_type="medical", template=template)) == (False, True)

    def test_a_type_that_needs_no_reviewer_is_not_offered(self, owner_client, approver) -> None:
        response = owner_client.post(reverse(ADD_REVIEWER), {
            "user": str(approver.pk), "claim_type": "product", "is_active": "on",
        })

        assert response.status_code == 200
        assert "claim_type" in response.context["adminform"].form.errors
        assert ClaimReviewer.objects.count() == 0

    def test_a_subcategory_is_not_offered_as_an_area(self, owner_client, approver, template) -> None:
        response = owner_client.post(reverse(ADD_REVIEWER), {
            "user": str(approver.pk), "claim_type": "medical",
            "category": str(template.category_id), "is_active": "on",
        })

        assert response.status_code == 200
        assert "category" in response.context["adminform"].form.errors
        assert ClaimReviewer.objects.count() == 0


# ── Засев из файла ─────────────────────────────────────────────────────────


def _seed(tmp_path: Path, capabilities: list) -> str:
    path = tmp_path / "knowledge.json"
    path.write_text(json.dumps({"capabilities": capabilities}, ensure_ascii=False), encoding="utf-8")
    out = StringIO()
    call_command("seed_procedure_knowledge", "--file", str(path), stdout=out)
    return out.getvalue()


def _refusal(tmp_path: Path, capabilities: list) -> str:
    with pytest.raises(CommandError) as raised:
        _seed(tmp_path, capabilities)
    assert ProcedureCapability.objects.count() == 0
    return str(raised.value)


APPROVED_BY_RULE = {
    "template_code": "1.1.3", "key": "signed", "status": "approved", "claim_scope": "supported",
    "source_ref": "DOC-2726", "confirmed_rule": "owner_rule_2726", "rule_version": "1",
    "confirmed_at": "2026-10-02T09:00:00+03:00",
}


class TestAFileCarriesTheTypeButNotTheReview:
    def test_the_type_is_seeded_with_the_draft(self, template, tmp_path) -> None:
        _seed(tmp_path, [{"template_code": "1.1.3", "key": "typed", "claim_type": "physiological"}])

        row = ProcedureCapability.objects.get()
        assert (row.status, row.claim_type, row.reviewed_by_id) == ("system_inference", "physiological", None)

    def test_a_row_without_a_type_is_seeded_unclassified(self, template, tmp_path) -> None:
        _seed(tmp_path, [{"template_code": "1.1.3", "key": "untyped"}])

        assert ProcedureCapability.objects.get().claim_type == "unclassified"

    def test_an_unknown_type_rejects_the_file(self, template, tmp_path) -> None:
        assert "неизвестный claim_type «cosmic»" in _refusal(
            tmp_path, [{"template_code": "1.1.3", "key": "odd", "claim_type": "cosmic"}],
        )

    def test_a_review_typed_into_the_file_is_an_unknown_field(self, template, tmp_path, reviewer) -> None:
        message = _refusal(tmp_path, [{
            "template_code": "1.1.3", "key": "self-reviewed", "claim_type": "medical",
            "reviewed_by": "reviewer-2726-r", "reviewed_at": "2026-10-02T09:00:00+03:00",
        }])

        assert "неизвестное поле «reviewed_by»" in message
        assert "неизвестное поле «reviewed_at»" in message

    def test_even_a_known_rule_cannot_approve_a_reviewed_type_from_a_file(
        self, template, tmp_path, monkeypatch
    ) -> None:
        """Импорт — не проверка: будь у владельца правило подтверждения без
        человека, медицинское утверждение из файла оно всё равно не подтвердит."""
        monkeypatch.setattr(knowledge_intake, "KNOWN_CONFIRMATION_RULES", frozenset({"owner_rule_2726"}))

        product = _seed(tmp_path, [{**APPROVED_BY_RULE, "claim_type": "product"}])
        ProcedureCapability.objects.all().delete()
        medical = _refusal(tmp_path, [{**APPROVED_BY_RULE, "claim_type": "medical"}])
        untyped = _refusal(tmp_path, [APPROVED_BY_RULE])

        assert "+1 capabilities" in product  # контроль: то же правило продуктовое подтверждает
        assert "подтверждается только после проверки рецензентом" in medical
        assert "подтвердить можно только утверждение с указанным типом" in untyped


# ── Миграция поверх строк, лежавших до неё ─────────────────────────────────

BEFORE = "0034_drf2726_approval_right"
THE_MIGRATION = "0035_drf2726_reviewer_role"

postgres_only = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="MigrationExecutor с CHECK-ограничениями — только на Postgres, как в CI",
)


@postgres_only
@pytest.mark.django_db(transaction=True)
class TestTheMigrationOnRowsThatWereThereBefore:
    @pytest.fixture(autouse=True)
    def _restore_services_schema(self):
        """Вернуть ``services`` на лист графа: DDL здесь коммитится в тестовую базу.
        Сначала схема, потом строки — живые модели знают колонки листа графа."""
        yield
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("services"))
        CapabilityGoalLink.objects.all().delete()
        ProcedureCapability.objects.all().delete()

    @staticmethod
    def _migrate(target: str):
        executor = MigrationExecutor(connection)
        executor.migrate([("services", target)])
        return executor.loader.project_state(("services", target)).apps

    def test_everything_approved_becomes_a_draft_and_keeps_its_trace(self) -> None:
        old = self._migrate(BEFORE)
        Capability = old.get_model("services", "ProcedureCapability")
        Link = old.get_model("services", "CapabilityGoalLink")
        category = old.get_model("services", "ServiceCategory").objects.create(name="Категория 2726-рм")
        template = old.get_model("services", "ServiceTemplate").objects.create(
            category=category, name="Процедура 2726-рм", name_short="Процедура 2726-рм",
        )
        goal = old.get_model("services", "GoalOption").objects.create(key="relax-2726-r", label="Цель")
        user = User.objects.create_user(username="mig-2726-r", password="x", role="admin")
        stamp = timezone.now()
        signed = {
            "status": "approved", "confirmed_by_id": user.id, "confirmed_at": stamp, "source_ref": "DOC-2726",
        }
        approved = Capability.objects.create(
            template=template, key="approved", text_client="Синтетика", claim_scope="supported", **signed,
        ).pk
        draft = Capability.objects.create(template=template, key="draft").pk
        approved_link = Link.objects.create(
            capability_id=draft, goal=goal, claim_scope="supported", **signed,
        ).pk

        new = self._migrate(THE_MIGRATION)  # не падает — это и есть свойство выкладки
        ProcedureCapability = new.get_model("services", "ProcedureCapability")  # noqa: N806
        CapabilityGoalLink = new.get_model("services", "CapabilityGoalLink")  # noqa: N806

        def state(model, pk):
            row = model.objects.get(pk=pk)
            return row.status, row.claim_type, row.confirmed_by_id, row.reviewed_by_id

        assert state(ProcedureCapability, approved) == ("system_inference", "unclassified", user.id, None)
        assert state(ProcedureCapability, draft) == ("system_inference", "unclassified", None, None)
        assert state(CapabilityGoalLink, approved_link) == ("system_inference", "unclassified", user.id, None)
        demoted = ProcedureCapability.objects.get(pk=approved)
        assert (demoted.text_client, demoted.source_ref, demoted.confirmed_at) == ("Синтетика", "DOC-2726", stamp)
