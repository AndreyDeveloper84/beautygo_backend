"""DRF-2741 — противопоказания хранятся структурированно: условие → действие.

Решение владельца №2 от 02.10: слот C8 — набор строк с условием, действием из
закрытого списка (``exclude`` / ``postpone`` / ``refer_to_doctor`` /
``emergency``), источником, областью применения, датой пересмотра и
рецензентом. «Один непроверяемый текстовый блок недостаточен».

Что держат узлы:

* действий — ровно четыре, литералом; условие и действие обязательны у любой
  строки (база);
* подтверждённая строка без источника, вида доказательства, даты пересмотра,
  отметки рецензента или подписи не хранится (база);
* в админке: «проверено» ставит только назначенный рецензент медицинских
  утверждений, чьи назначения покрывают ВСЮ область применения строки;
  суперпользователь и держатель права подтверждения рецензентами не являются;
  «подтверждено» — только держатель права подтверждения и только после
  проверки; это две подписи двух действий;
* правило сброса то же, что у остального знания (DRF-2726): правка содержания
  или области подтверждённой строки возвращает её в черновик; значимая правка
  процедуры из области применения — тоже; соседние строки не трогаются; всё
  пишется в журнал и показывается куратору;
* область меняется и без правки строки: удаление процедуры или категории из
  неё, появление или перенос процедуры в категории, которой область названа, —
  это тоже сброс;
* дата пересмотра у подтверждаемой строки — впереди;
* из файла строка приходит только черновиком: статус, отметка и подпись в
  файле — неизвестные ключи;
* клиентский путь знания таблицу не читает.

Все тексты синтетические.
"""

from __future__ import annotations

import datetime as dt
import json
from io import StringIO
from pathlib import Path

import pytest
from django.contrib import admin as django_admin
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from services import capabilities, knowledge_api
from services import models as services_models
from services.contraindication_intake import ACTIONS, FILE_KEYS, approval_errors
from services.knowledge_review import may_review_scope
from services.models import (
    ClaimApprovalReset,
    ClaimReviewer,
    GoalOption,
    ProcedureContraindication,
    ServiceCategory,
    ServiceTemplate,
)
from users.deletion_executor import undecided_pointers
from users.models import User

pytestmark = pytest.mark.django_db

ADD = "admin:services_procedurecontraindication_add"
CHANGE = "admin:services_procedurecontraindication_change"
DELETE = "admin:services_procedurecontraindication_delete"
LIST = "admin:services_procedurecontraindication_changelist"

EVERYDAY = [f"{action}_procedurecontraindication" for action in ("add", "change", "delete", "view")]
APPROVE = ["approve_procedurecontraindication"]
EXAMPLE_FILE = Path(__file__).resolve().parents[1] / "seeds" / "procedure_knowledge.example.json"


def _staff(username: str, codenames: list[str]) -> User:
    user = User.objects.create_user(
        username=username, password="pw", role="admin", is_staff=True,  # pragma: allowlist secret
    )
    found = Permission.objects.filter(content_type__app_label="services", codename__in=codenames)
    assert found.count() == len(codenames), sorted(set(codenames) - {p.codename for p in found})
    user.user_permissions.set(found)
    return user


