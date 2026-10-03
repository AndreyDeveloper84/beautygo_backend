"""DRF-2743 — общий словарь возможностей (решение владельца №5 от 02.10).

Одно знание не копируется вручную в десятки строк: запись возможности
существует один раз (``key`` уникален сам по себе) и привязана к нескольким
процедурам канона.

Что держат узлы:

* в базе: два раза один ``key`` не хранится; процедуру с привязанным знанием
  удалить нельзя — как до словаря;
* клиентский читатель отдаёт запись для КАЖДОЙ привязанной процедуры и только
  для них, с тем же идентификатором;
* привязка — часть содержания: добавить или убрать процедуру у подтверждённой
  записи — правка, запись уходит в черновик, в журнале — имена процедур было →
  стало;
* рецензент должен быть вправе проверять по каждой привязанной процедуре — и
  у самой записи, и у её связи с целью;
* значимая правка ЛЮБОЙ привязанной процедуры возвращает запись в черновик;
* файл засева: ``template_codes`` — список, старый ``template_code`` —
  список из одного; существующую запись файл не трогает и процедур ей не
  добавляет;
* миграция ``0040`` на строках, лежавших до неё: одинаковое сводится,
  расходящееся переименовывается и уходит в черновик, ничего не теряется.

Все тексты синтетические.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models import ProtectedError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from services.capabilities import client_facing_capabilities
from services.models import (
    CapabilityGoalLink,
    ClaimApprovalReset,
    ClaimReviewer,
    GoalOption,
    ProcedureCapability,
    ServiceCategory,
    ServiceTemplate,
)
from users.models import User

ADD = "admin:services_procedurecapability_add"
CHANGE = "admin:services_procedurecapability_change"
ADD_LINK = "admin:services_capabilitygoallink_add"

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
def body() -> ServiceCategory:
    return ServiceCategory.objects.create(name="Тело 2743")


@pytest.fixture
def face() -> ServiceCategory:
    return ServiceCategory.objects.create(name="Лицо 2743")


@pytest.fixture
def massage(body) -> ServiceTemplate:
    sub = ServiceCategory.objects.create(name="Массаж 2743", parent=body)
    return ServiceTemplate.objects.create(
        category=sub, name="Массаж спины 2743", name_short="Массаж спины 2743", canonical_code="1.1.3",
    )


@pytest.fixture
def wrap(body) -> ServiceTemplate:
    return ServiceTemplate.objects.create(
        category=body, name="Обёртывание 2743", name_short="Обёртывание 2743", canonical_code="1.1.4",
    )


@pytest.fixture
def peel(face) -> ServiceTemplate:
    return ServiceTemplate.objects.create(
        category=face, name="Пилинг 2743", name_short="Пилинг 2743", canonical_code="2.1.1",
    )


@pytest.fixture
def owner() -> User:
    return User.objects.create_user(username="owner-2743", password="pw", role="admin")  # pragma: allowlist secret


def _approved(owner: User) -> dict:
    return {
        "status": "approved", "claim_type": "product", "claim_scope": "supported",
        "evidence_kind": "product_policy", "source_ref": "DOC-2743",
        "confirmed_by": owner, "confirmed_at": timezone.now(), "text_client": "Синтетика",
    }


def _form(*templates: ServiceTemplate, **overrides) -> dict:
    data = {
        "procedures": [str(t.pk) for t in templates],
        "key": "shared_relaxation",
        "text_client": "Синтетика",
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
        "evidence_kind": "product_policy",
        "source_ref": "DOC-2743",
    }
    data.update(overrides)
    return data


def _codes(response) -> dict[str, list[str]]:
    assert response.status_code == 200  # форма возвращена, а не сохранена
    data = response.context["adminform"].form.errors.as_data()
    return {field: [e.code for e in errs] for field, errs in data.items()}


# ── База ───────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestOneEntryForManyProcedures:
    def test_an_entry_is_bound_to_several_procedures(self, massage, wrap) -> None:
        entry = ProcedureCapability.objects.create(templates=[massage, wrap], key="shared")

        assert set(entry.templates.all()) == {massage, wrap}
        assert set(massage.capabilities.all()) == {entry}

    def test_a_key_is_stored_once_whatever_the_procedure(self, massage, wrap) -> None:
        ProcedureCapability.objects.create(templates=[massage], key="shared")

        with pytest.raises(IntegrityError), transaction.atomic():
            ProcedureCapability.objects.create(templates=[wrap], key="shared")

    def test_a_procedure_with_bound_knowledge_cannot_be_deleted(self, massage) -> None:
        ProcedureCapability.objects.create(templates=[massage], key="shared")

        with pytest.raises(ProtectedError):
            massage.delete()

    def test_the_curator_reads_every_procedure_in_the_name(self, massage, wrap) -> None:
        entry = ProcedureCapability.objects.create(templates=[wrap, massage], key="shared")

        assert str(entry) == "Массаж спины 2743, Обёртывание 2743 · shared"


@pytest.mark.django_db
class TestTheClientReaderSeesTheEntryForEachProcedure:
    def test_the_same_entry_for_every_bound_procedure_and_none_for_others(
        self, massage, wrap, peel, owner
    ) -> None:
        entry = ProcedureCapability.objects.create(templates=[massage, wrap], key="shared", **_approved(owner))

        assert client_facing_capabilities(massage).capabilities == (entry,)
        assert client_facing_capabilities(wrap).capabilities == (entry,)
        assert client_facing_capabilities(peel).capabilities == ()


# ── Админка ────────────────────────────────────────────────────────────────


@pytest.fixture
def holder() -> User:
    """Подтверждает и держит продуктовые границы."""
    return _staff("holder-2743", EVERYDAY + APPROVE + BOUNDARY)


@pytest.mark.django_db
class TestTheBindingIsContent:
    def test_an_entry_is_created_with_several_procedures(self, holder, massage, wrap) -> None:
        response = _client(holder).post(reverse(ADD), _form(massage, wrap))

        assert response.status_code == 302, response.context["adminform"].form.errors
        assert set(ProcedureCapability.objects.get().templates.all()) == {massage, wrap}

    def test_an_entry_needs_at_least_one_procedure(self, holder) -> None:
        response = _client(holder).post(reverse(ADD), _form())

        assert _codes(response) == {"procedures": ["required"]}

    @pytest.mark.parametrize("change", ["add", "remove"])
    def test_a_changed_binding_returns_an_approved_entry_to_draft(
        self, holder, owner, massage, wrap, change
    ) -> None:
        bound = [massage] if change == "add" else [massage, wrap]
        entry = ProcedureCapability.objects.create(templates=bound, key="shared_relaxation", **_approved(owner))
        posted = [massage, wrap] if change == "add" else [massage]

        response = _client(holder).post(
            reverse(CHANGE, args=[entry.pk]), _form(*posted, status="approved"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        entry.refresh_from_db()
        assert entry.status == "system_inference"
        assert set(entry.templates.all()) == set(posted)
        journalled = ClaimApprovalReset.objects.get(capability=entry)
        assert journalled.changes == [{
            "field": "procedures",
            "old": ", ".join(sorted(t.name for t in bound)),
            "new": ", ".join(sorted(t.name for t in posted)),
        }]

    def test_an_untouched_binding_keeps_the_approval(self, holder, owner, massage, wrap) -> None:
        entry = ProcedureCapability.objects.create(
            templates=[massage, wrap], key="shared_relaxation", **_approved(owner),
        )

        response = _client(holder).post(reverse(CHANGE, args=[entry.pk]), _form(wrap, massage, status="approved"))

        assert response.status_code == 302, response.context["adminform"].form.errors
        entry.refresh_from_db()
        assert entry.status == "approved"
        assert ClaimApprovalReset.objects.count() == 0


@pytest.mark.django_db
class TestTheReviewerCoversEveryProcedure:
    @pytest.fixture
    def body_reviewer(self, body) -> User:
        user = _staff("body-2743", EVERYDAY)
        ClaimReviewer.objects.create(user=user, claim_type="medical", category=body)
        return user

    def test_a_reviewer_of_one_area_does_not_review_an_entry_bound_to_two(
        self, body_reviewer, massage, wrap, peel
    ) -> None:
        client = _client(body_reviewer)

        both = client.post(reverse(ADD), _form(massage, peel, claim_type="medical", mark_reviewed="on"))
        own = client.post(reverse(ADD), _form(massage, wrap, claim_type="medical", mark_reviewed="on"))

        assert _codes(both) == {"mark_reviewed": ["reviewer_competence_required"]}
        assert own.status_code == 302, own.context["adminform"].form.errors
        assert ProcedureCapability.objects.get().reviewed_by_id == body_reviewer.pk

    def test_the_link_of_an_entry_is_reviewed_only_by_who_covers_all_its_procedures(
        self, body_reviewer, massage, peel
    ) -> None:
        goal = GoalOption.objects.create(key="relax-2743", label="Цель 2743")
        narrow = ProcedureCapability.objects.create(templates=[massage], key="narrow")
        wide = ProcedureCapability.objects.create(templates=[massage, peel], key="wide")
        client = _client(body_reviewer)

        def link(capability) -> dict:
            return {
                "capability": str(capability.pk), "goal": str(goal.pk), "course_pattern": "",
                "result_horizon": "", "variability_note": "", "claim_type": "medical",
                "status": "system_inference", "claim_scope": "supported", "prohibited_statement": "",
                "limitations": "", "evidence_source": "", "evidence_kind": "professional_consensus",
                "source_ref": "DOC-2743", "mark_reviewed": "on",
            }

        refused = client.post(reverse(ADD_LINK), link(wide))
        accepted = client.post(reverse(ADD_LINK), link(narrow))

        assert _codes(refused) == {"mark_reviewed": ["reviewer_competence_required"]}
        assert accepted.status_code == 302, accepted.context["adminform"].form.errors


@pytest.mark.django_db
class TestAChangeOfAnyBoundProcedure:
    def test_returns_the_entry_and_its_links_to_draft(self, owner, massage, wrap, peel) -> None:
        goal = GoalOption.objects.create(key="relax-2743", label="Цель 2743")
        entry = ProcedureCapability.objects.create(templates=[massage, wrap], key="shared", **_approved(owner))
        link = CapabilityGoalLink.objects.create(
            capability=entry, goal=goal, **{k: v for k, v in _approved(owner).items() if k != "text_client"},
        )
        elsewhere = ProcedureCapability.objects.create(templates=[peel], key="elsewhere", **_approved(owner))

        wrap.name = "Обёртывание 2743, новая редакция"
        wrap.save()

        assert ProcedureCapability.objects.get(pk=entry.pk).status == "system_inference"
        assert CapabilityGoalLink.objects.get(pk=link.pk).status == "system_inference"
        assert ProcedureCapability.objects.get(pk=elsewhere.pk).status == "approved"
        assert ClaimApprovalReset.objects.filter(capability=entry).count() == 1


# ── Засев из файла ─────────────────────────────────────────────────────────


def _seed(tmp_path: Path, rows: list) -> str:
    path = tmp_path / "knowledge.json"
    path.write_text(json.dumps({"capabilities": rows}, ensure_ascii=False), encoding="utf-8")
    out = StringIO()
    call_command("seed_procedure_knowledge", "--file", str(path), stdout=out)
    return out.getvalue()


def _refusal(tmp_path: Path, rows: list) -> str:
    with pytest.raises(CommandError) as raised:
        _seed(tmp_path, rows)
    assert ProcedureCapability.objects.count() == 0
    return str(raised.value)


@pytest.mark.django_db
class TestAFileBindsAnEntryToItsProcedures:
    def test_a_list_of_codes_binds_one_entry(self, tmp_path, massage, wrap) -> None:
        _seed(tmp_path, [{"template_codes": ["1.1.3", "1.1.4"], "key": "shared"}])

        assert set(ProcedureCapability.objects.get(key="shared").templates.all()) == {massage, wrap}

    def test_the_old_single_code_still_works_as_a_list_of_one(self, tmp_path, massage) -> None:
        _seed(tmp_path, [{"template_code": "1.1.3", "key": "shared"}])

        assert list(ProcedureCapability.objects.get(key="shared").templates.all()) == [massage]

    @pytest.mark.parametrize(
        ("row", "named"),
        [
            ({"template_code": "1.1.3", "template_codes": ["1.1.4"]}, "либо template_codes"),
            ({"template_codes": []}, "не указан ни один код"),
            ({"template_codes": "1.1.3"}, "ожидался список кодов"),
            ({"template_codes": ["9.9.9"]}, "«9.9.9» не найден"),
            ({}, "не указан ни один код"),
            ({"template_code": 113}, "template_code — ожидалась строка"),
        ],
        ids=["both", "empty", "not-a-list", "unknown", "absent", "not-a-string"],
    )
    def test_a_broken_binding_is_named_and_nothing_is_written(self, tmp_path, massage, wrap, row, named) -> None:
        assert named in _refusal(tmp_path, [{"key": "shared", **row}])

    def test_one_key_twice_in_a_file_is_refused(self, tmp_path, massage, wrap) -> None:
        message = _refusal(tmp_path, [
            {"template_codes": ["1.1.3"], "key": "shared"},
            {"template_codes": ["1.1.4"], "key": "shared"},
        ])

        assert "такой key в файле уже есть" in message

    def test_an_existing_entry_gets_no_new_procedures_from_the_file(self, tmp_path, massage, wrap) -> None:
        ProcedureCapability.objects.create(templates=[massage], key="shared", text_client="правка в админке")

        output = _seed(tmp_path, [{"template_codes": ["1.1.3", "1.1.4"], "key": "shared", "text_client": "из файла"}])

        entry = ProcedureCapability.objects.get(key="shared")
        assert "existing left as is: 1 capabilities" in output
        assert (list(entry.templates.all()), entry.text_client) == ([massage], "правка в админке")


# ── Миграция 0040 на строках, лежавших до неё ─────────────────────────────

BEFORE = "0039_drf2741_contraindication_rules"
THE_MIGRATION = "0040_drf2743_capability_dictionary"


@pytest.mark.django_db(transaction=True)
class TestTheMigrationOnRowsThatWereThereBefore:
    @pytest.fixture(autouse=True)
    def _restore_services_schema(self):
        """Вернуть ``services`` на лист графа: DDL здесь коммитится в тестовую базу."""
        yield
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("services"))
        ClaimApprovalReset.objects.all().delete()
        CapabilityGoalLink.objects.all().delete()
        ProcedureCapability.objects.all().delete()

    @staticmethod
    def _migrate(target: str):
        executor = MigrationExecutor(connection)
        executor.migrate([("services", target)])
        return executor.loader.project_state(("services", target)).apps

    def test_same_keys_are_merged_only_when_identical_and_renamed_otherwise(self) -> None:
        old = self._migrate(BEFORE)
        Capability = old.get_model("services", "ProcedureCapability")
        Link = old.get_model("services", "CapabilityGoalLink")
        Reset = old.get_model("services", "ClaimApprovalReset")
        category = old.get_model("services", "ServiceCategory").objects.create(name="Категория 2743-м")
        Template = old.get_model("services", "ServiceTemplate")
        first, second, third, fourth, fifth = (
            Template.objects.create(category=category, name=f"Процедура {n}", name_short=f"П {n}", canonical_code=code)
            for n, code in ((1, "1.1.1"), (2, "1.1.2"), (3, "1.1.3"), (4, "1.1.4"), (5, None))
        )
        Goal = old.get_model("services", "GoalOption")
        relax, calm = Goal.objects.create(key="relax-2743-m", label="Цель 1"), Goal.objects.create(
            key="calm-2743-m", label="Цель 2",
        )
        user = User.objects.create_user(username="mig-2743", password="x", role="admin")
        stamp = timezone.now()
        signed = {
            "status": "approved", "claim_type": "product", "claim_scope": "supported",
            "evidence_kind": "product_policy", "source_ref": "DOC-2743", "confirmed_by_id": user.id,
            "confirmed_at": stamp, "text_client": "Одинаковая формулировка",
        }
        alone = Capability.objects.create(template=first, key="alone", **signed).pk
        same_a = Capability.objects.create(template=first, key="same", **signed).pk
        same_b = Capability.objects.create(template=second, key="same", **signed).pk
        Link.objects.create(capability_id=same_a, goal=relax)
        moved_link = Link.objects.create(capability_id=same_b, goal=calm).pk
        moved_reset = Reset.objects.create(
            reason="procedure_changed", claim_kind="capability", claim_label="same", capability_id=same_b,
        ).pk
        differ_a = Capability.objects.create(template=first, key="differ", **signed).pk
        differ_b = Capability.objects.create(
            template=third, key="differ", **{**signed, "text_client": "Другая формулировка"},
        ).pk
        differ_c = Capability.objects.create(template=fifth, key="differ", **signed).pk
        clash_a = Capability.objects.create(template=first, key="clash", **signed).pk
        clash_b = Capability.objects.create(template=fourth, key="clash", **signed).pk
        Link.objects.create(capability_id=clash_a, goal=relax)
        Link.objects.create(capability_id=clash_b, goal=relax)

        new = self._migrate(THE_MIGRATION)  # не падает — это и есть свойство выкладки
        Entry = new.get_model("services", "ProcedureCapability")  # noqa: N806
        NewLink = new.get_model("services", "CapabilityGoalLink")  # noqa: N806
        NewReset = new.get_model("services", "ClaimApprovalReset")  # noqa: N806

        def procedures(pk) -> set[str]:
            return set(Entry.objects.get(pk=pk).templates.values_list("name", flat=True))

        def state(pk):
            row = Entry.objects.get(pk=pk)
            return row.key, row.status, row.confirmed_by_id

        # Одна строка — одна запись со своей процедурой.
        assert procedures(alone) == {"Процедура 1"}
        assert state(alone) == ("alone", "approved", user.id)
        # Одинаковое — сведено: одна запись, две процедуры, связь и журнал перешли.
        assert not Entry.objects.filter(pk=same_b).exists()
        assert procedures(same_a) == {"Процедура 1", "Процедура 2"}
        assert state(same_a) == ("same", "approved", user.id)
        assert NewLink.objects.get(pk=moved_link).capability_id == same_a
        assert NewReset.objects.get(pk=moved_reset).capability_id == same_a
        # Расходящееся — отдельные записи, ключи с кодом процедуры, все в черновике, след цел.
        assert state(differ_a) == ("differ", "system_inference", user.id)
        assert state(differ_b) == ("differ-1-1-3", "system_inference", user.id)
        assert Entry.objects.get(pk=differ_c).key == f"differ-{fifth.pk.hex[:8]}"
        assert Entry.objects.get(pk=differ_c).status == "system_inference"
        assert procedures(differ_b) == {"Процедура 3"}
        # Одинаковое содержание, но связи с одной целью — не сводится.
        assert state(clash_a) == ("clash", "system_inference", user.id)
        assert state(clash_b) == ("clash-1-1-4", "system_inference", user.id)
        assert Entry.objects.count() == 7
