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
* в админке: отметку «проверено» ставит только держатель права рецензента,
  «подтверждено» — только держатель права подтверждения; это два действия и
  две подписи; правка содержания снимает отметку, и подтверждённой строка
  остаться не может; подтверждённая строка без области применения не
  сохраняется;
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
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from services import capabilities, knowledge_api
from services.contraindication_intake import ACTIONS, FILE_KEYS, approval_errors
from services.models import ProcedureContraindication, ServiceCategory, ServiceTemplate
from users.deletion_executor import undecided_pointers
from users.models import User

pytestmark = pytest.mark.django_db

ADD = "admin:services_procedurecontraindication_add"
CHANGE = "admin:services_procedurecontraindication_change"
DELETE = "admin:services_procedurecontraindication_delete"

EVERYDAY = [f"{action}_procedurecontraindication" for action in ("add", "change", "delete", "view")]
REVIEW = ["review_procedurecontraindication"]
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
def template() -> ServiceTemplate:
    category = ServiceCategory.objects.create(name="Категория 2741")
    return ServiceTemplate.objects.create(
        category=category, name="Процедура 2741", name_short="Процедура 2741", canonical_code="1.1.3",
    )


@pytest.fixture
def curator() -> User:
    """Ведёт черновики; ни права рецензента, ни права подтверждения."""
    return _staff("curator-2741", EVERYDAY)


@pytest.fixture
def reviewer() -> User:
    return _staff("reviewer-2741", EVERYDAY + REVIEW)


@pytest.fixture
def approver() -> User:
    return _staff("approver-2741", EVERYDAY + APPROVE)


COMPLETE = {
    "source_ref": "DOC-2741",
    "evidence_source": "синтетический источник",
    "evidence_kind": "clinical_guideline",
    "review_date": dt.date(2027, 10, 1),
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
        ) == {}

    def test_an_approved_row_is_asked_for_everything_at_once(self) -> None:
        errors = approval_errors(
            status="approved", source_ref="нет", evidence_kind="", review_date=None, has_scope=False,
        )

        assert set(errors) == {"source_ref", "evidence_kind", "review_date", "templates"}

    def test_a_kind_outside_the_list_is_refused_on_a_draft_too(self) -> None:
        errors = approval_errors(
            status="system_inference", source_ref="", evidence_kind="practice", review_date=None, has_scope=False,
        )

        assert set(errors) == {"evidence_kind"}


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
        "review_date": "2027-10-01",
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

        assert _codes(response) == {"mark_reviewed": ["reviewer_right_required"]}
        assert ProcedureContraindication.objects.count() == 0

    def test_the_approver_cannot_tick_the_review_for_the_reviewer(self, approver, template) -> None:
        response = _client(approver).post(
            reverse(ADD), _form(template, status="approved", mark_reviewed="on"),
        )

        codes = _codes(response)
        assert codes["mark_reviewed"] == ["reviewer_right_required"]
        assert codes["status"] == ["review_required"]

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

    def test_an_unreviewed_row_is_not_approved(self, approver, template) -> None:
        response = _client(approver).post(reverse(ADD), _form(template, status="approved"))

        assert _codes(response) == {"status": ["review_required"]}

    @pytest.mark.parametrize(
        ("missing", "field"),
        [
            ({"source_ref": ""}, "source_ref"),
            ({"source_ref": "нет"}, "source_ref"),
            ({"evidence_kind": ""}, "evidence_kind"),
            ({"review_date": ""}, "review_date"),
        ],
    )
    def test_approving_an_incomplete_row_is_a_field_error_not_a_database_error(
        self, reviewer, approver, template, missing, field
    ) -> None:
        row = _row(**COMPLETE, reviewed_by=reviewer, reviewed_at=timezone.now())
        row.templates.set([template])

        response = _client(approver).post(
            reverse(CHANGE, args=[row.pk]), _form(template, status="approved", **missing),
        )

        assert field in _codes(response)
        row.refresh_from_db()
        assert row.status == "system_inference"

    def test_an_approved_row_needs_a_scope(self, approver, reviewer) -> None:
        row = _row(**COMPLETE, reviewed_by=reviewer, reviewed_at=timezone.now())

        response = _client(approver).post(reverse(CHANGE, args=[row.pk]), _form(status="approved"))

        assert _codes(response) == {"templates": ["approval_incomplete"]}

    def test_an_edit_of_the_condition_drops_the_review_and_the_approval_with_it(
        self, reviewer, approver, template
    ) -> None:
        row = _row(**COMPLETE, **_both_signed(reviewer, approver))
        row.templates.set([template])
        client = _client(approver)

        kept_approved = client.post(
            reverse(CHANGE, args=[row.pk]), _form(template, status="approved", condition="новое условие"),
        )
        to_draft = client.post(
            reverse(CHANGE, args=[row.pk]), _form(template, condition="новое условие"),
        )

        assert _codes(kept_approved) == {"status": ["review_required"]}
        assert to_draft.status_code == 302, to_draft.context["adminform"].form.errors
        row.refresh_from_db()
        assert (row.status, row.reviewed_by_id, row.confirmed_by_id) == ("system_inference", None, None)
        assert any("Отметка проверки рецензентом снята" in str(m) for m in get_messages(to_draft.wsgi_request))

    def test_changing_the_scope_is_a_change_of_content(self, reviewer, approver, template) -> None:
        other = ServiceTemplate.objects.create(
            category=template.category, name="Процедура 2741-2", name_short="Процедура 2741-2",
        )
        row = _row(**COMPLETE, **_both_signed(reviewer, approver))
        row.templates.set([template])

        response = _client(approver).post(
            reverse(CHANGE, args=[row.pk]),
            _form(status="approved", templates=[str(template.pk), str(other.pk)]),
        )

        assert _codes(response) == {"status": ["review_required"]}

    def test_a_curator_cannot_edit_or_delete_an_approved_row(self, curator, reviewer, approver, template) -> None:
        row = _row(**COMPLETE, **_both_signed(reviewer, approver))
        row.templates.set([template])
        client = _client(curator)

        edited = client.post(reverse(CHANGE, args=[row.pk]), _form(template, condition="чужая правка"))
        deleted = client.post(reverse(DELETE, args=[row.pk]), {"post": "yes"})

        assert _codes(edited) == {"status": ["approval_right_required"]}
        assert deleted.status_code == 403
        row.refresh_from_db()
        assert (row.status, row.condition) == ("approved", "синтетическое условие")

    def test_saving_untouched_writes_nothing(self, curator, reviewer, approver, template) -> None:
        row = _row(**COMPLETE, **_both_signed(reviewer, approver))
        row.templates.set([template])
        before = (row.reviewed_at, row.confirmed_at, row.updated_at)

        response = _client(curator).post(reverse(CHANGE, args=[row.pk]), _form(template, status="approved"))

        assert response.status_code == 302, response.context["adminform"].form.errors
        row.refresh_from_db()
        assert (row.reviewed_at, row.confirmed_at, row.updated_at) == before


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
        from services.models import GoalOption

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

    def test_both_signatures_are_decided_for_the_erasure_of_a_person(self) -> None:
        """Новые указатели на пользователя записаны в реестр удаления — иначе
        исполнитель стирания останавливается до первой записи."""
        assert undecided_pointers() == {}