def _client(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def body() -> ServiceCategory:
    return ServiceCategory.objects.create(name="Тело 2741")


@pytest.fixture
def face() -> ServiceCategory:
    return ServiceCategory.objects.create(name="Лицо 2741")


@pytest.fixture
def template(body) -> ServiceTemplate:
    """Процедура в подкатегории «тела»."""
    massage = ServiceCategory.objects.create(name="Массаж 2741", parent=body)
    return ServiceTemplate.objects.create(
        category=massage, name="Процедура 2741", name_short="Процедура 2741", canonical_code="1.1.3",
    )


@pytest.fixture
def face_template(face) -> ServiceTemplate:
    return ServiceTemplate.objects.create(category=face, name="Процедура лица 2741", name_short="Лицо 2741")


@pytest.fixture
def curator() -> User:
    """Ведёт черновики; не рецензент и без права подтверждения."""
    return _staff("curator-2741", EVERYDAY)


@pytest.fixture
def reviewer() -> User:
    """Назначенный рецензент медицинских утверждений, любая область; подтверждать не вправе."""
    user = _staff("reviewer-2741", EVERYDAY)
    ClaimReviewer.objects.create(user=user, claim_type="medical")
    return user


@pytest.fixture
def approver() -> User:
    """Право подтверждения есть, назначения рецензентом нет."""
    return _staff("approver-2741", EVERYDAY + APPROVE)


#: Дата пересмотра «впереди» — от сегодняшнего дня, а не литералом: узел с
#: зашитым годом покраснел бы сам, когда этот год наступит.
AHEAD = timezone.localdate() + dt.timedelta(days=365)

COMPLETE = {
    "source_ref": "DOC-2741",
    "evidence_source": "синтетический источник",
    "evidence_kind": "clinical_guideline",
    "review_date": AHEAD,
}


def _both_signed(reviewer: User, approver: User) -> dict:
    now = timezone.now()
    return {
        "status": "approved", "reviewed_by": reviewer, "reviewed_at": now,
        "confirmed_by": approver, "confirmed_at": now,
    }


def _refused_by(constraint: str, create) -> None:
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        create()
    assert constraint in str(raised.value)


def _row(**fields) -> ProcedureContraindication:
    data = {"key": "example_condition", "condition": "синтетическое условие", "action": "postpone"}
    data.update(fields)
    return ProcedureContraindication.objects.create(**data)


@pytest.fixture
def approved(template, reviewer, approver) -> ProcedureContraindication:
    row = _row(**COMPLETE, **_both_signed(reviewer, approver))
    row.templates.set([template])
    return row


# ── Список действий и база ─────────────────────────────────────────────────


class TestTheFourActions:
    def test_the_model_and_the_inputs_know_the_same_four(self) -> None:
        the_four = ("exclude", "postpone", "refer_to_doctor", "emergency")

        assert tuple(a.value for a in ProcedureContraindication.Action) == the_four
        assert ACTIONS == the_four

    @pytest.mark.parametrize("action", ["exclude", "postpone", "refer_to_doctor", "emergency"])
    def test_each_action_is_stored(self, action) -> None:
        assert _row(action=action).action == action

    @pytest.mark.parametrize("action", ["", "be_careful", "Postpone"])
    def test_an_action_outside_the_list_is_refused(self, action) -> None:
        _refused_by("procedurecontraindication_action_known", lambda: _row(action=action))

    def test_a_row_without_a_condition_is_refused(self) -> None:
        _refused_by("procedurecontraindication_condition_not_blank", lambda: _row(condition=""))


class TestTheDatabaseKeepsApprovedComplete:
    def test_a_complete_approved_row_is_stored(self, reviewer, approver) -> None:
        """Контроль: подтверждённая строка вообще сохраняется."""
        row = _row(**COMPLETE, **_both_signed(reviewer, approver))

        assert (row.reviewed_by_id, row.confirmed_by_id) == (reviewer.pk, approver.pk)

    @pytest.mark.parametrize(
        "broken",
        [
            {"source_ref": ""},
            {"evidence_kind": ""},
            {"evidence_kind": "маркетинг салона"},
            {"review_date": None},
            {"reviewed_by": None, "reviewed_at": None},
            {"confirmed_by": None},
            {"confirmed_at": None},
        ],
        ids=["no-source", "no-kind", "kind-outside-the-list", "no-review-date", "no-review", "no-signer", "no-time"],
    )
    def test_an_approved_row_missing_any_part_is_refused(self, reviewer, approver, broken) -> None:
        fields = {**COMPLETE, **_both_signed(reviewer, approver), **broken}

        _refused_by("procedurecontraindication_approved_complete", lambda: _row(**fields))

    def test_half_a_review_is_refused(self, reviewer) -> None:
        _refused_by("procedurecontraindication_review_is_whole", lambda: _row(reviewed_by=reviewer))

    def test_a_draft_needs_only_its_condition_and_action(self) -> None:
        assert _row().status == "system_inference"

    def test_the_key_is_unique(self) -> None:
        _row()

        with pytest.raises(IntegrityError), transaction.atomic():
            _row()


class TestTheSharedRule:
    def test_a_draft_has_no_demands(self) -> None:
        assert approval_errors(
            status="system_inference", source_ref="", evidence_kind="", review_date=None, has_scope=False,
            today=dt.date(2026, 10, 2),
        ) == {}

    def test_an_approved_row_is_asked_for_everything_at_once(self) -> None:
        errors = approval_errors(
            status="approved", source_ref="нет", evidence_kind="", review_date=None, has_scope=False,
            today=dt.date(2026, 10, 2),
        )

        assert set(errors) == {"source_ref", "evidence_kind", "review_date", "templates"}

    @pytest.mark.parametrize(
        ("review_date", "refused"),
        [
            (dt.date(2026, 10, 1), True),
            (dt.date(2026, 10, 2), True),  # наступила сегодня — уже не «впереди»
            (dt.date(2026, 10, 3), False),
        ],
    )
    def test_the_review_date_of_an_approved_row_is_ahead(self, review_date, refused) -> None:
        errors = approval_errors(
            status="approved", source_ref="DOC-2741", evidence_kind="clinical_guideline",
            review_date=review_date, has_scope=True, today=dt.date(2026, 10, 2),
        )

        assert errors == ({"review_date": errors.get("review_date")} if refused else {})
        assert ("review_date" in errors) is refused

    def test_a_kind_outside_the_list_is_refused_on_a_draft_too(self) -> None:
        errors = approval_errors(
            status="system_inference", source_ref="", evidence_kind="practice", review_date=None, has_scope=False,
            today=dt.date(2026, 10, 2),
        )

        assert set(errors) == {"evidence_kind"}


# ── Кто вправе проверить ───────────────────────────────────────────────────


class TestWhoMayReviewAContraindication:
    def test_a_reviewer_of_any_area_covers_any_scope(self, reviewer, template, face_template) -> None:
        assert may_review_scope(reviewer, claim_type="medical", templates=[template, face_template], categories=[])
        assert may_review_scope(reviewer, claim_type="medical", templates=[], categories=[])

    def test_an_area_reviewer_must_cover_every_procedure_in_the_scope(
        self, template, face_template, body, face
    ) -> None:
        """Пример владельца: «тело/массаж и кожа лица — возможно, разные люди»."""
        body_reviewer = _staff("body-2741", EVERYDAY)
        ClaimReviewer.objects.create(user=body_reviewer, claim_type="medical", category=body)

        def may(templates, categories=()):
            return may_review_scope(
                body_reviewer, claim_type="medical", templates=templates, categories=list(categories),
            )

        assert may([template])  # подкатегория «тела»
        assert may([], [body])
        assert not may([template, face_template])  # одна из процедур — вне его области
        assert not may([], [face])
        assert not may([])  # пустая область — только рецензент без ограничения области

    def test_other_claim_types_admin_rights_and_the_approval_right_do_not_count(self, template, approver) -> None:
        physiological = _staff("physio-2741", EVERYDAY)
        ClaimReviewer.objects.create(user=physiological, claim_type="physiological")
        superuser = User.objects.create_superuser(
            username="super-2741", password="pw",  # pragma: allowlist secret
            email="super-2741@example.test", role="admin",
        )

        for user in (physiological, superuser, approver, None):
            assert not may_review_scope(user, claim_type="medical", templates=[template], categories=[])


# ── Админка ────────────────────────────────────────────────────────────────


def _form(template: ServiceTemplate | None = None, **overrides) -> dict:
    data = {
        "key": "example_condition",
        "condition": "синтетическое условие",
        "action": "postpone",
        "action_note": "",
        "scope_note": "",
        "source_ref": "DOC-2741",
        "evidence_source": "синтетический источник",
        "evidence_kind": "clinical_guideline",
        "review_date": AHEAD.isoformat(),
        "status": "system_inference",
    }
    if template is not None:
        data["templates"] = [str(template.pk)]
    data.update(overrides)
    return data


def _codes(response) -> dict[str, list[str]]:
    assert response.status_code == 200  # форма возвращена, а не сохранена
    data = response.context["adminform"].form.errors.as_data()
    return {field: [e.code for e in errs] for field, errs in data.items()}


class TestTheAdmin:
    def test_the_table_is_registered(self) -> None:
        assert ServiceTemplate in django_admin.site._registry  # контроль: реестр читается
        assert ProcedureContraindication in django_admin.site._registry

    def test_a_curator_saves_a_draft(self, curator, template) -> None:
        """Контроль: без особых прав черновик ведётся."""
        response = _client(curator).post(reverse(ADD), _form(template))

        assert response.status_code == 302, response.context["adminform"].form.errors
        row = ProcedureContraindication.objects.get()
        assert (row.status, row.reviewed_by_id, row.confirmed_by_id) == ("system_inference", None, None)
        assert list(row.templates.all()) == [template]

    def test_a_curator_cannot_tick_the_review(self, curator, template) -> None:
        response = _client(curator).post(reverse(ADD), _form(template, mark_reviewed="on"))

        assert _codes(response) == {"mark_reviewed": ["reviewer_competence_required"]}
        assert ProcedureContraindication.objects.count() == 0

    def test_the_approver_cannot_tick_the_review_for_the_reviewer(self, approver, template) -> None:
        response = _client(approver).post(
            reverse(ADD), _form(template, status="approved", mark_reviewed="on"),
        )

        codes = _codes(response)
        assert codes["mark_reviewed"] == ["reviewer_competence_required"]
        assert codes["status"] == ["review_required"]

    def test_a_superuser_neither_reviews_nor_approves_without_a_review(self, template) -> None:
        """Требование владельца (DRF-2726): суперпользователь не обходит рецензию."""
        superuser = User.objects.create_superuser(
            username="super-2741", password="pw",  # pragma: allowlist secret
            email="super-2741@example.test", role="admin",
        )
        client = _client(superuser)

        ticked = client.post(reverse(ADD), _form(template, mark_reviewed="on"))
        approved = client.post(reverse(ADD), _form(template, status="approved"))

        assert _codes(ticked) == {"mark_reviewed": ["reviewer_competence_required"]}
        assert _codes(approved) == {"status": ["review_required"]}

    def test_the_reviewer_cannot_approve(self, reviewer, template) -> None:
        response = _client(reviewer).post(
            reverse(ADD), _form(template, status="approved", mark_reviewed="on"),
        )

        assert _codes(response) == {"status": ["approval_right_required"]}

    def test_reviewed_by_one_person_and_approved_by_another(self, reviewer, approver, template) -> None:
        reviewed = _client(reviewer).post(reverse(ADD), _form(template, mark_reviewed="on"))
        assert reviewed.status_code == 302, reviewed.context["adminform"].form.errors
        row = ProcedureContraindication.objects.get()
        assert (row.status, row.reviewed_by_id, row.confirmed_by_id) == ("system_inference", reviewer.pk, None)
        stamped_at = row.reviewed_at

        approved = _client(approver).post(reverse(CHANGE, args=[row.pk]), _form(template, status="approved"))

        assert approved.status_code == 302, approved.context["adminform"].form.errors
        row.refresh_from_db()
        assert (row.status, row.reviewed_by_id, row.confirmed_by_id) == ("approved", reviewer.pk, approver.pk)
        assert row.reviewed_at == stamped_at

    def test_the_scope_decides_who_may_review_in_the_form(self, template, face_template, body) -> None:
        body_reviewer = _staff("body-2741", EVERYDAY)
        ClaimReviewer.objects.create(user=body_reviewer, claim_type="medical", category=body)
        client = _client(body_reviewer)

        both_areas = client.post(reverse(ADD), _form(
            templates=[str(template.pk), str(face_template.pk)], mark_reviewed="on",
        ))
        own_area = client.post(reverse(ADD), _form(template, mark_reviewed="on"))

        assert _codes(both_areas) == {"mark_reviewed": ["reviewer_competence_required"]}
        assert own_area.status_code == 302, own_area.context["adminform"].form.errors
        assert ProcedureContraindication.objects.get().reviewed_by_id == body_reviewer.pk

    def test_an_unreviewed_row_is_not_approved(self, approver, template) -> None:
        response = _client(approver).post(reverse(ADD), _form(template, status="approved"))

        assert _codes(response) == {"status": ["review_required"]}

    @pytest.mark.parametrize(
        ("stored", "field"),
        [
            ({"source_ref": ""}, "source_ref"),
            ({"source_ref": "нет"}, "source_ref"),
            ({"evidence_kind": ""}, "evidence_kind"),
            ({"review_date": None}, "review_date"),
        ],
        ids=["no-source", "a-placeholder-source", "no-kind", "no-review-date"],
    )
    def test_a_reviewed_draft_missing_a_part_is_not_approved(
        self, reviewer, approver, template, stored, field
    ) -> None:
        """Черновик проверен рецензентом, но неполон. Подтверждение меняет только
        статус — отметка рецензента в силе, и отказать может одно: само поле."""
        row = _row(**{**COMPLETE, **stored}, reviewed_by=reviewer, reviewed_at=timezone.now())
        row.templates.set([template])
        as_stored = {name: ("" if value is None else value) for name, value in stored.items()}

        response = _client(approver).post(
            reverse(CHANGE, args=[row.pk]), _form(template, status="approved", **as_stored),
        )

        assert _codes(response) == {field: ["approval_incomplete"]}
        row.refresh_from_db()
        assert row.status == "system_inference"

    def test_a_review_date_that_has_come_is_not_approved(self, reviewer, approver, template) -> None:
        today = timezone.localdate()
        row = _row(**{**COMPLETE, "review_date": today}, reviewed_by=reviewer, reviewed_at=timezone.now())
        row.templates.set([template])

        response = _client(approver).post(
            reverse(CHANGE, args=[row.pk]), _form(template, status="approved", review_date=today.isoformat()),
        )

        assert _codes(response) == {"review_date": ["approval_incomplete"]}

    def test_an_approved_row_whose_date_has_come_can_still_be_opened_and_closed(
        self, curator, template, approved
    ) -> None:
        """Сохранение без правок ни о чём не судит: просроченную строку можно открыть и закрыть."""
        today = timezone.localdate()
        ProcedureContraindication.objects.filter(pk=approved.pk).update(review_date=today)

        response = _client(curator).post(
            reverse(CHANGE, args=[approved.pk]), _form(template, status="approved", review_date=today.isoformat()),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        approved.refresh_from_db()
        assert approved.status == "approved"

    def test_a_scope_named_by_a_category_is_checked_against_the_appointment(self, template, body, face) -> None:
        body_reviewer = _staff("body-2741", EVERYDAY)
        ClaimReviewer.objects.create(user=body_reviewer, claim_type="medical", category=body)
        client = _client(body_reviewer)

        foreign = client.post(reverse(ADD), _form(categories=[str(face.pk)], mark_reviewed="on"))
        own = client.post(reverse(ADD), _form(categories=[str(template.category.pk)], mark_reviewed="on"))

        assert _codes(foreign) == {"mark_reviewed": ["reviewer_competence_required"]}
        assert own.status_code == 302, own.context["adminform"].form.errors
        row = ProcedureContraindication.objects.get()
        assert (row.reviewed_by_id, list(row.categories.all())) == (body_reviewer.pk, [template.category])

    def test_an_approved_row_needs_a_scope(self, approver, reviewer) -> None:
        row = _row(**COMPLETE, reviewed_by=reviewer, reviewed_at=timezone.now())

        response = _client(approver).post(reverse(CHANGE, args=[row.pk]), _form(status="approved"))

        assert _codes(response) == {"templates": ["approval_incomplete"]}

    def test_a_curator_cannot_edit_or_delete_an_approved_row(self, curator, template, approved) -> None:
        client = _client(curator)

        edited = client.post(reverse(CHANGE, args=[approved.pk]), _form(template, condition="чужая правка"))
        deleted = client.post(reverse(DELETE, args=[approved.pk]), {"post": "yes"})

        assert _codes(edited) == {"status": ["approval_right_required"]}
        assert deleted.status_code == 403
        approved.refresh_from_db()
        assert (approved.status, approved.condition) == ("approved", "синтетическое условие")

    def test_saving_untouched_writes_nothing(self, curator, template, approved) -> None:
        before = (approved.reviewed_at, approved.confirmed_at, approved.updated_at)

        response = _client(curator).post(reverse(CHANGE, args=[approved.pk]), _form(template, status="approved"))

        assert response.status_code == 302, response.context["adminform"].form.errors
        approved.refresh_from_db()
        assert (approved.reviewed_at, approved.confirmed_at, approved.updated_at) == before


# ── Правило сброса (то же, что у остального знания) ────────────────────────


class TestAnEditDropsTheApproval:
    @pytest.mark.parametrize(
        "change",
        [
            pytest.param({"condition": "новое условие"}, id="condition"),
            pytest.param({"action": "exclude"}, id="action"),
            pytest.param({"source_ref": "DOC-2741-B"}, id="source_ref"),
            pytest.param({"evidence_source": "другой источник"}, id="evidence_source"),
            pytest.param({"review_date": (AHEAD + dt.timedelta(days=30)).isoformat()}, id="review_date"),
        ],
    )
    def test_an_edit_of_an_approved_row_returns_it_to_draft_and_is_journalled(
        self, approver, template, approved, change
    ) -> None:
        """Сохраняющий оставил «подтверждено» — строка всё равно уходит в
        черновик: правка сохранена, обе подписи сняты, куратору сказано."""
        response = _client(approver).post(
            reverse(CHANGE, args=[approved.pk]), _form(template, **{"status": "approved", **change}),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        approved.refresh_from_db()
        assert (approved.status, approved.reviewed_by_id, approved.confirmed_by_id) == (
            "system_inference", None, None,
        )
        assert "Подтверждение снято" in " ".join(str(m) for m in get_messages(response.wsgi_request))
        journalled = ClaimApprovalReset.objects.get()
        assert (journalled.reason, journalled.claim_kind, journalled.contraindication_id) == (
            "claim_edited", "contraindication", approved.pk,
        )
        assert [c["field"] for c in journalled.changes] == list(change)
        assert (journalled.was_approved, journalled.had_review) == (True, True)

    def test_an_edit_saved_as_a_draft_by_hand_is_journalled_as_a_lost_approval(
        self, approver, template, approved
    ) -> None:
        """Сохранявший сам выбрал «черновик» и тем же сохранением поправил
        условие: в журнале строка была подтверждённой — она ею и была."""
        response = _client(approver).post(
            reverse(CHANGE, args=[approved.pk]),
            _form(template, status="system_inference", condition="новое условие"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        journalled = ClaimApprovalReset.objects.get()
        assert (journalled.was_approved, journalled.had_review) == (True, True)

    def test_a_return_to_draft_without_an_edit_is_a_decision_not_a_reset(
        self, approver, template, approved, reviewer
    ) -> None:
        response = _client(approver).post(
            reverse(CHANGE, args=[approved.pk]), _form(template, status="system_inference"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        approved.refresh_from_db()
        assert (approved.status, approved.reviewed_by_id) == ("system_inference", reviewer.pk)
        assert ClaimApprovalReset.objects.count() == 0

    def test_a_change_of_the_scope_is_a_change_of_content(
        self, approver, template, face_template, approved
    ) -> None:
        response = _client(approver).post(
            reverse(CHANGE, args=[approved.pk]),
            _form(status="approved", templates=[str(template.pk), str(face_template.pk)]),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        approved.refresh_from_db()
        assert approved.status == "system_inference"
        assert ClaimApprovalReset.objects.get().changes[0]["field"] == "templates"

    def test_a_new_approval_closes_the_reset_and_empties_the_queue(
        self, approver, reviewer, template, approved
    ) -> None:
        owner_of_both = _staff("both-2741", EVERYDAY + APPROVE)
        ClaimReviewer.objects.create(user=owner_of_both, claim_type="medical")
        client = _client(owner_of_both)
        url = reverse(CHANGE, args=[approved.pk])
        edited = _form(template, condition="новое условие")

        def queue() -> list:
            page = client.get(reverse(LIST), {"needs_reconfirmation": "yes"})
            return [row.pk for row in page.context["cl"].result_list]

        def open_resets() -> int:
            return ClaimApprovalReset.objects.filter(contraindication=approved, resolved_at__isnull=True).count()

        client.post(url, {**edited, "status": "approved"})
        waiting, notice, open_before = queue(), client.get(url).content.decode(), open_resets()
        again = client.post(url, {**edited, "status": "approved", "mark_reviewed": "on"})
        after = queue()

        assert waiting == [approved.pk]
        assert "подтверждение снято" in notice
        assert again.status_code == 302, again.context["adminform"].form.errors
        assert after == []
        # Очередь пуста уже потому, что строка подтверждена; закрыта ли сама
        # запись журнала — видно только по ней.
        assert (open_before, open_resets()) == (1, 0)
        assert ClaimApprovalReset.objects.filter(contraindication=approved).count() == 1
        approved.refresh_from_db()
        assert (approved.status, approved.reviewed_by_id, approved.confirmed_by_id) == (
            "approved", owner_of_both.pk, owner_of_both.pk,
        )


class TestAChangeOfTheProcedureReachesItsContraindications:
    def test_a_significant_change_returns_the_rules_of_this_procedure_to_draft(
        self, template, approved
    ) -> None:
        template.name = "Процедура 2741, новая редакция"
        template.save()

        approved.refresh_from_db()
        assert (approved.status, approved.reviewed_by_id) == ("system_inference", None)
        assert approved.confirmed_by_id is not None  # след подтверждения остаётся
        journalled = ClaimApprovalReset.objects.get()
        assert (journalled.reason, journalled.claim_kind) == ("procedure_changed", "contraindication")
        assert journalled.changes == [
            {"field": "name", "old": "Процедура 2741", "new": "Процедура 2741, новая редакция"},
        ]

    def test_the_rules_of_neighbouring_procedures_are_untouched(
        self, template, face_template, approved, reviewer, approver
    ) -> None:
        neighbour = _row(key="neighbour", **COMPLETE, **_both_signed(reviewer, approver))
        neighbour.templates.set([face_template])

        template.contraindications = "новая оговорка"
        template.save()

        neighbour.refresh_from_db()
        approved.refresh_from_db()
        assert (approved.status, neighbour.status) == ("system_inference", "approved")

    def test_a_new_catalogue_version_alone_keeps_the_approval(self, template, approved) -> None:
        template.approval_rule_version = "canonical_catalog_2027-01.json"
        template.is_popular = not template.is_popular
        template.save()

        approved.refresh_from_db()
        assert approved.status == "approved"
        assert ClaimApprovalReset.objects.count() == 0

    def test_a_rule_scoped_by_a_category_is_reset_when_the_category_moves(
        self, template, face, reviewer, approver
    ) -> None:
        by_category = _row(key="by-category", **COMPLETE, **_both_signed(reviewer, approver))
        by_category.categories.set([template.category])

        subcategory = template.category
        subcategory.parent = face
        subcategory.save()

        by_category.refresh_from_db()
        assert by_category.status == "system_inference"

    def test_the_procedure_admin_names_the_contraindications_it_reset(self, template, approved) -> None:
        owner = User.objects.create_superuser(
            username="owner-2741", password="pw",  # pragma: allowlist secret
            email="owner-2741@example.test", role="admin",
        )
        url = reverse("admin:services_servicetemplate_change", args=[template.pk])
        client = _client(owner)
        page = client.get(url)
        form = page.context["adminform"].form
        data = {}
        for name in form.fields:
            value = form.initial.get(name)
            if value is None or value is False:  # ``0 in (None, False)`` истинно — сравнивать тождеством
                continue
            data[form.add_prefix(name)] = "on" if value is True else value
        for inline in page.context["inline_admin_formsets"]:
            prefix = inline.formset.prefix
            data.update({f"{prefix}-TOTAL_FORMS": "0", f"{prefix}-INITIAL_FORMS": "0"})

        response = client.post(url, {**data, "contraindications": "новая оговорка"})

        assert response.status_code == 302, response.context["adminform"].form.errors
        told = " ".join(str(m) for m in get_messages(response.wsgi_request))
        assert "противопоказаний: 1" in told

    def test_a_deleted_rule_leaves_its_journal_behind(self, approver, template, approved) -> None:
        template.name = "Процедура 2741, новая редакция"
        template.save()

        response = _client(approver).post(reverse(DELETE, args=[approved.pk]), {"post": "yes"})

        assert response.status_code == 302
        assert not ProcedureContraindication.objects.filter(pk=approved.pk).exists()
        journalled = ClaimApprovalReset.objects.get()
        assert (journalled.claim_kind, journalled.contraindication_id) == ("contraindication", None)
        assert journalled.claim_label.startswith("example_condition")


@pytest.fixture
def by_category(template, reviewer, approver) -> ProcedureContraindication:
    """Подтверждённое правило, область которого названа подкатегорией процедуры."""
    row = _row(key="by-category", **COMPLETE, **_both_signed(reviewer, approver))
    row.categories.set([template.category])
    return row


@pytest.fixture
def by_root(body, reviewer, approver) -> ProcedureContraindication:
    """Подтверждённое правило, область которого названа корнем «тело»."""
    row = _row(key="by-root", **COMPLETE, **_both_signed(reviewer, approver))
    row.categories.set([body])
    return row


@pytest.fixture
def by_face(face, reviewer, approver) -> ProcedureContraindication:
    """Подтверждённое правило о другой области — «лицо»."""
    row = _row(key="by-face", **COMPLETE, **_both_signed(reviewer, approver))
    row.categories.set([face])
    return row


def _statuses(*rows: ProcedureContraindication) -> list[str]:
    return [ProcedureContraindication.objects.get(pk=row.pk).status for row in rows]


class TestTheScopeChangesWithoutAnEditOfTheRow:
    """Строку никто не правил, а то, к чему она применяется, стало другим."""

    def test_a_deleted_procedure_returns_the_rules_naming_it_to_draft(self, template, approved, by_face) -> None:
        template.delete()

        assert _statuses(approved, by_face) == ["system_inference", "approved"]
        assert list(approved.templates.all()) == []
        journalled = ClaimApprovalReset.objects.get()
        assert (journalled.reason, journalled.contraindication_id) == ("scope_changed", approved.pk)
        assert journalled.changes == [{"field": "templates", "old": "Процедура 2741", "new": ""}]

    def test_a_deleted_category_returns_the_rules_naming_it_or_its_root_to_draft(
        self, body, by_root, by_face, reviewer, approver
    ) -> None:
        empty = ServiceCategory.objects.create(name="Пустая 2741", parent=body)
        named = _row(key="named", **COMPLETE, **_both_signed(reviewer, approver))
        named.categories.set([empty])

        empty.delete()

        assert _statuses(named, by_root, by_face) == ["system_inference", "system_inference", "approved"]
        fields = set(ClaimApprovalReset.objects.values_list("reason", flat=True))
        assert fields == {"scope_changed"}

    def test_a_new_procedure_in_the_category_returns_its_rules_to_draft(
        self, template, by_category, by_root, by_face, approved
    ) -> None:
        """Правило, названное категорией, молча распространилось бы на процедуру,
        которой рецензент не видел. Правило, названное процедурами поимённо, — нет."""
        new = ServiceTemplate.objects.create(category=template.category, name="Новая 2741", name_short="Новая 2741")

        assert _statuses(by_category, by_root, by_face, approved) == [
            "system_inference", "system_inference", "approved", "approved",
        ]
        assert new.knowledge_reset == {"capabilities": 0, "goal_links": 0, "contraindications": 2}
        journalled = ClaimApprovalReset.objects.filter(contraindication=by_category).get()
        assert (journalled.reason, journalled.changes) == (
            "scope_changed", [{"field": "templates", "old": "", "new": "Новая 2741"}],
        )

    def test_a_procedure_moved_to_another_category_resets_the_rules_of_both(
        self, template, face, by_category, by_root, by_face
    ) -> None:
        template.category = face
        template.save()

        assert _statuses(by_category, by_root, by_face) == ["system_inference"] * 3
        assert template.knowledge_reset["contraindications"] == 3

    def test_a_significant_change_of_a_procedure_resets_the_rules_named_by_its_category(
        self, template, by_category, by_root, by_face
    ) -> None:
        template.name = "Процедура 2741, новая редакция"
        template.save()

        assert _statuses(by_category, by_root, by_face) == ["system_inference", "system_inference", "approved"]

    def test_a_neutral_change_of_a_procedure_keeps_the_rules_named_by_its_category(
        self, template, by_category, by_root
    ) -> None:
        template.sort_order = template.sort_order + 1
        template.save()

        assert _statuses(by_category, by_root) == ["approved", "approved"]
        assert ClaimApprovalReset.objects.count() == 0

    def test_a_subcategory_moved_to_another_root_resets_the_rules_of_both_roots(
        self, template, face, by_root, by_face
    ) -> None:
        subcategory = template.category
        subcategory.parent = face
        subcategory.save()

        assert _statuses(by_root, by_face) == ["system_inference", "system_inference"]

    def test_a_rule_under_two_matching_names_is_journalled_once(
        self, template, body, reviewer, approver
    ) -> None:
        """Строка названа и процедурой, и её категорией, и корнем — в журнале одна запись."""
        row = _row(key="thrice", **COMPLETE, **_both_signed(reviewer, approver))
        row.templates.set([template])
        row.categories.set([template.category, body])

        template.name = "Процедура 2741, новая редакция"
        template.save()

        assert ClaimApprovalReset.objects.filter(contraindication=row).count() == 1

    def test_the_procedure_admin_says_so_when_a_new_procedure_enters_a_scope(self, template, by_category) -> None:
        owner = User.objects.create_superuser(
            username="owner-2741", password="pw",  # pragma: allowlist secret
            email="owner-2741@example.test", role="admin",
        )
        request = RequestFactory().post("/")
        request.user = owner
        request.session = {}
        request._messages = FallbackStorage(request)
        new = ServiceTemplate(category=template.category, name="Новая 2741", name_short="Новая 2741")

        django_admin.site._registry[ServiceTemplate].save_model(request, new, form=None, change=False)

        told = " ".join(str(m) for m in request._messages)
        assert "Новая процедура попала в область применения противопоказаний" in told
        assert "противопоказаний: 1" in told


# ── Засев из файла ─────────────────────────────────────────────────────────


def _seed(tmp_path: Path, rows: list, *args: str) -> str:
    path = tmp_path / "knowledge.json"
    path.write_text(json.dumps({"contraindications": rows}, ensure_ascii=False), encoding="utf-8")
    out = StringIO()
    call_command("seed_procedure_knowledge", "--file", str(path), *args, stdout=out)
    return out.getvalue()


def _refusal(tmp_path: Path, rows: list) -> str:
    with pytest.raises(CommandError) as raised:
        _seed(tmp_path, rows)
    assert ProcedureContraindication.objects.count() == 0
    return str(raised.value)


FILE_ROW = {
    "key": "fever", "condition": "синтетическое условие", "action": "postpone",
    "template_codes": ["1.1.3"], "source_ref": "DOC-2741", "evidence_kind": "clinical_guideline",
    "review_date": "2027-10-01",
}


class TestAFileBringsDraftsOnly:
    def test_a_row_is_seeded_as_a_draft_with_its_scope(self, template, tmp_path) -> None:
        output = _seed(tmp_path, [FILE_ROW])

        assert "Contraindications: +1, existing left as is: 0" in output
        row = ProcedureContraindication.objects.get()
        assert (row.status, row.action, row.review_date) == ("system_inference", "postpone", dt.date(2027, 10, 1))
        assert (row.reviewed_by_id, row.confirmed_by_id) == (None, None)
        assert list(row.templates.all()) == [template]

    def test_a_second_run_adds_nothing_and_keeps_the_hand_of_the_admin(self, template, tmp_path) -> None:
        _seed(tmp_path, [FILE_ROW])
        ProcedureContraindication.objects.update(condition="правка в админке")

        output = _seed(tmp_path, [FILE_ROW])

        assert "Contraindications: +0, existing left as is: 1" in output
        assert ProcedureContraindication.objects.get().condition == "правка в админке"

    @pytest.mark.parametrize("smuggled", ["status", "reviewed_by", "reviewed_at", "confirmed_by", "confirmed_at"])
    def test_a_status_or_a_signature_in_the_file_is_an_unknown_field(self, template, tmp_path, smuggled) -> None:
        assert smuggled not in FILE_KEYS
        message = _refusal(tmp_path, [{**FILE_ROW, smuggled: "approved"}])

        assert f"неизвестное поле «{smuggled}»" in message

    def test_every_problem_of_the_section_is_named_at_once(self, template, tmp_path) -> None:
        message = _refusal(tmp_path, [
            {"key": "bad", "condition": "", "action": "be_careful", "template_codes": ["9.9.9"],
             "evidence_kind": "маркетинг салона", "review_date": "2027-02-30"},
        ])

        assert "condition — не указано условие" in message
        assert "action «be_careful»" in message
        assert "код «9.9.9» не найден" in message
        assert "evidence_kind — «маркетинг салона»" in message
        assert "review_date — нужна существующая дата" in message

    @pytest.mark.parametrize("broken", [0, "", False, {}, None, "1.1.3"], ids=repr)
    def test_a_scope_that_is_not_a_list_is_named_not_read_as_no_scope(self, template, tmp_path, broken) -> None:
        message = _refusal(tmp_path, [{**FILE_ROW, "template_codes": broken}])

        assert "template_codes — ожидался список кодов процедур" in message

    def test_a_dry_run_writes_nothing(self, template, tmp_path) -> None:
        output = _seed(tmp_path, [FILE_ROW], "--dry-run")

        assert "1 contraindications" in output
        assert ProcedureContraindication.objects.count() == 0

    def test_the_example_file_seeds_its_contraindication_section(self) -> None:
        category = ServiceCategory.objects.create(name="Категория 2741-пример")
        for code in ("1.1.3", "1.1.2"):
            ServiceTemplate.objects.create(
                category=category, name=f"Процедура {code}", name_short=f"Процедура {code}", canonical_code=code,
            )
        GoalOption.objects.create(key="relax", label="Цель 2741")
        out = StringIO()

        call_command("seed_procedure_knowledge", "--file", str(EXAMPLE_FILE), stdout=out)

        assert "Contraindications: +1" in out.getvalue()
        assert ProcedureContraindication.objects.get().status == "system_inference"


# ── Чего здесь нет ─────────────────────────────────────────────────────────


class TestTheCarrierIsNotARoute:
    def test_the_client_reader_and_the_read_endpoint_do_not_know_the_table(self) -> None:
        """Носитель, а не путь ответа: клиентский читатель и внутренняя ручка
        чтения таблицу не упоминают. Появится читатель — этот узел обязан
        измениться вместе с ним."""
        for module in (capabilities, knowledge_api):
            source = Path(module.__file__).read_text(encoding="utf-8")
            assert "ProcedureCapability" in source  # контроль: это тот самый модуль
            assert "ProcedureContraindication" not in source

    def test_nothing_but_the_inputs_names_the_table_or_its_reverse_side(self) -> None:
        """Шире предыдущего: читатель мог бы прийти и не через класс, а через
        обратную сторону связи (``template.contraindication_rules``) — и из
        любого модуля. Список тех, кто таблицу упоминает, — литералом; новый
        читатель обязан появиться здесь вместе с решением о нём."""
        root = Path(services_models.__file__).resolve().parents[1]
        mentions = set()
        for path in root.rglob("*.py"):
            parts = path.relative_to(root).parts
            if parts[0].startswith(".") or {"migrations", "tests", "node_modules", "site-packages"} & set(parts):
                continue
            source = path.read_text(encoding="utf-8", errors="ignore")
            if "ProcedureContraindication" in source or "contraindication_rules" in source:
                mentions.add("/".join(parts))

        assert mentions == {
            "services/models.py",
            "services/admin.py",
            "services/contraindication_intake.py",
            "services/management/commands/seed_procedure_knowledge.py",
            "users/deletion_executor.py",
        }

    def test_both_signatures_are_decided_for_the_erasure_of_a_person(self) -> None:
        """Новые указатели на пользователя записаны в реестр удаления — иначе
        исполнитель стирания останавливается до первой записи."""
        assert undecided_pointers() == {}
