"""DRF-2717 — у знания о процедурах есть путь ввода: админка и засев из файла.

Модели (:class:`ProcedureCapability`, :class:`CapabilityGoalLink`, DRF-2606) и
читатель (:func:`client_facing_capabilities`) существовали, а вписать строку
было нечем. Решение владельца 02.10: оба входа, куратор — он сам.

Что держат узлы:

* обе модели стоят в реестре админки (положительный контроль — сосед);
* «подтверждено» без источника не сохраняется — отказ ФОРМЫ по полю, а не имя
  ограничения базы после нажатия; автора подтверждения ставит сохранение, а не
  поле формы;
* курс у связи с целью — словами и с основанием, отказ тоже по полям;
* засев из файла идемпотентен, существующее не трогает и по умолчанию кладёт
  черновик; файл с ошибкой не пишет ничего;
* черновик и пустота для читателя — ``UNKNOWN``; подтверждённое — ``KNOWN``.

Все тексты синтетические.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from django.contrib import admin as django_admin
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from services.admin import CapabilityGoalLinkAdminForm, ProcedureCapabilityAdmin
from services.capabilities import KnowledgeState, client_facing_capabilities
from services.management.commands import seed_procedure_knowledge as seed_module
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
EXAMPLE_FILE = Path(seed_module.__file__).resolve().parents[2] / "seeds" / "procedure_knowledge.example.json"
ENTRYPOINT = Path(seed_module.__file__).resolve().parents[3] / "entrypoint.sh"


def _template(name: str, code: str) -> ServiceTemplate:
    category = ServiceCategory.objects.get_or_create(name="Категория 2717")[0]
    return ServiceTemplate.objects.create(
        category=category, name=name, name_short=name[:40], canonical_code=code,
    )


@pytest.fixture
def world():
    """Два шаблона и цель — то, на что ссылается пример-файл."""
    return {
        "relaxing": _template("Процедура 2717-А", "1.1.3"),
        "general": _template("Процедура 2717-Б", "1.1.2"),
        "goal": GoalOption.objects.create(key="relax", label="Цель 2717"),
    }


@pytest.fixture
def owner():
    return User.objects.create_superuser(
        username="owner-2717", password="pw",  # pragma: allowlist secret
        email="owner-2717@example.test", role="admin",
    )


@pytest.fixture
def admin_client(owner):
    client = Client()
    client.force_login(owner)
    return client


def _capability_form(template: ServiceTemplate, **overrides) -> dict:
    data = {
        "template": str(template.pk),
        "key": "example_effect",
        "text_client": "Синтетическая формулировка",
        "text_professional": "",
        "expected_effect": "",
        "result_timeframe": "",
        "status": "system_inference",
        "claim_scope": "supported",
        "limitations": "",
        "evidence_source": "",
        "evidence_kind": "",
        "source_ref": "",
    }
    data.update(overrides)
    return data


def _seed(path: Path | None = None, *args: str) -> str:
    out = StringIO()
    argv = list(args)
    if path is not None:
        argv = ["--file", str(path), *argv]
    call_command("seed_procedure_knowledge", *argv, stdout=out)
    return out.getvalue()


def _write(tmp_path: Path, capabilities: list[dict]) -> Path:
    path = tmp_path / "knowledge.json"
    path.write_text(json.dumps({"capabilities": capabilities}, ensure_ascii=False), encoding="utf-8")
    return path


# ── Админка ────────────────────────────────────────────────────────────────


class TestTheAdminHasTheTwoModels:
    def test_both_are_registered(self) -> None:
        registry = django_admin.site._registry
        assert ServiceTemplate in registry  # контроль: реестр вообще читается
        assert ProcedureCapability in registry
        assert CapabilityGoalLink in registry

    def test_the_key_is_typed_not_derived_from_the_text(self) -> None:
        """Решение владельца 29.09: ``key`` — не производная формулировки."""
        assert not ProcedureCapabilityAdmin.prepopulated_fields


class TestApprovingNeedsASource:
    def test_approved_without_a_source_is_refused_by_the_form(self, admin_client, world) -> None:
        response = admin_client.post(
            reverse(ADD_CAPABILITY),
            _capability_form(world["relaxing"], status="approved"),
        )

        assert response.status_code == 200  # форма возвращена, а не сохранена
        errors = response.context["adminform"].form.errors
        assert "source_ref" in errors
        assert ProcedureCapability.objects.count() == 0

    def test_approved_with_a_source_is_saved_and_signed_by_the_person_who_saved(
        self, admin_client, owner, world
    ) -> None:
        response = admin_client.post(
            reverse(ADD_CAPABILITY),
            _capability_form(world["relaxing"], status="approved", source_ref="DOC-2717"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        row = ProcedureCapability.objects.get()
        assert row.status == "approved"
        assert row.confirmed_by_id == owner.pk
        assert row.confirmed_at is not None
        readout = client_facing_capabilities(world["relaxing"])
        assert readout.state is KnowledgeState.KNOWN
        assert [c.key for c in readout.capabilities] == ["example_effect"]

    def test_the_confirmer_cannot_be_typed_into_the_form(self, admin_client, owner, world) -> None:
        """Подтверждение — действие, а не поле: чужое имя в запросе не едет."""
        somebody = User.objects.create_user(
            username="somebody-2717", password="pw", role="admin",  # pragma: allowlist secret
        )

        admin_client.post(
            reverse(ADD_CAPABILITY),
            _capability_form(
                world["relaxing"],
                status="approved",
                source_ref="DOC-2717",
                confirmed_by=str(somebody.pk),
                confirmed_rule="typed-by-hand",
            ),
        )

        row = ProcedureCapability.objects.get()
        assert row.confirmed_by_id == owner.pk
        assert row.confirmed_rule == ""

    def test_a_draft_is_saved_unsigned_and_the_reader_says_unknown(self, admin_client, world) -> None:
        response = admin_client.post(reverse(ADD_CAPABILITY), _capability_form(world["relaxing"]))

        assert response.status_code == 302
        row = ProcedureCapability.objects.get()
        assert (row.status, row.confirmed_by_id, row.confirmed_at) == ("system_inference", None, None)
        assert client_facing_capabilities(world["relaxing"]).state is KnowledgeState.UNKNOWN

    def test_returning_to_draft_takes_the_signature_off(self, admin_client, world) -> None:
        admin_client.post(
            reverse(ADD_CAPABILITY),
            _capability_form(world["relaxing"], status="approved", source_ref="DOC-2717"),
        )
        row = ProcedureCapability.objects.get()
        assert row.confirmed_by_id is not None

        response = admin_client.post(
            reverse("admin:services_procedurecapability_change", args=[row.pk]),
            _capability_form(world["relaxing"], status="system_inference", source_ref="DOC-2717"),
        )

        assert response.status_code == 302
        row.refresh_from_db()
        assert (row.status, row.confirmed_by_id, row.confirmed_at) == ("system_inference", None, None)


class TestTheCourseIsWordsWithAGround:
    def _form(self, world, **overrides) -> CapabilityGoalLinkAdminForm:
        capability = ProcedureCapability.objects.create(template=world["relaxing"], key="for-link")
        data = {
            "capability": str(capability.pk),
            "goal": str(world["goal"].pk),
            "course_pattern": "",
            "result_horizon": "",
            "variability_note": "",
            "status": "system_inference",
            "claim_scope": "supported",
            "limitations": "",
            "evidence_source": "",
            "evidence_kind": "",
            "source_ref": "",
        }
        data.update(overrides)
        return CapabilityGoalLinkAdminForm(data=data)

    def test_a_link_without_a_course_is_valid(self, world) -> None:
        assert self._form(world).is_valid()  # контроль: форма вообще проходит

    def test_a_bare_number_is_refused(self, world) -> None:
        form = self._form(
            world, course_pattern="10", variability_note="оговорка",
            evidence_source="источник", source_ref="DOC-2717",
        )

        assert not form.is_valid()
        assert set(form.errors) == {"course_pattern"}

    def test_a_course_without_its_ground_is_refused_field_by_field(self, world) -> None:
        form = self._form(world, course_pattern="обычно рассматривается как курс сеансов")

        assert not form.is_valid()
        assert set(form.errors) == {"variability_note", "evidence_source", "source_ref"}

    def test_a_course_with_its_ground_is_valid(self, world) -> None:
        form = self._form(
            world, course_pattern="обычно рассматривается как курс сеансов",
            variability_note="зависит от исходного состояния",
            evidence_source="источник", source_ref="DOC-2717",
        )

        assert form.is_valid(), form.errors


# ── Засев из файла ─────────────────────────────────────────────────────────


class TestSeedingFromTheCuratorsFile:
    def test_the_example_file_seeds_drafts_only(self, world) -> None:
        output = _seed(EXAMPLE_FILE)

        assert "+3 capabilities, +1 goal links" in output
        assert set(ProcedureCapability.objects.values_list("status", flat=True)) == {"system_inference"}
        assert set(CapabilityGoalLink.objects.values_list("status", flat=True)) == {"system_inference"}
        # Черновик — не знание: читатель по-прежнему отвечает «неизвестно».
        assert client_facing_capabilities(world["relaxing"]).state is KnowledgeState.UNKNOWN

    def test_a_second_run_adds_nothing_and_keeps_the_curators_hand(self, world, owner) -> None:
        _seed(EXAMPLE_FILE)
        ProcedureCapability.objects.filter(key="example_supported_effect").update(
            status="approved", confirmed_by=owner, confirmed_at=timezone.now(),
            source_ref="DOC-2717", text_client="Правка куратора",
        )

        output = _seed(EXAMPLE_FILE)

        assert "+0 capabilities, +0 goal links" in output
        assert "existing left as is: 3 capabilities, 1 goal links" in output
        assert (ProcedureCapability.objects.count(), CapabilityGoalLink.objects.count()) == (3, 1)
        row = ProcedureCapability.objects.get(key="example_supported_effect")
        assert (row.status, row.text_client, row.confirmed_by_id) == ("approved", "Правка куратора", owner.pk)

    def test_without_the_curators_file_there_is_nothing_to_seed(self, world) -> None:
        """Файл по умолчанию кладёт куратор. Пример им не является — иначе
        синтетика уехала бы в базу стенда при первой же выкладке."""
        assert not seed_module.DEFAULT_FILE.exists()
        assert seed_module.DEFAULT_FILE != EXAMPLE_FILE

        output = _seed()

        assert "nothing to seed" in output
        assert ProcedureCapability.objects.count() == 0

    def test_a_named_file_that_is_missing_is_an_error(self, world, tmp_path) -> None:
        with pytest.raises(CommandError, match="not found"):
            _seed(tmp_path / "absent.json")

    def test_a_dry_run_writes_nothing(self, world) -> None:
        output = _seed(EXAMPLE_FILE, "--dry-run")

        assert "3 capabilities · 1 goal links" in output
        assert ProcedureCapability.objects.count() == 0

    def test_approved_from_a_file_needs_a_named_rule_and_writes_nothing_otherwise(
        self, world, tmp_path
    ) -> None:
        path = _write(tmp_path, [
            {"template_code": "1.1.3", "key": "fine", "claim_scope": "supported"},
            {"template_code": "1.1.2", "key": "unsigned", "status": "approved",
             "claim_scope": "supported", "source_ref": "DOC-2717"},
        ])

        with pytest.raises(CommandError) as raised:
            _seed(path)

        assert "confirmed_rule" in str(raised.value)
        assert ProcedureCapability.objects.count() == 0  # и годная строка тоже не записана

    def test_approved_from_a_file_with_its_rule_is_known_to_the_reader(self, world, tmp_path) -> None:
        path = _write(tmp_path, [{
            "template_code": "1.1.3", "key": "signed", "status": "approved",
            "claim_scope": "supported", "source_ref": "DOC-2717",
            "confirmed_rule": "owner_knowledge_file", "rule_version": "2026-10-02",
            "confirmed_at": "2026-10-02T09:00:00+03:00",
        }])

        _seed(path)

        row = ProcedureCapability.objects.get()
        assert (row.status, row.confirmed_rule, row.confirmed_by_id) == ("approved", "owner_knowledge_file", None)
        assert client_facing_capabilities(world["relaxing"]).state is KnowledgeState.KNOWN

    def test_every_problem_of_the_file_is_named_at_once(self, world, tmp_path) -> None:
        path = _write(tmp_path, [
            {"template_code": "9.9.9", "key": "no-such-template"},
            {"template_code": "1.1.3", "key": "bad-links", "goal_links": [
                {"goal": "no-such-goal"},
                {"goal": "relax", "course_pattern": "10"},
            ]},
        ])

        with pytest.raises(CommandError) as raised:
            _seed(path)

        message = str(raised.value)
        assert "«9.9.9» не найден" in message
        assert "«no-such-goal» не найдена" in message
        assert "course_pattern" in message
        assert (ProcedureCapability.objects.count(), CapabilityGoalLink.objects.count()) == (0, 0)


class TestTheStandGetsTheSeedWithoutAShell:
    def test_the_entrypoint_runs_the_seed_after_the_migrations(self) -> None:
        script = ENTRYPOINT.read_text(encoding="utf-8")

        migrate = script.index("manage.py migrate --noinput")
        seed = script.index("manage.py seed_procedure_knowledge")
        gunicorn = script.index("exec gunicorn")
        assert migrate < seed < gunicorn
