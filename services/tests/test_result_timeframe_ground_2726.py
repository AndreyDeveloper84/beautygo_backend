"""DRF-2726 (блок B) — срок результата не хранится самостоятельным числом.

Решение владельца 02.10: ``result_timeframe`` — «не самостоятельное число без
контекста, источника и оговорки о вариативности»; защита та же, что у курса.
Того же рода поле у связи с целью — ``result_horizon``.

Два рубежа, и они намеренно разной строгости:

* **входы** (форма админки и засев из файла) спрашивают слова, оговорку,
  источник и ссылку у ЛЮБОЙ строки со сроком — как у курса;
* **база** судит только ПОДТВЕРЖДЁННУЮ строку. Черновик со сроком без оговорки
  база принимает: человеку он не говорится, а строка, заведённая до правила,
  не должна ронять миграцию при выкладке.

Все тексты синтетические.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from services.admin import CapabilityGoalLinkAdminForm
from services.knowledge_intake import timeframe_errors
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

GROUND = {
    "variability_note": "зависит от исходного состояния",
    "evidence_source": "синтетический источник",
    "source_ref": "DOC-2726",
}


@pytest.fixture
def template() -> ServiceTemplate:
    category = ServiceCategory.objects.create(name="Категория 2726")
    return ServiceTemplate.objects.create(
        category=category, name="Процедура 2726", name_short="Процедура 2726", canonical_code="1.1.3",
    )


@pytest.fixture
def goal() -> GoalOption:
    return GoalOption.objects.create(key="relax", label="Цель 2726")


@pytest.fixture
def owner() -> User:
    return User.objects.create_superuser(
        username="owner-2726", password="pw",  # pragma: allowlist secret
        email="owner-2726@example.test", role="admin",
    )


@pytest.fixture
def admin_client(owner) -> Client:
    client = Client()
    client.force_login(owner)
    return client


def _approved(owner: User) -> dict:
    return {"status": "approved", "confirmed_by": owner, "confirmed_at": timezone.now()}


def _refused_by(constraint: str, create) -> None:
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        create()
    assert constraint in str(raised.value)


# ── База: последний рубеж, только у подтверждённой строки ──────────────────


class TestTheDatabaseGuardsAnApprovedTimeframe:
    def test_approved_with_a_grounded_timeframe_is_stored(self, template, owner) -> None:
        """Контроль: подтверждённая строка со сроком вообще сохраняется."""
        ProcedureCapability.objects.create(
            template=template, key="grounded", result_timeframe="обычно в первые недели",
            **GROUND, **_approved(owner),
        )

        assert ProcedureCapability.objects.count() == 1

    @pytest.mark.parametrize("missing", ["variability_note", "evidence_source"])
    def test_approved_timeframe_without_its_ground_is_refused(self, template, owner, missing) -> None:
        ground = {**GROUND, missing: ""}

        _refused_by(
            "procedurecapability_approved_timeframe_grounded",
            lambda: ProcedureCapability.objects.create(
                template=template, key="bare", result_timeframe="обычно в первые недели",
                **ground, **_approved(owner),
            ),
        )

    @pytest.mark.parametrize("number", ["3", " 10 ", "2,5", "2.5"])
    def test_approved_timeframe_as_one_number_is_refused(self, template, owner, number) -> None:
        _refused_by(
            "procedurecapability_approved_timeframe_not_bare_number",
            lambda: ProcedureCapability.objects.create(
                template=template, key="number", result_timeframe=number,
                **GROUND, **_approved(owner),
            ),
        )

    def test_a_draft_is_not_judged_by_the_database(self, template) -> None:
        """Свойство выкладки: черновик, заведённый до правила, миграцию не роняет.

        Ограничение, добавленное миграцией, проверяет уже лежащие строки. Узел
        держит литералом то, что черновик с голым числом и без оговорки для
        базы законен, — иначе ``migrate`` под ``set -e`` остановил бы сайт.
        """
        ProcedureCapability.objects.create(template=template, key="draft", result_timeframe="3")

        assert ProcedureCapability.objects.get().status == "system_inference"

    def test_approved_without_a_timeframe_needs_no_note(self, template, owner) -> None:
        ProcedureCapability.objects.create(
            template=template, key="no-timeframe", source_ref="DOC-2726", **_approved(owner),
        )

        assert ProcedureCapability.objects.count() == 1


class TestTheDatabaseGuardsAnApprovedHorizon:
    @pytest.fixture
    def capability(self, template) -> ProcedureCapability:
        return ProcedureCapability.objects.create(template=template, key="for-link")

    def test_approved_with_a_grounded_horizon_is_stored(self, capability, goal, owner) -> None:
        CapabilityGoalLink.objects.create(
            capability=capability, goal=goal, result_horizon="обычно в первые недели",
            **GROUND, **_approved(owner),
        )

        assert CapabilityGoalLink.objects.count() == 1

    def test_approved_horizon_without_a_note_is_refused(self, capability, goal, owner) -> None:
        _refused_by(
            "capabilitygoallink_approved_horizon_grounded",
            lambda: CapabilityGoalLink.objects.create(
                capability=capability, goal=goal, result_horizon="обычно в первые недели",
                **{**GROUND, "variability_note": ""}, **_approved(owner),
            ),
        )

    def test_approved_horizon_as_one_number_is_refused(self, capability, goal, owner) -> None:
        _refused_by(
            "capabilitygoallink_approved_horizon_not_bare_number",
            lambda: CapabilityGoalLink.objects.create(
                capability=capability, goal=goal, result_horizon="3",
                **GROUND, **_approved(owner),
            ),
        )

    def test_a_draft_link_is_not_judged_by_the_database(self, capability, goal) -> None:
        CapabilityGoalLink.objects.create(capability=capability, goal=goal, result_horizon="3")

        assert CapabilityGoalLink.objects.count() == 1


# ── Общая проверка входов ──────────────────────────────────────────────────


class TestTheSharedRule:
    def test_no_timeframe_no_demands(self) -> None:
        assert timeframe_errors(
            field="result_timeframe", value="  ", variability_note="", evidence_source="", source_ref="",
        ) == {}

    def test_the_number_is_named_on_the_field_it_was_typed_into(self) -> None:
        errors = timeframe_errors(field="result_horizon", value="3", **GROUND)

        assert set(errors) == {"result_horizon"}

    def test_words_with_a_ground_pass(self) -> None:
        assert timeframe_errors(field="result_timeframe", value="обычно в первые недели", **GROUND) == {}


# ── Форма админки ──────────────────────────────────────────────────────────


def _capability_form(template: ServiceTemplate, **overrides) -> dict:
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
        "limitations": "",
        "evidence_source": "",
        "evidence_kind": "",
        "source_ref": "",
    }
    data.update(overrides)
    return data


class TestTheAdminFormAsksForTheGround:
    def test_a_capability_without_a_timeframe_is_saved(self, admin_client, template) -> None:
        """Контроль: форма без срока проходит — отказы ниже вызваны сроком."""
        response = admin_client.post(reverse(ADD_CAPABILITY), _capability_form(template))

        assert response.status_code == 302

    def test_a_timeframe_without_its_ground_is_refused_field_by_field(self, admin_client, template) -> None:
        response = admin_client.post(
            reverse(ADD_CAPABILITY),
            _capability_form(template, result_timeframe="обычно в первые недели"),
        )

        assert response.status_code == 200
        errors = response.context["adminform"].form.errors
        assert set(errors) == {"variability_note", "evidence_source", "source_ref"}
        assert ProcedureCapability.objects.count() == 0

    def test_a_timeframe_as_one_number_is_refused_on_its_own_field(self, admin_client, template) -> None:
        response = admin_client.post(
            reverse(ADD_CAPABILITY), _capability_form(template, result_timeframe="3", **GROUND),
        )

        assert response.status_code == 200
        assert set(response.context["adminform"].form.errors) == {"result_timeframe"}
        assert ProcedureCapability.objects.count() == 0

    def test_a_grounded_timeframe_is_saved_with_its_note(self, admin_client, template) -> None:
        response = admin_client.post(
            reverse(ADD_CAPABILITY),
            _capability_form(template, result_timeframe="обычно в первые недели", **GROUND),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        row = ProcedureCapability.objects.get()
        assert (row.result_timeframe, row.variability_note) == (
            "обычно в первые недели", "зависит от исходного состояния",
        )

    def test_an_old_draft_cannot_be_approved_until_its_timeframe_is_grounded(
        self, admin_client, template
    ) -> None:
        """Черновик, который база пропустила, на подтверждении упирается в форму
        — словами и по полям, а не именем ограничения."""
        draft = ProcedureCapability.objects.create(
            template=template, key="example_effect", result_timeframe="3",
        )

        response = admin_client.post(
            reverse(CHANGE_CAPABILITY, args=[draft.pk]),
            _capability_form(template, result_timeframe="3", status="approved", source_ref="DOC-2726"),
        )

        assert response.status_code == 200
        errors = response.context["adminform"].form.errors
        assert {"result_timeframe", "variability_note", "evidence_source"} <= set(errors)
        draft.refresh_from_db()
        assert draft.status == "system_inference"

    def test_the_link_form_asks_the_same_of_the_horizon(self, template, goal) -> None:
        capability = ProcedureCapability.objects.create(template=template, key="for-link")
        data = {
            "capability": str(capability.pk), "goal": str(goal.pk),
            "course_pattern": "", "result_horizon": "3", "variability_note": "",
            "status": "system_inference", "claim_scope": "supported", "limitations": "",
            "evidence_source": "", "evidence_kind": "", "source_ref": "",
        }

        bare = CapabilityGoalLinkAdminForm(data=data)
        grounded = CapabilityGoalLinkAdminForm(
            data={**data, "result_horizon": "обычно в первые недели", **GROUND},
        )

        assert not bare.is_valid()
        assert set(bare.errors) == {"result_horizon", "variability_note", "evidence_source", "source_ref"}
        assert grounded.is_valid(), grounded.errors


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


class TestTheSeedAsksForTheGround:
    def test_a_grounded_timeframe_and_horizon_are_seeded(self, template, goal, tmp_path) -> None:
        """Контроль: файл со сроком и горизонтом вообще проходит."""
        _seed(tmp_path, [{
            "template_code": "1.1.3", "key": "grounded",
            "result_timeframe": "обычно в первые недели", **GROUND,
            "goal_links": [{"goal": "relax", "result_horizon": "обычно в первые недели", **GROUND}],
        }])

        assert ProcedureCapability.objects.get().variability_note == "зависит от исходного состояния"
        assert CapabilityGoalLink.objects.get().result_horizon == "обычно в первые недели"

    def test_a_timeframe_as_one_number_rejects_the_file(self, template, tmp_path) -> None:
        message = _refusal(tmp_path, [
            {"template_code": "1.1.3", "key": "number", "result_timeframe": "3", **GROUND},
        ])

        assert "result_timeframe — Срок результата описывается словами" in message

    def test_a_timeframe_without_a_note_rejects_the_file(self, template, tmp_path) -> None:
        message = _refusal(tmp_path, [
            {"template_code": "1.1.3", "key": "no-note", "result_timeframe": "обычно в первые недели",
             "evidence_source": "синтетический источник", "source_ref": "DOC-2726"},
        ])

        assert "variability_note — У срока результата должна быть оговорка" in message

    def test_a_horizon_as_one_number_rejects_the_file(self, template, goal, tmp_path) -> None:
        message = _refusal(tmp_path, [{
            "template_code": "1.1.3", "key": "link-number",
            "goal_links": [{"goal": "relax", "result_horizon": "3", **GROUND}],
        }])

        assert "result_horizon — Срок результата описывается словами" in message
