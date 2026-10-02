"""DRF-2742 — вид доказательства выбирается из закрытого списка.

Решение владельца №4 от 02.10: ``evidence_kind`` — закрытый список из восьми
видов, а не свободный текст; маркетинг салона доказательством не является.

Что держат узлы:

* список — ровно восемь названных владельцем видов, литералом;
* подтверждённая строка без вида или с видом вне списка не хранится — база;
* входы (форма админки и засев) не принимают вид вне списка ни у какой
  строки и требуют вид у подтверждённой;
* черновик базу не интересует — иначе строка, заведённая до правила, уронила
  бы миграцию;
* строки, подтверждённые ДО правила с пустым или произвольным видом,
  миграция возвращает в черновик — на настоящей миграции.

Все тексты синтетические.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from services import knowledge_intake
from services.admin import CapabilityGoalLinkAdminForm
from services.knowledge_intake import EVIDENCE_KINDS, evidence_kind_errors
from services.migrations import _drf2742_evidence_kind_demote as evidence_kind_demote
from services.models import (
    CapabilityGoalLink,
    ClaimEvidence,
    GoalOption,
    ProcedureCapability,
    ServiceCategory,
    ServiceTemplate,
)
from users.models import User

pytestmark = pytest.mark.django_db

ADD_CAPABILITY = "admin:services_procedurecapability_add"

#: Восемь видов, названных владельцем, — литералом, а не из константы кода.
THE_EIGHT = (
    "clinical_guideline",
    "systematic_review",
    "rct",
    "manufacturer_ifu",
    "regulatory_document",
    "professional_consensus",
    "legal_rule",
    "product_policy",
)
NOT_EVIDENCE = ["маркетинг салона", "practice", "Протокол салона", "RCT"]


@pytest.fixture
def template() -> ServiceTemplate:
    category = ServiceCategory.objects.create(name="Категория 2742")
    return ServiceTemplate.objects.create(
        category=category, name="Процедура 2742", name_short="Процедура 2742", canonical_code="1.1.3",
    )


@pytest.fixture
def goal() -> GoalOption:
    return GoalOption.objects.create(key="relax", label="Цель 2742")


@pytest.fixture
def owner() -> User:
    return User.objects.create_superuser(
        username="owner-2742", password="pw",  # pragma: allowlist secret
        email="owner-2742@example.test", role="admin",
    )


@pytest.fixture
def admin_client(owner) -> Client:
    client = Client()
    client.force_login(owner)
    return client


def _signed(owner: User) -> dict:
    return {
        "status": "approved", "claim_type": "product",
        "confirmed_by": owner, "confirmed_at": timezone.now(), "source_ref": "DOC-2742",
    }


def _refused_by(constraint: str, create) -> None:
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        create()
    assert constraint in str(raised.value)


class TestTheListIsTheOwnersEight:
    def test_the_model_the_inputs_and_the_migration_know_the_same_eight(self) -> None:
        assert tuple(kind.value for kind in ClaimEvidence.EvidenceKind) == THE_EIGHT
        assert EVIDENCE_KINDS == THE_EIGHT
        assert evidence_kind_demote.KNOWN_KINDS == THE_EIGHT

    def test_salon_marketing_is_not_in_the_list(self) -> None:
        labels = " ".join(str(label) for _, label in ClaimEvidence.EvidenceKind.choices).casefold()

        assert "маркетинг" not in labels
        assert "marketing" not in " ".join(THE_EIGHT)


# ── База ───────────────────────────────────────────────────────────────────


class TestTheDatabaseKeepsApprovedOnKnownEvidence:
    @pytest.mark.parametrize("kind", THE_EIGHT)
    def test_each_of_the_eight_is_stored_on_an_approved_row(self, template, owner, kind) -> None:
        ProcedureCapability.objects.create(template=template, key="known", evidence_kind=kind, **_signed(owner))

        assert ProcedureCapability.objects.get().evidence_kind == kind

    def test_an_approved_row_without_a_kind_is_refused(self, template, owner) -> None:
        _refused_by(
            "procedurecapability_approved_evidence_kind_known",
            lambda: ProcedureCapability.objects.create(template=template, key="no-kind", **_signed(owner)),
        )

    @pytest.mark.parametrize("kind", NOT_EVIDENCE)
    def test_an_approved_row_with_a_kind_outside_the_list_is_refused(self, template, owner, kind) -> None:
        """«RCT» заглавными — тоже вне списка: значение сверяется буквально."""
        _refused_by(
            "procedurecapability_approved_evidence_kind_known",
            lambda: ProcedureCapability.objects.create(
                template=template, key="outside", evidence_kind=kind, **_signed(owner),
            ),
        )

    def test_a_draft_is_not_judged_by_the_database(self, template) -> None:
        """Условие выкладки: черновик с произвольным видом для базы законен."""
        ProcedureCapability.objects.create(template=template, key="draft", evidence_kind="маркетинг салона")
        ProcedureCapability.objects.create(template=template, key="empty-draft")

        assert ProcedureCapability.objects.count() == 2

    def test_the_goal_link_carries_the_same_rule(self, template, goal, owner) -> None:
        capability = ProcedureCapability.objects.create(template=template, key="for-link")

        _refused_by(
            "capabilitygoallink_approved_evidence_kind_known",
            lambda: CapabilityGoalLink.objects.create(capability=capability, goal=goal, **_signed(owner)),
        )
        CapabilityGoalLink.objects.create(
            capability=capability, goal=goal, evidence_kind="legal_rule", **_signed(owner),
        )
        assert CapabilityGoalLink.objects.get().evidence_kind == "legal_rule"


# ── Общая проверка входов ──────────────────────────────────────────────────


class TestTheSharedRule:
    def test_a_draft_without_a_kind_has_no_demands(self) -> None:
        assert evidence_kind_errors(status="system_inference", evidence_kind="") == {}

    @pytest.mark.parametrize("status", ["system_inference", "approved"])
    def test_a_known_kind_passes(self, status) -> None:
        assert evidence_kind_errors(status=status, evidence_kind="rct") == {}

    @pytest.mark.parametrize("status", ["system_inference", "approved"])
    def test_a_kind_outside_the_list_is_refused_on_any_row(self, status) -> None:
        errors = evidence_kind_errors(status=status, evidence_kind="маркетинг салона")

        assert set(errors) == {"evidence_kind"}
        assert "Маркетинг салона доказательством не является" in errors["evidence_kind"]

    def test_an_approved_row_needs_a_kind(self) -> None:
        assert set(evidence_kind_errors(status="approved", evidence_kind="  ")) == {"evidence_kind"}


# ── Форма админки ──────────────────────────────────────────────────────────


def _form(template: ServiceTemplate, **overrides) -> dict:
    data = {
        "template": str(template.pk),
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
        "evidence_kind": "",
        "source_ref": "DOC-2742",
    }
    data.update(overrides)
    return data


def _codes(response) -> dict[str, list[str]]:
    assert response.status_code == 200  # форма возвращена, а не сохранена
    data = response.context["adminform"].form.errors.as_data()
    return {field: [e.code for e in errs] for field, errs in data.items()}


class TestTheAdminFormOffersTheListOnly:
    def test_a_draft_without_a_kind_is_saved(self, admin_client, template) -> None:
        """Контроль: форма проходит — отказы ниже вызваны видом доказательства."""
        response = admin_client.post(reverse(ADD_CAPABILITY), _form(template))

        assert response.status_code == 302, response.context["adminform"].form.errors

    def test_an_approved_row_with_a_known_kind_is_saved(self, admin_client, template) -> None:
        response = admin_client.post(
            reverse(ADD_CAPABILITY), _form(template, status="approved", evidence_kind="manufacturer_ifu"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        row = ProcedureCapability.objects.get()
        assert (row.status, row.evidence_kind) == ("approved", "manufacturer_ifu")

    def test_approving_without_a_kind_is_a_field_error_not_a_database_error(self, admin_client, template) -> None:
        response = admin_client.post(reverse(ADD_CAPABILITY), _form(template, status="approved"))

        assert _codes(response) == {"evidence_kind": ["evidence_kind_required"]}
        assert ProcedureCapability.objects.count() == 0

    @pytest.mark.parametrize("status", ["system_inference", "approved"])
    def test_a_kind_outside_the_list_cannot_be_typed_in(self, admin_client, template, status) -> None:
        response = admin_client.post(
            reverse(ADD_CAPABILITY), _form(template, status=status, evidence_kind="маркетинг салона"),
        )

        assert _codes(response) == {"evidence_kind": ["invalid_choice"]}
        assert ProcedureCapability.objects.count() == 0

    def test_the_link_form_asks_the_same(self, template, goal) -> None:
        capability = ProcedureCapability.objects.create(template=template, key="for-link")
        data = {
            "capability": str(capability.pk), "goal": str(goal.pk),
            "course_pattern": "", "result_horizon": "", "variability_note": "",
            "claim_type": "product",
            "status": "system_inference", "claim_scope": "supported", "prohibited_statement": "",
            "limitations": "", "evidence_source": "", "evidence_kind": "practice", "source_ref": "",
        }

        outside = CapabilityGoalLinkAdminForm(data=data)
        known = CapabilityGoalLinkAdminForm(data={**data, "evidence_kind": "professional_consensus"})

        assert not outside.is_valid()
        assert set(outside.errors) == {"evidence_kind"}
        assert known.is_valid(), known.errors


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
    assert (ProcedureCapability.objects.count(), CapabilityGoalLink.objects.count()) == (0, 0)
    return str(raised.value)


class TestTheSeedKnowsTheListToo:
    def test_a_known_kind_and_no_kind_are_both_seeded_as_drafts(self, template, tmp_path) -> None:
        _seed(tmp_path, [
            {"template_code": "1.1.3", "key": "kind", "evidence_kind": "systematic_review"},
            {"template_code": "1.1.3", "key": "no-kind"},
        ])

        kinds = dict(ProcedureCapability.objects.values_list("key", "evidence_kind"))
        assert kinds == {"kind": "systematic_review", "no-kind": ""}

    def test_a_kind_outside_the_list_rejects_the_file_once(self, template, tmp_path) -> None:
        message = _refusal(tmp_path, [
            {"template_code": "1.1.3", "key": "marketing", "evidence_kind": "маркетинг салона"},
        ])

        assert "evidence_kind — «маркетинг салона» — не вид доказательства из закрытого списка" in message
        assert "1 problem(s)" in message  # одна ошибка, а не две об одном и том же

    def test_a_goal_link_with_a_kind_outside_the_list_rejects_the_file(self, template, goal, tmp_path) -> None:
        message = _refusal(tmp_path, [{
            "template_code": "1.1.3", "key": "effect",
            "goal_links": [{"goal": "relax", "evidence_kind": "practice"}],
        }])

        assert "goal_links[1]: evidence_kind — «practice»" in message

    def test_even_a_known_rule_cannot_approve_without_a_kind(self, template, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(knowledge_intake, "KNOWN_CONFIRMATION_RULES", frozenset({"owner_rule_2742"}))
        row = {
            "template_code": "1.1.3", "key": "signed", "status": "approved", "claim_type": "product",
            "claim_scope": "supported",
            "source_ref": "DOC-2742", "confirmed_rule": "owner_rule_2742", "rule_version": "1",
            "confirmed_at": "2026-10-02T09:00:00+03:00",
        }

        with_kind = _seed(tmp_path, [{**row, "evidence_kind": "legal_rule"}])
        ProcedureCapability.objects.all().delete()
        without = _refusal(tmp_path, [row])

        assert "+1 capabilities" in with_kind  # контроль: то же правило с видом подтверждает
        assert "У подтверждённого утверждения должен быть вид доказательства" in without


# ── Миграция поверх строк, лежавших до неё ─────────────────────────────────

BEFORE = "0033_drf2726_prohibited_statement"
THE_MIGRATION = "0036_drf2742_evidence_kind_closed_list"

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

    def test_approved_rows_without_a_known_kind_become_drafts_and_nothing_else_changes(self) -> None:
        old = self._migrate(BEFORE)
        Capability = old.get_model("services", "ProcedureCapability")
        Link = old.get_model("services", "CapabilityGoalLink")
        category = old.get_model("services", "ServiceCategory").objects.create(name="Категория 2742-м")
        template = old.get_model("services", "ServiceTemplate").objects.create(
            category=category, name="Процедура 2742-м", name_short="Процедура 2742-м",
        )
        goal = old.get_model("services", "GoalOption").objects.create(key="relax-2742", label="Цель")
        user = User.objects.create_user(username="mig-2742", password="x", role="admin")
        stamp = timezone.now()
        signed = {
            "status": "approved", "confirmed_by_id": user.id, "confirmed_at": stamp, "source_ref": "DOC-2742",
        }

        def capability(key: str, **fields):
            return Capability.objects.create(template=template, key=key, **fields).pk

        no_kind = capability("approved-no-kind", **signed)
        free_text = capability("approved-free-text", evidence_kind="Протокол салона", **signed)
        known = capability("approved-known", evidence_kind="rct", **signed)
        draft = capability("draft-free-text", evidence_kind="маркетинг салона")
        link = Link.objects.create(capability_id=draft, goal=goal, **signed).pk

        new = self._migrate(THE_MIGRATION)  # не падает — это и есть свойство выкладки
        ProcedureCapability = new.get_model("services", "ProcedureCapability")  # noqa: N806
        CapabilityGoalLink = new.get_model("services", "CapabilityGoalLink")  # noqa: N806

        def state(model, pk):
            row = model.objects.get(pk=pk)
            return row.status, row.evidence_kind, row.confirmed_by_id

        assert state(ProcedureCapability, no_kind) == ("system_inference", "", user.id)
        # Текст вида не тронут и не угадан: решать, что это за доказательство, — куратору.
        assert state(ProcedureCapability, free_text) == ("system_inference", "Протокол салона", user.id)
        assert state(ProcedureCapability, known) == ("approved", "rct", user.id)
        assert state(ProcedureCapability, draft) == ("system_inference", "маркетинг салона", None)
        assert state(CapabilityGoalLink, link) == ("system_inference", "", user.id)
        assert ProcedureCapability.objects.get(pk=no_kind).confirmed_at == stamp
