"""DRF-2726 (блок A) — право подтверждать знание отдельно от права его изменять.

Решение владельца 02.10: «обычный сотрудник с админкой — не вправе»
подтверждать. До этого листа подтвердить мог любой, у кого есть право
изменения двух таблиц знания.

Что держат узлы:

* у обеих таблиц есть своё право ``approve_<модель>``;
* сотрудник с правом изменения, но без права подтверждения, ведёт черновики
  — и не может: сохранить строку подтверждённой, изменить подтверждённую,
  вернуть её в черновик, удалить её (по одной и списком);
* сохранение подтверждённой строки без правок право не требует и ничего не
  меняет;
* человек с правом подтверждения делает всё это, и подпись — его;
* форма, собранная без пользователя, подтвердить не может.

Кому дать право, решает владелец; код имён не знает. Все тексты синтетические.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from services.admin import ProcedureCapabilityAdminForm
from services.models import (
    CapabilityGoalLink,
    GoalOption,
    ProcedureCapability,
    ServiceCategory,
    ServiceTemplate,
)
from users.models import User

pytestmark = pytest.mark.django_db

ADD = "admin:services_procedurecapability_add"
CHANGE = "admin:services_procedurecapability_change"
DELETE = "admin:services_procedurecapability_delete"
LIST = "admin:services_procedurecapability_changelist"
ADD_LINK = "admin:services_capabilitygoallink_add"

EVERYDAY = [
    f"{action}_{model}"
    for model in ("procedurecapability", "capabilitygoallink")
    for action in ("add", "change", "delete", "view")
]
APPROVE = ["approve_procedurecapability", "approve_capabilitygoallink"]


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
    category = ServiceCategory.objects.create(name="Категория 2726-п")
    return ServiceTemplate.objects.create(
        category=category, name="Процедура 2726-п", name_short="Процедура 2726-п", canonical_code="1.1.3",
    )


@pytest.fixture
def goal() -> GoalOption:
    return GoalOption.objects.create(key="relax", label="Цель 2726-п")


@pytest.fixture
def editor() -> User:
    """Сотрудник с обычными правами на обе таблицы — без права подтверждения."""
    return _staff("editor-2726", EVERYDAY)


@pytest.fixture
def approver() -> User:
    return _staff("approver-2726", EVERYDAY + APPROVE)


def _form(template: ServiceTemplate, **overrides) -> dict:
    data = {
        "template": str(template.pk),
        "key": "example_effect",
        "text_client": "Синтетическая формулировка",
        "text_professional": "",
        "expected_effect": "",
        "result_timeframe": "",
        "variability_note": "",
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


@pytest.fixture
def approved_row(template, approver) -> ProcedureCapability:
    return ProcedureCapability.objects.create(
        template=template, key="example_effect", text_client="Синтетическая формулировка",
        status="approved", claim_scope="supported", source_ref="DOC-2726",
        confirmed_by=approver, confirmed_at=timezone.now(),
    )


def _status_error(response) -> None:
    assert response.status_code == 200  # форма возвращена, а не сохранена
    assert set(response.context["adminform"].form.errors) == {"status"}


class TestTheRightExists:
    def test_both_tables_have_their_own_approval_permission(self) -> None:
        codenames = set(
            Permission.objects.filter(content_type__app_label="services").values_list("codename", flat=True)
        )

        assert "change_procedurecapability" in codenames  # контроль: права таблицы вообще заведены
        assert {"approve_procedurecapability", "approve_capabilitygoallink"} <= codenames


class TestAnEditorKeepsDraftsOnly:
    def test_a_draft_is_saved_without_the_right(self, editor, template) -> None:
        """Контроль: у сотрудника всё работает, пока он не подтверждает."""
        response = _client(editor).post(reverse(ADD), _form(template))

        assert response.status_code == 302, response.context["adminform"].form.errors
        assert ProcedureCapability.objects.get().status == "system_inference"

    def test_approving_is_refused(self, editor, template) -> None:
        response = _client(editor).post(reverse(ADD), _form(template, status="approved"))

        _status_error(response)
        assert ProcedureCapability.objects.count() == 0

    def test_approving_an_existing_draft_is_refused(self, editor, template) -> None:
        draft = ProcedureCapability.objects.create(
            template=template, key="example_effect", text_client="Синтетическая формулировка",
            source_ref="DOC-2726", claim_scope="supported",
        )

        response = _client(editor).post(reverse(CHANGE, args=[draft.pk]), _form(template, status="approved"))

        _status_error(response)
        draft.refresh_from_db()
        assert (draft.status, draft.confirmed_by_id) == ("system_inference", None)

    def test_editing_an_approved_row_is_refused(self, editor, template, approved_row, approver) -> None:
        response = _client(editor).post(
            reverse(CHANGE, args=[approved_row.pk]),
            _form(template, status="approved", text_client="Чужая правка"),
        )

        _status_error(response)
        approved_row.refresh_from_db()
        assert (approved_row.text_client, approved_row.confirmed_by_id) == (
            "Синтетическая формулировка", approver.pk,
        )

    def test_returning_an_approved_row_to_draft_is_refused(self, editor, template, approved_row) -> None:
        response = _client(editor).post(
            reverse(CHANGE, args=[approved_row.pk]), _form(template, status="system_inference"),
        )

        _status_error(response)
        approved_row.refresh_from_db()
        assert approved_row.status == "approved"

    def test_saving_an_approved_row_untouched_needs_no_right_and_changes_nothing(
        self, editor, template, approved_row, approver
    ) -> None:
        stamped_at = approved_row.confirmed_at

        response = _client(editor).post(
            reverse(CHANGE, args=[approved_row.pk]), _form(template, status="approved"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        approved_row.refresh_from_db()
        assert (approved_row.status, approved_row.confirmed_by_id, approved_row.confirmed_at) == (
            "approved", approver.pk, stamped_at,
        )

    def test_deleting_an_approved_row_is_refused(self, editor, approved_row) -> None:
        response = _client(editor).post(reverse(DELETE, args=[approved_row.pk]), {"post": "yes"})

        assert response.status_code == 403
        assert ProcedureCapability.objects.filter(pk=approved_row.pk).exists()

    def test_deleting_approved_rows_from_the_list_is_refused(self, editor, approved_row) -> None:
        _client(editor).post(
            reverse(LIST),
            {"action": "delete_selected", "_selected_action": [str(approved_row.pk)], "post": "yes"},
        )

        assert ProcedureCapability.objects.filter(pk=approved_row.pk).exists()

    def test_a_draft_can_still_be_deleted(self, editor, template) -> None:
        """Контроль: отказ выше вызван подтверждением, а не правом удаления."""
        draft = ProcedureCapability.objects.create(template=template, key="draft")

        response = _client(editor).post(reverse(DELETE, args=[draft.pk]), {"post": "yes"})

        assert response.status_code == 302
        assert not ProcedureCapability.objects.filter(pk=draft.pk).exists()

    def test_the_goal_link_is_guarded_by_its_own_right(self, editor, template, goal) -> None:
        capability = ProcedureCapability.objects.create(template=template, key="for-link")

        response = _client(editor).post(reverse(ADD_LINK), {
            "capability": str(capability.pk), "goal": str(goal.pk),
            "course_pattern": "", "result_horizon": "", "variability_note": "",
            "status": "approved", "claim_scope": "supported", "prohibited_statement": "",
            "limitations": "", "evidence_source": "", "evidence_kind": "", "source_ref": "DOC-2726",
        })

        _status_error(response)
        assert CapabilityGoalLink.objects.count() == 0

    def test_the_right_for_one_table_does_not_open_the_other(self, template, goal) -> None:
        only_capabilities = _staff("half-2726", EVERYDAY + ["approve_procedurecapability"])
        capability = ProcedureCapability.objects.create(template=template, key="for-link")

        response = _client(only_capabilities).post(reverse(ADD_LINK), {
            "capability": str(capability.pk), "goal": str(goal.pk),
            "course_pattern": "", "result_horizon": "", "variability_note": "",
            "status": "approved", "claim_scope": "supported", "prohibited_statement": "",
            "limitations": "", "evidence_source": "", "evidence_kind": "", "source_ref": "DOC-2726",
        })

        _status_error(response)


class TestTheApproverDoesAllOfIt:
    def test_approving_is_saved_and_signed_by_the_approver(self, approver, template) -> None:
        response = _client(approver).post(reverse(ADD), _form(template, status="approved"))

        assert response.status_code == 302, response.context["adminform"].form.errors
        row = ProcedureCapability.objects.get()
        assert (row.status, row.confirmed_by_id) == ("approved", approver.pk)

    def test_editing_and_returning_to_draft_are_allowed(self, approver, template, approved_row) -> None:
        client = _client(approver)

        edited = client.post(
            reverse(CHANGE, args=[approved_row.pk]),
            _form(template, status="approved", text_client="Правка подтверждающего"),
        )
        returned = client.post(
            reverse(CHANGE, args=[approved_row.pk]),
            _form(template, status="system_inference", text_client="Правка подтверждающего"),
        )

        assert (edited.status_code, returned.status_code) == (302, 302)
        approved_row.refresh_from_db()
        assert (approved_row.status, approved_row.text_client) == ("system_inference", "Правка подтверждающего")

    def test_deleting_an_approved_row_is_allowed(self, approver, approved_row) -> None:
        response = _client(approver).post(reverse(DELETE, args=[approved_row.pk]), {"post": "yes"})

        assert response.status_code == 302
        assert not ProcedureCapability.objects.filter(pk=approved_row.pk).exists()


class TestAFormWithoutAPersonCannotApprove:
    def test_no_user_no_approval(self, template) -> None:
        """Форма, собранная вне админки, пользователя не несёт — и подтвердить не может."""
        draft = ProcedureCapabilityAdminForm(data=_form(template))
        approved = ProcedureCapabilityAdminForm(data=_form(template, status="approved"))

        assert draft.is_valid(), draft.errors  # контроль: сама форма проходит
        assert not approved.is_valid()
        assert set(approved.errors) == {"status"}
