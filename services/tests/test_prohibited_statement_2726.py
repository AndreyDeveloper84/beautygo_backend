"""DRF-2726 (блок C) — у запрещённого утверждения есть предмет запрета.

Решение владельца 02.10: машиночитаемое правило запрета хранится в
claim-policy слое у самого утверждения — не прозой в ``limitations`` и не
отдельным списком фраз; запрещённое служит внутреннему проверяющему и
клиенту как рекомендация не передаётся.

Что держат узлы:

* предмет запрета (``prohibited_statement``) заполняется только у
  ``prohibited_claim`` — база, у любой строки;
* подтверждённый запрет без предмета не хранится — база, у подтверждённой
  строки; входы (форма и засев) спрашивают предмет у любого запрета;
* у запрета нет формулировки для клиента — входы;
* клиентский читатель запрет не отдаёт, с предметом или без;
* запрет, подтверждённый ДО правила, миграция возвращает в черновик — на
  настоящей миграции.

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

from services.admin import CapabilityGoalLinkAdminForm
from services.capabilities import (
    KnowledgeState,
    client_facing_capabilities,
    client_facing_goal_links,
)
from services.knowledge_intake import prohibition_errors
from services.models import (
    CapabilityGoalLink,
    GoalOption,
    ProcedureCapability,
    ServiceCategory,
    ServiceTemplate,
)
from users.models import User

pytestmark = pytest.mark.django_db

ADD_CAPABILITY = "admin:services_procedurecapability_add"
CHANGE_CAPABILITY = "admin:services_procedurecapability_change"
STATEMENT = "синтетическое обещание, которое нельзя произносить"


@pytest.fixture
def template() -> ServiceTemplate:
    category = ServiceCategory.objects.create(name="Категория 2726-з")
    return ServiceTemplate.objects.create(
        category=category, name="Процедура 2726-з", name_short="Процедура 2726-з", canonical_code="1.1.3",
    )


@pytest.fixture
def goal() -> GoalOption:
    return GoalOption.objects.create(key="relax", label="Цель 2726-з")


@pytest.fixture
def owner() -> User:
    return User.objects.create_superuser(
        username="owner-2726-c", password="pw",  # pragma: allowlist secret
        email="owner-2726-c@example.test", role="admin",
    )


@pytest.fixture
def admin_client(owner) -> Client:
    client = Client()
    client.force_login(owner)
    return client


def _approved(owner: User) -> dict:
    return {
        "status": "approved", "claim_type": "product", "evidence_kind": "professional_consensus",
        "confirmed_by": owner, "confirmed_at": timezone.now(),
        "source_ref": "DOC-2726",
    }


def _refused_by(constraint: str, create) -> None:
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        create()
    assert constraint in str(raised.value)


# ── База ───────────────────────────────────────────────────────────────────


class TestTheDatabaseTiesTheStatementToTheProhibition:
    def test_an_approved_prohibition_with_its_statement_is_stored(self, template, owner) -> None:
        """Контроль: подтверждённый запрет вообще сохраняется."""
        ProcedureCapability.objects.create(
            template=template, key="ban", claim_scope="prohibited_claim",
            prohibited_statement=STATEMENT, **_approved(owner),
        )

        assert ProcedureCapability.objects.get().prohibited_statement == STATEMENT

    def test_an_approved_prohibition_without_a_statement_is_refused(self, template, owner) -> None:
        _refused_by(
            "procedurecapability_approved_prohibition_has_statement",
            lambda: ProcedureCapability.objects.create(
                template=template, key="ban", claim_scope="prohibited_claim", **_approved(owner),
            ),
        )

    @pytest.mark.parametrize("scope", ["supported", "not_supported"])
    @pytest.mark.parametrize("status", ["system_inference", "approved"])
    def test_a_statement_on_a_claim_that_is_not_prohibited_is_refused(
        self, template, owner, scope, status
    ) -> None:
        """У любой строки, не только подтверждённой: текст запрещённого обещания
        не лежит у утверждения, которое может стать клиентским."""
        signed = _approved(owner) if status == "approved" else {}

        _refused_by(
            "procedurecapability_prohibition_only_on_prohibited_claim",
            lambda: ProcedureCapability.objects.create(
                template=template, key="not-a-ban", claim_scope=scope,
                prohibited_statement=STATEMENT, **signed,
            ),
        )

    def test_a_draft_prohibition_without_a_statement_is_not_judged(self, template) -> None:
        """Условие выкладки: черновик запрета без предмета для базы законен."""
        ProcedureCapability.objects.create(template=template, key="draft-ban", claim_scope="prohibited_claim")

        assert ProcedureCapability.objects.count() == 1

    def test_the_goal_link_carries_the_same_two_rules(self, template, goal, owner) -> None:
        capability = ProcedureCapability.objects.create(template=template, key="for-link")

        _refused_by(
            "capabilitygoallink_approved_prohibition_has_statement",
            lambda: CapabilityGoalLink.objects.create(
                capability=capability, goal=goal, claim_scope="prohibited_claim", **_approved(owner),
            ),
        )
        _refused_by(
            "capabilitygoallink_prohibition_only_on_prohibited_claim",
            lambda: CapabilityGoalLink.objects.create(
                capability=capability, goal=goal, claim_scope="supported", prohibited_statement=STATEMENT,
            ),
        )
        CapabilityGoalLink.objects.create(
            capability=capability, goal=goal, claim_scope="prohibited_claim",
            prohibited_statement=STATEMENT, **_approved(owner),
        )
        assert CapabilityGoalLink.objects.count() == 1


# ── Клиентский читатель ────────────────────────────────────────────────────


class TestTheProhibitionNeverReachesTheClientReader:
    def test_an_approved_prohibition_leaves_the_procedure_unknown(self, template, owner) -> None:
        ProcedureCapability.objects.create(
            template=template, key="ban", claim_scope="prohibited_claim",
            prohibited_statement=STATEMENT, **_approved(owner),
        )

        readout = client_facing_capabilities(template)

        assert readout.state is KnowledgeState.UNKNOWN
        assert readout.capabilities == ()

    def test_next_to_supported_knowledge_only_the_supported_row_is_returned(self, template, owner) -> None:
        """Контроль: читатель что-то отдаёт — и среди отданного запрета нет."""
        ProcedureCapability.objects.create(
            template=template, key="ban", claim_scope="prohibited_claim",
            prohibited_statement=STATEMENT, **_approved(owner),
        )
        ProcedureCapability.objects.create(
            template=template, key="effect", claim_scope="supported", **_approved(owner),
        )

        readout = client_facing_capabilities(template)

        assert [c.key for c in readout.capabilities] == ["effect"]

    def test_a_prohibited_goal_link_is_not_returned(self, template, goal, owner) -> None:
        capability = ProcedureCapability.objects.create(
            template=template, key="effect", claim_scope="supported", **_approved(owner),
        )
        CapabilityGoalLink.objects.create(
            capability=capability, goal=goal, claim_scope="prohibited_claim",
            prohibited_statement=STATEMENT, **_approved(owner),
        )

        assert client_facing_goal_links(capability) == ()


# ── Общая проверка входов ──────────────────────────────────────────────────


class TestTheSharedRule:
    def test_a_supported_claim_without_a_statement_has_no_demands(self) -> None:
        assert prohibition_errors(claim_scope="supported", prohibited_statement="", text_client="текст") == {}

    def test_a_prohibition_with_its_statement_and_no_client_text_passes(self) -> None:
        assert prohibition_errors(
            claim_scope="prohibited_claim", prohibited_statement=STATEMENT, text_client="",
        ) == {}

    def test_a_prohibition_needs_its_statement(self) -> None:
        errors = prohibition_errors(claim_scope="prohibited_claim", prohibited_statement="  ", text_client="")

        assert set(errors) == {"prohibited_statement"}

    def test_a_prohibition_has_no_client_wording(self) -> None:
        errors = prohibition_errors(
            claim_scope="prohibited_claim", prohibited_statement=STATEMENT, text_client="обещание",
        )

        assert set(errors) == {"text_client"}

    def test_a_statement_belongs_to_a_prohibition_only(self) -> None:
        errors = prohibition_errors(claim_scope="not_supported", prohibited_statement=STATEMENT)

        assert set(errors) == {"prohibited_statement"}


# ── Форма админки ──────────────────────────────────────────────────────────


def _capability_form(template: ServiceTemplate, **overrides) -> dict:
    data = {
        "template": str(template.pk),
        "key": "example_ban",
        "text_client": "",
        "text_professional": "",
        "expected_effect": "",
        "result_timeframe": "",
        "variability_note": "",
        "claim_type": "product",
        "status": "system_inference",
        "claim_scope": "prohibited_claim",
        "prohibited_statement": STATEMENT,
        "limitations": "",
        "evidence_source": "",
        "evidence_kind": "professional_consensus",
        "source_ref": "",
    }
    data.update(overrides)
    return data


class TestTheAdminFormAsksForTheStatement:
    def test_a_prohibition_with_its_statement_is_saved(self, admin_client, template) -> None:
        """Контроль: форма запрета проходит — отказы ниже вызваны названной причиной."""
        response = admin_client.post(reverse(ADD_CAPABILITY), _capability_form(template))

        assert response.status_code == 302, response.context["adminform"].form.errors
        assert ProcedureCapability.objects.get().prohibited_statement == STATEMENT

    @pytest.mark.parametrize("status", ["system_inference", "approved"])
    def test_a_prohibition_without_a_statement_is_refused_on_its_field(
        self, admin_client, template, status
    ) -> None:
        response = admin_client.post(
            reverse(ADD_CAPABILITY),
            _capability_form(template, prohibited_statement="  ", status=status, source_ref="DOC-2726"),
        )

        assert response.status_code == 200
        assert set(response.context["adminform"].form.errors) == {"prohibited_statement"}
        assert ProcedureCapability.objects.count() == 0

    def test_a_prohibition_with_a_client_wording_is_refused(self, admin_client, template) -> None:
        response = admin_client.post(
            reverse(ADD_CAPABILITY), _capability_form(template, text_client="обещание клиенту"),
        )

        assert response.status_code == 200
        assert set(response.context["adminform"].form.errors) == {"text_client"}
        assert ProcedureCapability.objects.count() == 0

    def test_a_statement_on_a_supported_claim_is_a_field_error_not_a_database_error(
        self, admin_client, template
    ) -> None:
        response = admin_client.post(
            reverse(ADD_CAPABILITY), _capability_form(template, claim_scope="supported"),
        )

        assert response.status_code == 200
        assert set(response.context["adminform"].form.errors) == {"prohibited_statement"}
        assert ProcedureCapability.objects.count() == 0

    def test_a_prohibition_cannot_be_turned_into_a_supported_claim_with_its_statement_left_in(
        self, admin_client, template
    ) -> None:
        """Запрет → «поддержано» с оставленным предметом запрета: текст
        запрещённого обещания оказался бы у строки, которая может стать
        клиентской. Отказ — по полю, а не ошибкой базы."""
        ban = ProcedureCapability.objects.create(
            template=template, key="example_ban", claim_scope="prohibited_claim",
            prohibited_statement=STATEMENT,
        )

        response = admin_client.post(
            reverse(CHANGE_CAPABILITY, args=[ban.pk]), _capability_form(template, claim_scope="supported"),
        )

        assert response.status_code == 200
        assert set(response.context["adminform"].form.errors) == {"prohibited_statement"}
        ban.refresh_from_db()
        assert ban.claim_scope == "prohibited_claim"

    def test_the_link_form_asks_the_same(self, template, goal) -> None:
        capability = ProcedureCapability.objects.create(template=template, key="for-link")
        data = {
            "capability": str(capability.pk), "goal": str(goal.pk),
            "course_pattern": "", "result_horizon": "", "variability_note": "",
            "claim_type": "product",
            "status": "system_inference", "claim_scope": "prohibited_claim",
            "prohibited_statement": "", "limitations": "",
            "evidence_source": "", "evidence_kind": "", "source_ref": "",
        }

        bare = CapabilityGoalLinkAdminForm(data=data)
        stated = CapabilityGoalLinkAdminForm(data={**data, "prohibited_statement": STATEMENT})

        assert not bare.is_valid()
        assert set(bare.errors) == {"prohibited_statement"}
        assert stated.is_valid(), stated.errors


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


class TestTheSeedAsksForTheStatement:
    def test_a_prohibition_with_its_statement_is_seeded_as_a_draft(self, template, tmp_path) -> None:
        _seed(tmp_path, [{
            "template_code": "1.1.3", "key": "ban", "claim_scope": "prohibited_claim",
            "prohibited_statement": STATEMENT,
        }])

        row = ProcedureCapability.objects.get()
        assert (row.status, row.prohibited_statement) == ("system_inference", STATEMENT)

    def test_a_prohibition_without_a_statement_rejects_the_file(self, template, tmp_path) -> None:
        message = _refusal(tmp_path, [
            {"template_code": "1.1.3", "key": "ban", "claim_scope": "prohibited_claim"},
        ])

        assert "prohibited_statement — У запрещённого утверждения должен быть предмет" in message

    def test_a_prohibition_with_a_client_wording_rejects_the_file(self, template, tmp_path) -> None:
        message = _refusal(tmp_path, [{
            "template_code": "1.1.3", "key": "ban", "claim_scope": "prohibited_claim",
            "prohibited_statement": STATEMENT, "text_client": "обещание клиенту",
        }])

        assert "text_client — У запрещённого утверждения нет формулировки для клиента" in message

    def test_a_statement_on_a_supported_claim_rejects_the_file(self, template, tmp_path) -> None:
        message = _refusal(tmp_path, [{
            "template_code": "1.1.3", "key": "effect", "claim_scope": "supported",
            "prohibited_statement": STATEMENT,
        }])

        assert "prohibited_statement — Предмет запрета заполняется только" in message

    def test_a_client_wording_on_a_goal_link_is_an_unknown_field_and_nothing_more(
        self, template, goal, tmp_path
    ) -> None:
        """У связи с целью формулировки для клиента нет: лишний ключ — опечатка,
        а не «у запрета не может быть формулировки»."""
        message = _refusal(tmp_path, [{
            "template_code": "1.1.3", "key": "effect",
            "goal_links": [{
                "goal": "relax", "claim_scope": "prohibited_claim",
                "prohibited_statement": STATEMENT, "text_client": "лишнее",
            }],
        }])

        assert "неизвестное поле «text_client»" in message
        assert "1 problem(s)" in message

    def test_a_prohibited_goal_link_needs_its_statement_too(self, template, goal, tmp_path) -> None:
        message = _refusal(tmp_path, [{
            "template_code": "1.1.3", "key": "effect",
            "goal_links": [{"goal": "relax", "claim_scope": "prohibited_claim"}],
        }])

        assert "goal_links[1]: prohibited_statement — У запрещённого утверждения должен быть предмет" in message


# ── Миграция поверх строк, лежавших до неё ─────────────────────────────────

BEFORE = "0032_drf2726_result_timeframe_ground"
THE_MIGRATION = "0033_drf2726_prohibited_statement"

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

        Сначала схема, потом строки: живые модели знают колонки листа графа,
        а база после теста стоит на проверяемой миграции — следующая миграция,
        добавившая колонку, иначе сломала бы уборку.
        """
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

    def test_approved_prohibitions_become_drafts_and_nothing_else_changes(self) -> None:
        old = self._migrate(BEFORE)
        Capability = old.get_model("services", "ProcedureCapability")
        Link = old.get_model("services", "CapabilityGoalLink")
        category = old.get_model("services", "ServiceCategory").objects.create(name="Категория 2726-зм")
        template = old.get_model("services", "ServiceTemplate").objects.create(
            category=category, name="Процедура 2726-зм", name_short="Процедура 2726-зм",
        )
        goal = old.get_model("services", "GoalOption").objects.create(key="relax-2726-c", label="Цель")
        user = User.objects.create_user(username="mig-2726-c", password="x", role="admin")
        stamp = timezone.now()
        signed = {
            "status": "approved", "confirmed_by_id": user.id, "confirmed_at": stamp, "source_ref": "DOC-2726",
        }

        def capability(key: str, **fields):
            return Capability.objects.create(template=template, key=key, **fields).pk

        # До миграции предмет запрета вписать было некуда: любой
        # подтверждённый запрет — нарушитель.
        approved_ban = capability(
            "approved-ban", claim_scope="prohibited_claim", limitations="проза о запрете", **signed,
        )
        approved_effect = capability("approved-effect", claim_scope="supported", **signed)
        draft_ban = capability("draft-ban", claim_scope="prohibited_claim")
        parent = Capability.objects.create(template=template, key="for-link")
        approved_ban_link = Link.objects.create(
            capability=parent, goal=goal, claim_scope="prohibited_claim", **signed,
        ).pk

        new = self._migrate(THE_MIGRATION)  # не падает — это и есть свойство выкладки
        # Читать историческими моделями ЭТОЙ миграции, а не живыми: живые
        # знают колонки более поздних миграций, которых в базе сейчас нет.
        ProcedureCapability = new.get_model("services", "ProcedureCapability")  # noqa: N806
        CapabilityGoalLink = new.get_model("services", "CapabilityGoalLink")  # noqa: N806

        def state(model, pk):
            row = model.objects.get(pk=pk)
            return row.status, row.confirmed_by_id

        assert state(ProcedureCapability, approved_ban) == ("system_inference", user.id)
        assert state(ProcedureCapability, approved_effect) == ("approved", user.id)
        assert state(ProcedureCapability, draft_ban) == ("system_inference", None)
        assert state(CapabilityGoalLink, approved_ban_link) == ("system_inference", user.id)
        # Содержимое возвращённой строки не тронуто, предмет из прозы не выведен.
        demoted = ProcedureCapability.objects.get(pk=approved_ban)
        assert (demoted.claim_scope, demoted.limitations, demoted.prohibited_statement, demoted.confirmed_at) == (
            "prohibited_claim", "проза о запрете", "", stamp,
        )
