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
* форма, собранная без пользователя, подтвердить не может;
* первое право можно выдать без оболочки — командой, которую зовёт
  ``entrypoint.sh``, учёткам из переменной окружения.

Кому дать право, решает владелец; код имён не знает. Все тексты синтетические.
"""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
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
CHANGE_LINK = "admin:services_capabilitygoallink_change"
DELETE_LINK = "admin:services_capabilitygoallink_delete"
ENTRYPOINT = Path(__file__).resolve().parents[2] / "entrypoint.sh"

EVERYDAY = [
    f"{action}_{model}"
    for model in ("procedurecapability", "capabilitygoallink")
    for action in ("add", "change", "delete", "view")
]
APPROVE = ["approve_procedurecapability", "approve_capabilitygoallink"]
#: Право продуктовых границ (DRF-2726 п.1): без него не подтвердить утверждение
#: типа ``product``, которым пользуются эти узлы. Оно есть у всех участников —
#: здесь проверяется право ПОДТВЕРЖДЕНИЯ, и отличаться должно только оно.
BOUNDARY = ["approve_claim_without_reviewer"]


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
    return _staff("editor-2726", EVERYDAY + BOUNDARY)


@pytest.fixture
def approver() -> User:
    return _staff("approver-2726", EVERYDAY + APPROVE + BOUNDARY)


def _form(template: ServiceTemplate, **overrides) -> dict:
    data = {
        "procedures": [str(template.pk)],
        "key": "example_effect",
        "text_client": "Синтетическая формулировка",
        "text_professional": "",
        "expected_effect": "",
        "result_timeframe": "",
        "variability_note": "",
        "claim_type": "product",
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


@pytest.fixture
def approved_row(template, approver) -> ProcedureCapability:
    return ProcedureCapability.objects.create(
        templates=[template], key="example_effect", text_client="Синтетическая формулировка",
        status="approved", claim_type="product", claim_scope="supported",
        evidence_kind="professional_consensus", source_ref="DOC-2726",
        confirmed_by=approver, confirmed_at=timezone.now(),
    )


def _status_error(response) -> None:
    """Отказ именно по праву подтверждения — по коду ошибки, а не по имени поля."""
    assert response.status_code == 200  # форма возвращена, а не сохранена
    form = response.context["adminform"].form
    assert set(form.errors) == {"status"}
    assert [e.code for e in form.errors.as_data()["status"]] == ["approval_right_required"]


def _link_form(capability: ProcedureCapability, goal: GoalOption, **overrides) -> dict:
    data = {
        "capability": str(capability.pk), "goal": str(goal.pk),
        "course_pattern": "", "result_horizon": "", "variability_note": "",
        "claim_type": "product",
        "status": "system_inference", "claim_scope": "supported", "prohibited_statement": "",
        "limitations": "", "evidence_source": "", "evidence_kind": "professional_consensus", "source_ref": "DOC-2726",
    }
    data.update(overrides)
    return data


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
            templates=[template], key="example_effect", text_client="Синтетическая формулировка",
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
        stamped_at, updated_at = approved_row.confirmed_at, approved_row.updated_at

        response = _client(editor).post(
            reverse(CHANGE, args=[approved_row.pk]), _form(template, status="approved"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        approved_row.refresh_from_db()
        assert (approved_row.status, approved_row.confirmed_by_id, approved_row.confirmed_at) == (
            "approved", approver.pk, stamped_at,
        )
        assert approved_row.updated_at == updated_at  # «ничего не меняет» — буквально

    def test_deleting_an_approved_row_is_refused(self, editor, approved_row) -> None:
        response = _client(editor).post(reverse(DELETE, args=[approved_row.pk]), {"post": "yes"})

        assert response.status_code == 403
        assert ProcedureCapability.objects.filter(pk=approved_row.pk).exists()

    def test_deleting_approved_rows_from_the_list_is_refused(self, editor, template, approved_row) -> None:
        client = _client(editor)
        draft = ProcedureCapability.objects.create(templates=[template], key="draft")

        def delete_from_list(pk):
            return client.post(
                reverse(LIST), {"action": "delete_selected", "_selected_action": [str(pk)], "post": "yes"},
            )

        control = delete_from_list(draft.pk)
        refused = delete_from_list(approved_row.pk)

        # Контроль: тот же запрос черновик удаляет — значит, запрос составлен верно.
        assert control.status_code == 302
        assert not ProcedureCapability.objects.filter(pk=draft.pk).exists()
        assert refused.status_code == 403
        assert ProcedureCapability.objects.filter(pk=approved_row.pk).exists()

    def test_deleting_a_draft_that_carries_an_approved_link_is_refused(
        self, editor, approver, template, goal
    ) -> None:
        """Связь удаляется каскадом вместе с возможностью — подтверждённую связь
        нельзя снести, удалив её черновую возможность."""
        draft = ProcedureCapability.objects.create(templates=[template], key="draft-with-link")
        link = CapabilityGoalLink.objects.create(
            capability=draft, goal=goal, status="approved", claim_type="product", claim_scope="supported",
            evidence_kind="professional_consensus",
            source_ref="DOC-2726", confirmed_by=approver, confirmed_at=timezone.now(),
        )

        response = _client(editor).post(reverse(DELETE, args=[draft.pk]), {"post": "yes"})

        assert response.status_code == 403
        assert CapabilityGoalLink.objects.filter(pk=link.pk).exists()

    def test_a_draft_can_still_be_deleted(self, editor, template) -> None:
        """Контроль: отказ выше вызван подтверждением, а не правом удаления."""
        draft = ProcedureCapability.objects.create(templates=[template], key="draft")

        response = _client(editor).post(reverse(DELETE, args=[draft.pk]), {"post": "yes"})

        assert response.status_code == 302
        assert not ProcedureCapability.objects.filter(pk=draft.pk).exists()

    def test_the_goal_link_is_guarded_by_its_own_right(self, editor, template, goal) -> None:
        capability = ProcedureCapability.objects.create(templates=[template], key="for-link")

        response = _client(editor).post(reverse(ADD_LINK), _link_form(capability, goal, status="approved"))

        _status_error(response)
        assert CapabilityGoalLink.objects.count() == 0

    def test_an_approved_goal_link_is_neither_edited_nor_deleted(
        self, editor, approver, template, goal
    ) -> None:
        capability = ProcedureCapability.objects.create(templates=[template], key="for-link")
        link = CapabilityGoalLink.objects.create(
            capability=capability, goal=goal, status="approved", claim_type="product", claim_scope="supported",
            evidence_kind="professional_consensus",
            source_ref="DOC-2726", confirmed_by=approver, confirmed_at=timezone.now(),
        )
        client = _client(editor)

        edited = client.post(
            reverse(CHANGE_LINK, args=[link.pk]),
            _link_form(capability, goal, status="approved", limitations="чужая правка"),
        )
        deleted = client.post(reverse(DELETE_LINK, args=[link.pk]), {"post": "yes"})

        _status_error(edited)
        assert deleted.status_code == 403
        link.refresh_from_db()
        assert (link.status, link.limitations) == ("approved", "")

    @pytest.mark.parametrize(
        ("held", "url", "kind"),
        [
            ("approve_procedurecapability", ADD_LINK, "link"),
            ("approve_capabilitygoallink", ADD, "capability"),
        ],
    )
    def test_the_right_for_one_table_does_not_open_the_other(self, template, goal, held, url, kind) -> None:
        half = _staff(f"half-{kind}-2726", EVERYDAY + BOUNDARY + [held])
        capability = ProcedureCapability.objects.create(templates=[template], key="for-link")
        data = (
            _link_form(capability, goal, status="approved")
            if kind == "link"
            else _form(template, status="approved")
        )

        response = _client(half).post(reverse(url), data)

        _status_error(response)


class TestTheApproverDoesAllOfIt:
    def test_approving_is_saved_and_signed_by_the_approver(self, approver, template) -> None:
        response = _client(approver).post(reverse(ADD), _form(template, status="approved"))

        assert response.status_code == 302, response.context["adminform"].form.errors
        row = ProcedureCapability.objects.get()
        assert (row.status, row.confirmed_by_id) == ("approved", approver.pk)

    def test_editing_and_returning_to_draft_are_allowed(self, approver, template, approved_row) -> None:
        """Править подтверждённое держатель права может. С DRF-2726 п.1 правка
        содержания сама снимает подтверждение; вернуть его — новым сохранением."""
        client = _client(approver)

        edited = client.post(
            reverse(CHANGE, args=[approved_row.pk]),
            _form(template, status="approved", text_client="Правка подтверждающего"),
        )
        approved_row.refresh_from_db()
        after_edit = (approved_row.status, approved_row.text_client)
        approved_again = client.post(
            reverse(CHANGE, args=[approved_row.pk]),
            _form(template, status="approved", text_client="Правка подтверждающего"),
        )
        returned = client.post(
            reverse(CHANGE, args=[approved_row.pk]),
            _form(template, status="system_inference", text_client="Правка подтверждающего"),
        )

        assert (edited.status_code, approved_again.status_code, returned.status_code) == (302, 302, 302)
        assert after_edit == ("system_inference", "Правка подтверждающего")
        approved_row.refresh_from_db()
        assert (approved_row.status, approved_row.confirmed_by_id) == ("system_inference", None)

    def test_deleting_an_approved_row_is_allowed(self, approver, approved_row) -> None:
        response = _client(approver).post(reverse(DELETE, args=[approved_row.pk]), {"post": "yes"})

        assert response.status_code == 302
        assert not ProcedureCapability.objects.filter(pk=approved_row.pk).exists()

    def test_the_right_given_through_a_group_works(self, editor, template) -> None:
        """Владелец раздаёт право группами — форма спрашивает ``has_perm``, а не
        личный список прав."""
        group = Group.objects.create(name="Кураторы знания 2726")
        group.permissions.set(Permission.objects.filter(codename__in=APPROVE))
        editor.groups.add(group)

        response = _client(editor).post(reverse(ADD), _form(template, status="approved"))

        assert response.status_code == 302, response.context["adminform"].form.errors
        assert ProcedureCapability.objects.get().confirmed_by_id == editor.pk


class TestAFormWithoutAPersonCannotApprove:
    def test_no_user_no_approval(self, template) -> None:
        """Форма, собранная вне админки, пользователя не несёт — и подтвердить не может."""
        draft = ProcedureCapabilityAdminForm(data=_form(template))
        approved = ProcedureCapabilityAdminForm(data=_form(template, status="approved"))

        assert draft.is_valid(), draft.errors  # контроль: сама форма проходит
        assert not approved.is_valid()
        # Без пользователя нет ни права подтверждения (``status``), ни права
        # продуктовых границ (``claim_type``, DRF-2726 п.1): оба отказа — по правам.
        codes = {field: [e.code for e in errs] for field, errs in approved.errors.as_data().items()}
        assert codes == {
            "status": ["approval_right_required"],
            "claim_type": ["product_boundary_right_required"],
        }


# ── Первое право — без оболочки ────────────────────────────────────────────


def _grant(*usernames: str) -> tuple[str, str]:
    out, err = StringIO(), StringIO()
    call_command("grant_knowledge_approval", *usernames, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


def _held(user: User) -> set[str]:
    return set(user.user_permissions.values_list("codename", flat=True))


class TestTheFirstRightIsGrantedWithoutAShell:
    def test_the_named_staff_account_gets_exactly_the_two_approval_rights(self, editor, template) -> None:
        before = _held(editor)

        out, _ = _grant("editor-2726")

        assert "granted=1" in out
        assert _held(editor) - before == set(APPROVE)
        assert not User.objects.get(pk=editor.pk).is_superuser
        # И право действует: тот же сотрудник теперь подтверждает.
        response = _client(User.objects.get(pk=editor.pk)).post(
            reverse(ADD), _form(template, status="approved"),
        )
        assert response.status_code == 302, response.context["adminform"].form.errors

    def test_without_arguments_the_names_come_from_the_environment(self, editor, monkeypatch) -> None:
        monkeypatch.setenv("KNOWLEDGE_APPROVER_USERNAMES", " editor-2726 , ")

        out, _ = _grant()

        assert "granted=1" in out
        assert set(APPROVE) <= _held(editor)

    def test_an_empty_environment_grants_nobody(self, editor, monkeypatch) -> None:
        monkeypatch.delenv("KNOWLEDGE_APPROVER_USERNAMES", raising=False)

        out, _ = _grant()

        assert "nothing to grant" in out
        assert not set(APPROVE) & _held(editor)

    def test_a_second_run_changes_nothing(self, editor) -> None:
        _grant("editor-2726")

        out, _ = _grant("editor-2726")

        assert "granted=0 already_had=1" in out

    @pytest.mark.parametrize("flaw", ["unknown", "not_staff", "inactive"])
    def test_an_account_that_cannot_use_the_right_is_skipped_not_fatal(self, flaw) -> None:
        if flaw != "unknown":
            User.objects.create_user(
                username="odd-2726", password="pw", role="admin",  # pragma: allowlist secret
                is_staff=flaw != "not_staff", is_active=flaw != "inactive",
            )

        out, err = _grant("odd-2726")

        assert "granted=0 already_had=0 skipped=1" in out
        assert "WARNING" in err
        assert "odd-2726" not in out + err  # имя учётки в журнал не пишется
        assert not Permission.objects.filter(user__username="odd-2726").exists()

    def test_the_entrypoint_calls_it_after_the_migrations_and_does_not_stop_on_failure(self) -> None:
        lines = [
            line.strip() for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

        migrate = next(i for i, line in enumerate(lines) if "manage.py migrate --noinput" in line)
        grant = next(i for i, line in enumerate(lines) if "manage.py grant_knowledge_approval" in line)
        gunicorn = next(i for i, line in enumerate(lines) if line.startswith("exec gunicorn"))
        assert migrate < grant < gunicorn
        assert re.search(r"grant_knowledge_approval\s*\|\|", lines[grant])
