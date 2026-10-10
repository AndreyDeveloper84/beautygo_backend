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

import datetime as dt
import json
import uuid
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
from services import models as services_models
from services.knowledge_review import may_review, requires_review
from services.models import (
    CapabilityGoalLink,
    ClaimApprovalReset,
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
        "procedures": [str(template.pk)],
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
        "evidence_kind": "professional_consensus",
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
    return {
        "status": "approved", "evidence_kind": "professional_consensus",
        "confirmed_by": owner, "confirmed_at": timezone.now(), "source_ref": "DOC-2726",
    }


def _refused_by(constraint: str, create) -> None:
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        create()
    assert constraint in str(raised.value)


# ── База ───────────────────────────────────────────────────────────────────


class TestTheDatabaseKeepsTheTwoSignaturesApart:
    def test_an_approved_product_claim_needs_no_reviewer(self, template, approver) -> None:
        """Контроль: подтверждённая строка вообще сохраняется."""
        ProcedureCapability.objects.create(
            templates=[template], key="product", claim_type="product", **_signed(approver),
        )

        assert ProcedureCapability.objects.get().reviewed_by_id is None

    def test_an_approved_claim_without_a_type_is_refused(self, template, approver) -> None:
        _refused_by(
            "procedurecapability_approved_has_claim_type",
            lambda: ProcedureCapability.objects.create(templates=[template], key="untyped", **_signed(approver)),
        )

    @pytest.mark.parametrize("claim_type", ["professional", "physiological", "medical"])
    def test_an_approved_claim_of_a_reviewed_type_needs_the_review(
        self, template, approver, reviewer, claim_type
    ) -> None:
        _refused_by(
            "procedurecapability_approved_review_when_required",
            lambda: ProcedureCapability.objects.create(
                templates=[template], key="unreviewed", claim_type=claim_type, **_signed(approver),
            ),
        )

        ProcedureCapability.objects.create(
            templates=[template], key="reviewed", claim_type=claim_type,
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
        ProcedureCapability.objects.create(templates=[template], key="draft", claim_type="medical")
        ProcedureCapability.objects.create(templates=[template], key="untyped-draft")

        assert ProcedureCapability.objects.count() == 2

    def test_half_a_review_is_refused(self, template, reviewer) -> None:
        _refused_by(
            "procedurecapability_review_is_whole",
            lambda: ProcedureCapability.objects.create(
                templates=[template], key="half", claim_type="medical", reviewed_by=reviewer,
            ),
        )

    def test_the_goal_link_carries_the_same_rules(self, template, goal, approver) -> None:
        capability = ProcedureCapability.objects.create(templates=[template], key="for-link")

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
        capability = ProcedureCapability.objects.create(templates=[template], key="for-link")
        data = {
            "capability": str(capability.pk), "goal": str(goal.pk),
            "course_pattern": "", "result_horizon": "", "variability_note": "",
            "claim_type": "medical", "mark_reviewed": "on",
            "status": "system_inference", "claim_scope": "supported", "prohibited_statement": "",
            "limitations": "", "evidence_source": "", "evidence_kind": "professional_consensus",
            "source_ref": "DOC-2726",
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
            templates=[template], key="example_effect", text_client="Синтетическая формулировка",
            claim_type="medical", claim_scope="supported",
            reviewed_by=reviewer, reviewed_at=timezone.now(), **_signed(approver),
        )

    def test_a_new_wording_does_not_stay_approved_under_the_old_review(
        self, approver, template, reviewed_and_approved
    ) -> None:
        """Сохраняющий оставил «подтверждено» — строка всё равно уходит в
        черновик: правка сохранена, обе подписи сняты, и об этом сказано."""
        response = _client(approver).post(
            reverse(CHANGE, args=[reviewed_and_approved.pk]),
            _form(template, status="approved", text_client="Новая формулировка"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        row = reviewed_and_approved
        row.refresh_from_db()
        assert (row.text_client, row.status, row.reviewed_by_id, row.confirmed_by_id) == (
            "Новая формулировка", "system_inference", None, None,
        )
        told = " ".join(str(m) for m in get_messages(response.wsgi_request))
        assert "Подтверждение снято" in told
        assert "Отметка проверки рецензентом снята" in told

    def test_a_reviewed_draft_edited_and_approved_in_one_save_is_refused(
        self, approver, template, reviewer
    ) -> None:
        """Тот же обход с другой стороны: проверенный ЧЕРНОВИК поправить и тем
        же сохранением подтвердить — под прежней отметкой нельзя."""
        draft = ProcedureCapability.objects.create(
            templates=[template], key="example_effect", text_client="Синтетическая формулировка",
            claim_type="medical", claim_scope="supported", source_ref="DOC-2726",
            reviewed_by=reviewer, reviewed_at=timezone.now(),
        )

        response = _client(approver).post(
            reverse(CHANGE, args=[draft.pk]),
            _form(template, status="approved", text_client="Новая формулировка"),
        )

        assert _errors(response) == {"status": ["review_required"]}
        draft.refresh_from_db()
        assert (draft.status, draft.text_client, draft.reviewed_by_id) == (
            "system_inference", "Синтетическая формулировка", reviewer.pk,
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
        self, approver, template, reviewed_and_approved
    ) -> None:
        """Обход, найденный ревью: сменить тип на тот, которому рецензент не
        нужен, и оставить «подтверждено». Смена типа снимает подтверждение, а
        подтвердить продуктовое заново куратор не вправе."""
        client = _client(approver)
        relabel = _form(template, status="approved", claim_type="product")

        first = client.post(reverse(CHANGE, args=[reviewed_and_approved.pk]), relabel)
        row = reviewed_and_approved
        row.refresh_from_db()
        after_relabel = (row.claim_type, row.status, row.reviewed_by_id, row.confirmed_by_id)
        second = client.post(reverse(CHANGE, args=[row.pk]), relabel)

        assert first.status_code == 302, first.context["adminform"].form.errors
        assert after_relabel == ("product", "system_inference", None, None)
        assert _errors(second) == {"claim_type": ["product_boundary_right_required"]}
        row.refresh_from_db()
        assert row.status == "system_inference"

    def test_the_holder_of_the_product_boundary_may_relabel__in_two_steps_and_signs_it(
        self, boundary_holder, template, reviewed_and_approved
    ) -> None:
        """Предел, литералом: держатель продуктовых границ может объявить
        продуктовым и проверенное медицинское — но не одним сохранением:
        смена типа снимает подтверждение, новое подписано его именем."""
        client = _client(boundary_holder)
        relabel = _form(template, status="approved", claim_type="product")

        client.post(reverse(CHANGE, args=[reviewed_and_approved.pk]), relabel)
        row = reviewed_and_approved
        row.refresh_from_db()
        after_first = row.status
        second = client.post(reverse(CHANGE, args=[row.pk]), relabel)

        assert after_first == "system_inference"
        assert second.status_code == 302, second.context["adminform"].form.errors
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


class TestTheOwnersTwoRequirements:
    """Два требования владельца к этому листу — литералом, чтобы не регрессировали."""

    @pytest.fixture
    def superuser(self) -> User:
        return User.objects.create_superuser(
            username="super-2726-req", password="pw",  # pragma: allowlist secret
            email="super-2726-req@example.test", role="admin",
        )

    # 1. Суперпользователь не обходит обязательную рецензию.

    @pytest.mark.parametrize("claim_type", ["medical", "professional", "physiological"])
    def test_a_superuser_cannot_approve_a_reviewed_type_without_a_review(
        self, superuser, template, claim_type
    ) -> None:
        response = _client(superuser).post(reverse(ADD), _form(template, claim_type=claim_type, status="approved"))

        assert _errors(response) == {"status": ["review_required"]}
        assert ProcedureCapability.objects.count() == 0

    def test_a_superuser_approves_a_product_claim(self, superuser, template) -> None:
        """Контроль: отказ выше вызван типом — продуктовое суперпользователь
        подтверждает (третье право касается только ``product``)."""
        response = _client(superuser).post(reverse(ADD), _form(template, claim_type="product", status="approved"))

        assert response.status_code == 302, response.context["adminform"].form.errors
        assert ProcedureCapability.objects.get().status == "approved"

    # 2. Смена типа, содержания или источника снимает подтверждение.

    CHANGES = [
        pytest.param({"claim_type": "professional"}, id="type"),
        pytest.param({"text_client": "Другая формулировка"}, id="content"),
        pytest.param({"source_ref": "DOC-2726-B"}, id="source_ref"),
        pytest.param({"evidence_source": "другой источник"}, id="evidence_source"),
    ]

    @pytest.mark.parametrize("change", CHANGES)
    def test_an_approved_product_claim_loses_its_approval(self, superuser, template, change) -> None:
        """Даже у того, кто вправе всё: сохранить «подтверждено» вместе с
        правкой нельзя — строка уходит в черновик."""
        row = ProcedureCapability.objects.create(
            templates=[template], key="example_effect", text_client="Синтетическая формулировка",
            claim_type="product", claim_scope="supported", **_signed(superuser),
        )

        response = _client(superuser).post(
            reverse(CHANGE, args=[row.pk]),
            _form(template, **{"claim_type": "product", "status": "approved", **change}),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        row.refresh_from_db()
        assert (row.status, row.confirmed_by_id, row.confirmed_at) == ("system_inference", None, None)
        assert any("Подтверждение снято" in str(m) for m in get_messages(response.wsgi_request))

    @pytest.mark.parametrize("change", CHANGES)
    def test_an_approved_reviewed_claim_loses_both_signatures(
        self, superuser, reviewer, template, change
    ) -> None:
        row = ProcedureCapability.objects.create(
            templates=[template], key="example_effect", text_client="Синтетическая формулировка",
            claim_type="medical", claim_scope="supported",
            reviewed_by=reviewer, reviewed_at=timezone.now(), **_signed(superuser),
        )

        response = _client(superuser).post(
            reverse(CHANGE, args=[row.pk]), _form(template, status="approved", **change),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        row.refresh_from_db()
        assert (row.status, row.confirmed_by_id, row.reviewed_by_id) == ("system_inference", None, None)

    def test_the_term_of_validity_alone_keeps_the_approval(self, superuser, template) -> None:
        """Контроль: снимает подтверждение не любое сохранение, а правка типа,
        содержания или источника."""
        row = ProcedureCapability.objects.create(
            templates=[template], key="example_effect", text_client="Синтетическая формулировка",
            claim_type="product", claim_scope="supported", **_signed(superuser),
        )

        response = _client(superuser).post(
            reverse(CHANGE, args=[row.pk]),
            _form(template, claim_type="product", status="approved",
                  valid_until_0="2030-01-01", valid_until_1="00:00:00"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        row.refresh_from_db()
        assert (row.status, row.confirmed_by_id) == ("approved", superuser.pk)

    def test_the_goal_link_loses_its_approval_too(self, superuser, template, goal) -> None:
        capability = ProcedureCapability.objects.create(templates=[template], key="for-link")
        link = CapabilityGoalLink.objects.create(
            capability=capability, goal=goal, claim_type="product", claim_scope="supported",
            **_signed(superuser),
        )

        response = _client(superuser).post(
            reverse("admin:services_capabilitygoallink_change", args=[link.pk]),
            {
                "capability": str(capability.pk), "goal": str(goal.pk),
                "course_pattern": "", "result_horizon": "", "variability_note": "",
                "claim_type": "product", "status": "approved", "claim_scope": "supported",
                "prohibited_statement": "", "limitations": "", "evidence_source": "",
                "evidence_kind": "professional_consensus", "source_ref": "DOC-2726-B",
            },
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        link.refresh_from_db()
        assert (link.status, link.confirmed_by_id) == ("system_inference", None)


class TestAChangeOfTheProcedureDropsTheApproval:
    """Требование владельца: подтверждение не переносится на изменившийся предмет.

    Пять узлов, которые владелец просил проверить отдельно: (1) body→face,
    (2) medical→product, (3) смена источника, (4) новая версия каталога при
    неизменном остальном — подтверждение остаётся, (5) неизменность доказать
    нечем — подтверждать заново.
    """

    @pytest.fixture
    def claims(self, template, goal, reviewer, approver):
        capability = ProcedureCapability.objects.create(
            templates=[template], key="example_effect", text_client="Синтетическая формулировка",
            claim_type="medical", claim_scope="supported",
            reviewed_by=reviewer, reviewed_at=timezone.now(), **_signed(approver),
        )
        link = CapabilityGoalLink.objects.create(
            capability=capability, goal=goal, claim_type="medical", claim_scope="supported",
            reviewed_by=reviewer, reviewed_at=timezone.now(), **_signed(approver),
        )
        return capability, link

    @staticmethod
    def _states(claims) -> list[tuple]:
        out = []
        for row in claims:
            row.refresh_from_db()
            out.append((row.status, row.reviewed_by_id is not None, row.confirmed_by_id is not None))
        return out

    APPROVED_AND_REVIEWED = ("approved", True, True)
    #: Черновик без отметки рецензента; след подтверждения остаётся.
    RETURNED = ("system_inference", False, True)

    # 1. Смена области компетенции body↔face.

    def test_1_moving_the_procedure_from_face_to_body_returns_its_knowledge_to_draft(
        self, template, body, claims
    ) -> None:
        template.category = body
        template.save()

        assert self._states(claims) == [self.RETURNED, self.RETURNED]

    def test_1_moving_its_subcategory_under_another_root_does_the_same(self, template, body, claims) -> None:
        subcategory = template.category
        subcategory.parent = body
        subcategory.save()

        assert self._states(claims) == [self.RETURNED, self.RETURNED]

    def test_1_rebinding_the_claim_to_a_procedure_of_another_area_drops_its_approval(
        self, approver, template, body, claims, reviewer
    ) -> None:
        capability, _ = claims
        elsewhere = ServiceTemplate.objects.create(
            category=body, name="Процедура тела 2726-р", name_short="Процедура тела 2726-р",
        )

        response = _client(approver).post(
            reverse(CHANGE, args=[capability.pk]),
            _form(elsewhere, status="approved", text_client="Синтетическая формулировка"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        capability.refresh_from_db()
        assert (list(capability.templates.all()), capability.status, capability.reviewed_by_id) == (
            [elsewhere], "system_inference", None,
        )

    # 2. medical → product.

    def test_2_medical_to_product_returns_the_claim_to_draft(self, boundary_holder, template, claims) -> None:
        """Даже у держателя продуктовых границ: смена типа — это draft, а не
        переподпись. (Обычному куратору подтвердить продуктовое заново не даёт
        третье право — ``test_the_curator_cannot_relabel_…``.)"""
        capability, _ = claims

        response = _client(boundary_holder).post(
            reverse(CHANGE, args=[capability.pk]), _form(template, status="approved", claim_type="product"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        capability.refresh_from_db()
        assert (capability.claim_type, capability.status, capability.reviewed_by_id) == (
            "product", "system_inference", None,
        )

    # 3. Смена источника.

    @pytest.mark.parametrize("field", ["source_ref", "evidence_source"])
    def test_3_a_new_source_returns_the_claim_to_draft(self, approver, template, claims, field) -> None:
        capability, _ = claims

        response = _client(approver).post(
            reverse(CHANGE, args=[capability.pk]), _form(template, status="approved", **{field: "DOC-2726-NEW"}),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        capability.refresh_from_db()
        assert (capability.status, capability.reviewed_by_id) == ("system_inference", None)

    # 4. Новый номер версии каталога при неизменном остальном.

    def test_4_a_new_catalogue_version_alone_keeps_the_approval(self, template, claims) -> None:
        template.approval_rule_version = "canonical_catalog_2027-01.json"
        template.approval_source_ref = "эталонный справочник владельца, издание 2027-01"
        template.approved_at = timezone.now()
        template.is_popular = not template.is_popular
        template.save()

        assert self._states(claims) == [self.APPROVED_AND_REVIEWED, self.APPROVED_AND_REVIEWED]

    def test_4_saving_the_procedure_unchanged_keeps_the_approval(self, template, claims) -> None:
        template.save()

        assert self._states(claims) == [self.APPROVED_AND_REVIEWED, self.APPROVED_AND_REVIEWED]

    # 5. Неизменность доказать нечем — подтверждать заново.

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("name", "Процедура 2726-р, новая редакция", id="name"),
            pytest.param("contraindications", "новая оговорка", id="contraindications"),
            pytest.param("requires_health_check", True, id="health_check"),
            pytest.param("name_short", "Новая редакция", id="name_short"),
            pytest.param("duration_default", 95, id="duration"),
        ],
    )
    def test_5_a_new_version_that_changes_the_procedure_requires_a_new_confirmation(
        self, template, claims, field, value
    ) -> None:
        """Версия каталога пришла вместе с правкой самой процедуры — подтверждение
        не переносится."""
        template.approval_rule_version = "canonical_catalog_2027-01.json"
        setattr(template, field, value)
        template.save()

        assert self._states(claims) == [self.RETURNED, self.RETURNED]

    NEUTRAL = {
        "approval_rule_version", "approval_source_ref", "approved_at", "approved_by", "approved_rule",
        "is_popular", "sort_order", "created_at", "updated_at",
    }

    @staticmethod
    def _another_value(field, current):
        """Значение того же типа, отличное от нынешнего, — для любого поля модели."""
        kind = field.get_internal_type()
        if kind in ("ForeignKey", "UUIDField"):
            return uuid.uuid4()
        if kind == "BooleanField":
            return not current
        if kind in ("PositiveIntegerField", "IntegerField"):
            return (current or 0) + 1
        if kind == "DateTimeField":
            return timezone.now() + dt.timedelta(days=1)
        return f"{current or ''}-другое"

    def test_5_the_neutral_list_is_these_nine_fields__literally(self) -> None:
        """Разрешительный список литералом: штамп версии каталога и витринные
        признаки. Расширить его — решение владельца."""
        assert ServiceTemplate.KNOWLEDGE_NEUTRAL_FIELDS == self.NEUTRAL
        every_field = {f.name for f in ServiceTemplate._meta.concrete_fields}
        assert self.NEUTRAL <= every_field  # в списке нет несуществующих имён

    def test_5_every_other_field_of_the_procedure_counts_as_a_change(self, template) -> None:
        """Исполнением, по каждому полю модели: поле, которого нет в списке
        нейтральных, — в том числе добавленное в модель позже — считается
        значимым. Доказать неизменность по нему нечем."""
        checked = []
        for field in ServiceTemplate._meta.concrete_fields:
            if field.primary_key:
                continue
            row = ServiceTemplate.objects.get(pk=template.pk)
            setattr(row, field.attname, self._another_value(field, getattr(row, field.attname)))
            reported = [change["field"] for change in row._significant_changes()]
            assert reported == ([] if field.name in self.NEUTRAL else [field.name]), field.name
            checked.append(field.name)

        # Контроль: перебор дошёл до полей, о которых владелец сказал отдельно,
        # и до тех, что стали значимыми по умолчанию.
        assert {
            "name", "contraindications", "requires_health_check", "category",
            "lifecycle", "health_check_origin", "canonical_code", "approved_by",
        } <= set(checked)

    def test_a_different_line_ending_is_not_a_change(self, template, claims) -> None:
        """Браузер присылает CRLF там, где засев положил LF: первое сохранение
        формы не должно возвращать знание в черновик."""
        ServiceTemplate.objects.filter(pk=template.pk).update(contraindications="строка один\nстрока два")
        row = ServiceTemplate.objects.get(pk=template.pk)

        row.contraindications = "строка один\r\nстрока два"
        row.save()

        assert self._states(claims) == [self.APPROVED_AND_REVIEWED, self.APPROVED_AND_REVIEWED]

    def test_if_the_reset_fails_the_change_of_the_procedure_is_not_stored(
        self, template, claims, monkeypatch
    ) -> None:
        """Запись и сброс — одной транзакцией. Иначе правка осталась бы в базе,
        повторное сохранение разницы не увидело бы — и подтверждение пережило
        бы правку навсегда."""
        def boom(*args, **kwargs):
            raise RuntimeError("reset failed")

        monkeypatch.setattr(services_models, "reset_claims_of_procedure", boom)
        template.name = "Процедура 2726-р, новая редакция"

        # Без своей транзакции вокруг: откатить запись должен сам ``save()``.
        with pytest.raises(RuntimeError):
            template.save()

        assert ServiceTemplate.objects.get(pk=template.pk).name == "Процедура 2726-р"
        assert self._states(claims) == [self.APPROVED_AND_REVIEWED, self.APPROVED_AND_REVIEWED]

    def test_when_dependencies_cannot_be_told_every_claim_of_this_procedure_is_reset(
        self, template, claims, boundary_holder, goal
    ) -> None:
        """От какого поля процедуры зависит конкретное утверждение, нигде не
        записано — значит, значимая правка возвращает в черновик ВСЕ утверждения
        этой процедуры: и проверенные рецензентом, и продуктовое, которому
        рецензент не нужен."""
        product = ProcedureCapability.objects.create(
            templates=[template], key="product-claim", claim_type="product", claim_scope="supported",
            **_signed(boundary_holder),
        )

        template.name = "Процедура 2726-р, новая редакция"
        template.save()

        assert self._states(claims) == [self.RETURNED, self.RETURNED]
        product.refresh_from_db()
        assert product.status == "system_inference"

    def test_a_draft_reviewed_claim_loses_its_review_too(self, template, body, reviewer) -> None:
        draft = ProcedureCapability.objects.create(
            templates=[template], key="reviewed-draft", claim_type="medical",
            reviewed_by=reviewer, reviewed_at=timezone.now(),
        )

        template.category = body
        template.save()

        draft.refresh_from_db()
        assert (draft.status, draft.reviewed_by_id) == ("system_inference", None)

    def test_the_knowledge_of_other_procedures_is_untouched(self, template, body, claims, reviewer, approver) -> None:
        """Контроль: возврат касается только знания об изменившейся процедуре."""
        other = ServiceTemplate.objects.create(
            category=template.category, name="Соседняя процедура 2726-р", name_short="Соседняя 2726-р",
        )
        neighbour = ProcedureCapability.objects.create(
            templates=[other], key="neighbour", claim_type="medical", claim_scope="supported",
            reviewed_by=reviewer, reviewed_at=timezone.now(), **_signed(approver),
        )

        template.category = body
        template.save()

        assert self._states([neighbour]) == [self.APPROVED_AND_REVIEWED]


class TestTheCuratorSeesWhatWasReset:
    """Требование владельца: причина, старое и новое значение, время, список
    затронутых утверждений — и куратору показано, что нужна повторная проверка."""

    @pytest.fixture
    def claims(self, template, goal, reviewer, approver):
        capability = ProcedureCapability.objects.create(
            templates=[template], key="example_effect", text_client="Синтетическая формулировка",
            claim_type="medical", claim_scope="supported",
            reviewed_by=reviewer, reviewed_at=timezone.now(), **_signed(approver),
        )
        link = CapabilityGoalLink.objects.create(
            capability=capability, goal=goal, claim_type="medical", claim_scope="supported",
            reviewed_by=reviewer, reviewed_at=timezone.now(), **_signed(approver),
        )
        return capability, link

    @pytest.fixture
    def owner(self) -> User:
        return User.objects.create_superuser(
            username="owner-2726-audit", password="pw",  # pragma: allowlist secret
            email="owner-2726-audit@example.test", role="admin",
        )

    def test_a_change_of_the_procedure_is_journalled_per_claim_with_old_and_new(self, template, claims) -> None:
        capability, link = claims
        before = timezone.now()

        template.name = "Процедура 2726-р, новая редакция"
        template.save()

        rows = list(ClaimApprovalReset.objects.order_by("created_at"))
        assert {(r.claim_kind, r.capability_id, r.goal_link_id) for r in rows} == {
            ("capability", capability.pk, None), ("goal_link", None, link.pk),
        }
        for row in rows:
            assert row.reason == "procedure_changed"
            assert row.changes == [
                {"field": "name", "old": "Процедура 2726-р", "new": "Процедура 2726-р, новая редакция"},
            ]
            assert (row.was_approved, row.had_review) == (True, True)
            assert row.created_at >= before

    def test_a_move_to_another_area_names_both_areas(self, template, body, claims) -> None:
        template.category = body
        template.save()

        row = ClaimApprovalReset.objects.filter(capability__isnull=False).get()
        assert row.changes == [{"field": "category", "old": "Пилинги 2726-р", "new": "Тело 2726-р"}]

    def test_a_new_catalogue_version_alone_writes_nothing(self, template, claims) -> None:
        template.approval_rule_version = "canonical_catalog_2027-01.json"
        template.save()

        assert ClaimApprovalReset.objects.count() == 0

    @staticmethod
    def _posted(form) -> dict:
        """То, что прислал бы браузер, открыв форму и ничего не тронув."""
        data = {}
        for name in form.fields:
            value = form.initial.get(name)
            if value is None or value is False:
                continue
            data[form.add_prefix(name)] = "on" if value is True else value
        return data

    def test_the_procedure_admin_tells_the_curator_at_once(self, owner, template, claims) -> None:
        url = reverse("admin:services_servicetemplate_change", args=[template.pk])
        client = _client(owner)
        page = client.get(url)
        data = self._posted(page.context["adminform"].form)
        for inline in page.context["inline_admin_formsets"]:
            prefix = inline.formset.prefix
            data.update({f"{prefix}-TOTAL_FORMS": "0", f"{prefix}-INITIAL_FORMS": "0"})

        untouched = client.post(url, data)
        nothing_yet = ClaimApprovalReset.objects.count()
        edited = client.post(url, {**data, "contraindications": "новая оговорка"})

        # Контроль: сохранение процедуры без правок знание не трогает и ничего не говорит.
        assert untouched.status_code == 302, untouched.context["adminform"].form.errors
        assert nothing_yet == 0
        assert not any("повторной проверки" in str(m) for m in get_messages(untouched.wsgi_request))
        assert edited.status_code == 302, edited.context["adminform"].form.errors
        told = " ".join(str(m) for m in get_messages(edited.wsgi_request))
        assert "требует повторной проверки — возможностей: 1, связей с целями: 1" in told
        assert ClaimApprovalReset.objects.count() == 2

    def test_an_edit_in_the_table_under_the_category_tells_the_curator_too(
        self, owner, template, claims
    ) -> None:
        """Процедуру можно править и из карточки категории — правило то же, и
        куратор должен узнать о сбросе там же."""
        url = reverse("admin:services_servicecategory_change", args=[template.category_id])
        client = _client(owner)
        page = client.get(url)
        data = self._posted(page.context["adminform"].form)
        data["slug"] = "peels-2726-r"  # слаг из кириллицы форма категории не принимает
        formset = page.context["inline_admin_formsets"][0].formset
        data.update({
            f"{formset.prefix}-TOTAL_FORMS": str(len(formset.forms)),
            f"{formset.prefix}-INITIAL_FORMS": str(len(formset.forms)),
        })
        for form in formset.forms:
            data.update(self._posted(form))
            data[form.add_prefix("id")] = str(form.instance.pk)
            data[form.add_prefix("category")] = str(template.category_id)
            data[form.add_prefix("name")] = "Процедура 2726-р, новая редакция"

        response = client.post(url, data)

        assert response.status_code == 302, (
            response.context["adminform"].form.errors, formset.errors,
        )
        told = " ".join(str(m) for m in get_messages(response.wsgi_request))
        assert "требует повторной проверки — возможностей: 1, связей с целями: 1" in told

    def test_the_claim_page_and_the_queue_show_the_reset(self, owner, template, claims) -> None:
        capability, _ = claims
        template.contraindications = "новая оговорка"
        template.save()
        client = _client(owner)

        page = client.get(reverse(CHANGE, args=[capability.pk]))
        queue = client.get(reverse("admin:services_procedurecapability_changelist"), {"needs_reconfirmation": "yes"})
        everything = client.get(reverse("admin:services_procedurecapability_changelist"))

        notice = page.content.decode()
        assert "подтверждение снято" in notice
        assert "Изменены значимые данные процедуры" in notice
        assert "новая оговорка" in notice
        assert [row.pk for row in queue.context["cl"].result_list] == [capability.pk]
        assert everything.status_code == 200

    def test_an_untouched_claim_is_not_in_the_queue(self, owner, template) -> None:
        ProcedureCapability.objects.create(templates=[template], key="plain-draft")

        queue = _client(owner).get(
            reverse("admin:services_procedurecapability_changelist"), {"needs_reconfirmation": "yes"},
        )

        assert list(queue.context["cl"].result_list) == []

    def test_an_edit_of_the_claim_itself_is_journalled_with_old_and_new(
        self, approver, template, claims
    ) -> None:
        capability, _ = claims

        _client(approver).post(
            reverse(CHANGE, args=[capability.pk]),
            _form(template, status="approved", text_client="Новая формулировка"),
        )

        own = ClaimApprovalReset.objects.get(capability=capability)
        assert own.reason == "claim_edited"
        assert own.changes == [
            {"field": "text_client", "old": "Синтетическая формулировка", "new": "Новая формулировка"},
        ]
        assert (own.was_approved, own.had_review) == (True, True)

    def test_an_edit_saved_as_a_draft_by_hand_is_journalled_as_a_lost_approval(
        self, approver, template, claims
    ) -> None:
        """DRF-2741: сохранявший сам выбрал «черновик» и тем же сохранением
        поправил текст — в журнале строка была подтверждённой, она ею и была."""
        capability, _ = claims

        _client(approver).post(
            reverse(CHANGE, args=[capability.pk]),
            _form(template, status="system_inference", text_client="Новая формулировка"),
        )

        own = ClaimApprovalReset.objects.get(capability=capability)
        assert (own.was_approved, own.had_review) == (True, True)

    def test_a_new_approval_closes_the_reset_and_a_later_return_to_draft_does_not_reopen_it(
        self, owner, template, claims
    ) -> None:
        capability, _ = claims
        template.contraindications = "новая оговорка"
        template.save()
        ClaimReviewer.objects.create(user=owner, claim_type="medical")
        client = _client(owner)
        queue_url = reverse("admin:services_procedurecapability_changelist")
        change_url = reverse(CHANGE, args=[capability.pk])

        def in_queue() -> list:
            page = client.get(queue_url, {"needs_reconfirmation": "yes"})
            return [row.pk for row in page.context["cl"].result_list]

        waiting = in_queue()
        approved = client.post(change_url, _form(template, status="approved", mark_reviewed="on"))
        after_approval = in_queue()
        client.post(change_url, _form(template, status="system_inference"))
        after_return = in_queue()

        assert waiting == [capability.pk]
        assert approved.status_code == 302, approved.context["adminform"].form.errors
        assert after_approval == []
        # Куратор сам вернул строку в черновик — это не сброс, и прежняя причина не всплывает.
        assert after_return == []
        assert "подтверждение снято" not in client.get(change_url).content.decode()
        assert ClaimApprovalReset.objects.get(capability=capability).resolved_at is not None

    def test_two_resets_of_one_claim_put_it_in_the_queue_once(self, owner, template, claims) -> None:
        capability, _ = claims
        reviewer_id = capability.reviewed_by_id
        for text in ("первая оговорка", "вторая оговорка"):
            ProcedureCapability.objects.filter(pk=capability.pk).update(
                status="approved", reviewed_by_id=reviewer_id, reviewed_at=timezone.now(),
            )
            template.contraindications = text
            template.save()

        queue = _client(owner).get(
            reverse("admin:services_procedurecapability_changelist"), {"needs_reconfirmation": "yes"},
        )

        assert ClaimApprovalReset.objects.filter(capability=capability).count() == 2
        assert [row.pk for row in queue.context["cl"].result_list] == [capability.pk]

    def test_a_claim_that_was_reset_can_still_be_deleted_and_the_journal_survives(
        self, owner, template, claims
    ) -> None:
        """Журнал в админке только читается — и не должен из-за этого запирать
        удаление утверждения; а удаление не должно стирать след."""
        capability, link = claims
        template.name = "Процедура 2726-р, новая редакция"
        template.save()

        response = _client(owner).post(
            reverse("admin:services_procedurecapability_delete", args=[capability.pk]), {"post": "yes"},
        )

        assert response.status_code == 302
        assert not ProcedureCapability.objects.filter(pk=capability.pk).exists()
        rows = list(ClaimApprovalReset.objects.order_by("claim_kind"))
        assert [(r.claim_kind, r.capability_id, r.goal_link_id) for r in rows] == [
            ("capability", None, None), ("goal_link", None, None),
        ]
        # Подпись снята в момент сброса — уже с новым названием процедуры.
        assert rows[0].claim_label == "Процедура 2726-р, новая редакция · example_effect"

    def test_a_renamed_goal_returns_its_links_to_draft__and_only_them(
        self, template, claims, goal, reviewer, approver
    ) -> None:
        """Связь утверждает «помогает ЭТОЙ цели»: смена названия цели меняет
        предмет утверждения. Адресно — связи этой цели; возможность и связи
        с другими целями не тронуты."""
        capability, link = claims
        other_goal = GoalOption.objects.create(key="other-goal-2726-j", label="Другая цель")
        other_link = CapabilityGoalLink.objects.create(
            capability=capability, goal=other_goal, claim_type="medical", claim_scope="supported",
            reviewed_by=reviewer, reviewed_at=timezone.now(), **_signed(approver),
        )

        goal.label = "Цель 2726-р, новая редакция"
        goal.save()
        goal.sort_order = 7  # не содержание цели
        goal.save()

        for row in (capability, link, other_link):
            row.refresh_from_db()
        assert (link.status, link.reviewed_by_id) == ("system_inference", None)
        assert (capability.status, other_link.status) == ("approved", "approved")
        journalled = ClaimApprovalReset.objects.get()
        assert (journalled.reason, journalled.goal_link_id) == ("goal_changed", link.pk)
        assert journalled.changes == [
            {"field": "goal.label", "old": "Цель 2726-р", "new": "Цель 2726-р, новая редакция"},
        ]

    def test_the_journal_is_read_only(self, owner, template, claims) -> None:
        template.name = "Процедура 2726-р, новая редакция"
        template.save()
        row = ClaimApprovalReset.objects.first()
        client = _client(owner)

        listing = client.get(reverse("admin:services_claimapprovalreset_changelist"))
        add = client.get(reverse("admin:services_claimapprovalreset_add"))
        delete = client.post(
            reverse("admin:services_claimapprovalreset_delete", args=[row.pk]), {"post": "yes"},
        )

        assert listing.status_code == 200
        assert (add.status_code, delete.status_code) == (403, 403)
        assert ClaimApprovalReset.objects.filter(pk=row.pk).exists()


class TestAnEditOfTheCapabilityUnderAReviewedLink:
    """Связь с целью проверена как утверждение об ЭТОЙ возможности."""

    @pytest.fixture
    def capability(self, template) -> ProcedureCapability:
        return ProcedureCapability.objects.create(
            templates=[template], key="example_effect", text_client="Синтетическая формулировка",
            claim_type="product", claim_scope="supported", source_ref="DOC-2726",
            evidence_kind="professional_consensus",
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
        assert any("возвращены в черновик и требуют повторной проверки — 2" in m for m in told)

    def test_the_reset_is_addressed__only_the_links_of_the_edited_capability(
        self, approver, template, capability, approved_link, reviewer
    ) -> None:
        """Адресный сброс: от возможности зависят ЕЁ связи. Вторая возможность
        той же процедуры и её связь не тронуты."""
        other_goal = GoalOption.objects.create(key="other-goal-2726-r", label="Другая цель")
        sibling = ProcedureCapability.objects.create(
            templates=[template], key="sibling", claim_type="medical", claim_scope="supported",
            reviewed_by=reviewer, reviewed_at=timezone.now(), **_signed(approver),
        )
        sibling_link = CapabilityGoalLink.objects.create(
            capability=sibling, goal=other_goal, claim_type="medical", claim_scope="supported",
            reviewed_by=reviewer, reviewed_at=timezone.now(), **_signed(approver),
        )

        response = _client(approver).post(
            reverse(CHANGE, args=[capability.pk]),
            _form(template, claim_type="product", text_client="Новая формулировка"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        approved_link.refresh_from_db()
        sibling.refresh_from_db()
        sibling_link.refresh_from_db()
        assert approved_link.status == "system_inference"
        assert (sibling.status, sibling.reviewed_by_id) == ("approved", reviewer.pk)
        assert (sibling_link.status, sibling_link.reviewed_by_id) == ("approved", reviewer.pk)
        journalled = ClaimApprovalReset.objects.get(goal_link=approved_link)
        assert journalled.reason == "capability_edited"
        assert not ClaimApprovalReset.objects.filter(goal_link=sibling_link).exists()

    def test_an_approved_product_link_depends_on_its_capability_too(
        self, boundary_holder, template, capability, goal
    ) -> None:
        """Связи рецензент мог быть и не нужен — она всё равно утверждает нечто
        об этой возможности и уходит в черновик вместе с её правкой."""
        link = CapabilityGoalLink.objects.create(
            capability=capability, goal=goal, claim_type="product", claim_scope="supported",
            **_signed(boundary_holder),
        )

        response = _client(boundary_holder).post(
            reverse(CHANGE, args=[capability.pk]),
            _form(template, claim_type="product", text_client="Новая формулировка"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        link.refresh_from_db()
        assert link.status == "system_inference"

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
    "evidence_kind": "professional_consensus",
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

BEFORE = "0037_drf2726_approval_right"
THE_MIGRATION = "0038_drf2726_reviewer_role"

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
        # Листья ВСЕГО графа, не только services: откат services уводит за собой
        # зависящие от него миграции других приложений (wellness/0009 и дальше),
        # и один лист services их не возвращает — узлы после этого шли бы по
        # базе без их таблиц и колонок.
        executor.migrate(executor.loader.graph.leaf_nodes())
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
            "evidence_kind": "professional_consensus",
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
