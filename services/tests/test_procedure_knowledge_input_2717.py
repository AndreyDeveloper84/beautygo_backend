"""DRF-2717 — у знания о процедурах есть путь ввода: админка и засев из файла.

Модели (:class:`ProcedureCapability`, :class:`CapabilityGoalLink`, DRF-2606) и
читатель (:func:`client_facing_capabilities`) существовали, а вписать строку
было нечем. Решение владельца 02.10: оба входа, куратор — он сам.

Что держат узлы:

* обе модели стоят в реестре админки (положительный контроль — сосед);
* «подтверждено» без источника или с заглушкой вместо него не сохраняется —
  отказ ФОРМЫ по полю, а не имя ограничения базы после нажатия; автора
  подтверждения ставит сохранение, а не поле формы;
* курс у связи с целью — словами и с основанием, отказ тоже по полям;
* **импорт — не подтверждение**: ``approved`` из файла проходит только
  правилом из известного набора, а набор сегодня пуст; файл с любым другим
  правилом отклоняется целиком;
* засев идемпотентен, существующее не трогает, по умолчанию кладёт черновик,
  а файл с ошибкой — любой — не пишет ничего и называет все ошибки сразу;
* черновик и пустота для читателя — ``UNKNOWN``; подтверждённое — ``KNOWN``.

Все тексты синтетические.
"""

from __future__ import annotations

import json
import re
from io import StringIO
from pathlib import Path

import pytest
from django.contrib import admin as django_admin
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from services import knowledge_intake
from services.admin import CapabilityGoalLinkAdminForm, ProcedureCapabilityAdmin
from services.capabilities import (
    KnowledgeState,
    client_facing_capabilities,
    client_facing_goal_links,
)
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
CHANGE_CAPABILITY = "admin:services_procedurecapability_change"
ADD_LINK = "admin:services_capabilitygoallink_add"
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
        "claim_type": "product",
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


def _write(tmp_path: Path, capabilities: list) -> Path:
    path = tmp_path / "knowledge.json"
    path.write_text(json.dumps({"capabilities": capabilities}, ensure_ascii=False), encoding="utf-8")
    return path


def _refusal(path: Path) -> str:
    """Файл отклонён, и в базе после этого пусто."""
    with pytest.raises(CommandError) as raised:
        _seed(path)
    assert (ProcedureCapability.objects.count(), CapabilityGoalLink.objects.count()) == (0, 0)
    return str(raised.value)


APPROVED_BY_RULE = {
    "template_code": "1.1.3", "key": "signed", "status": "approved", "claim_type": "product",
    "claim_scope": "supported", "source_ref": "DOC-2717",
    "confirmed_rule": "owner_rule_2717", "rule_version": "1",
    "confirmed_at": "2026-10-02T09:00:00+03:00",
}


# ── Админка ────────────────────────────────────────────────────────────────


class TestTheAdminHasTheTwoModels:
    def test_both_are_registered(self) -> None:
        registry = django_admin.site._registry
        assert ServiceTemplate in registry  # контроль: реестр вообще читается
        assert ProcedureCapability in registry
        assert CapabilityGoalLink in registry

    def test_the_key_is_typed_not_derived_from_the_text(self) -> None:
        """Решение владельца 29.09: ``key`` — не производная формулировки. Узел
        краснеет, если ключу однажды заведут автозаполнение из текста."""
        assert not ProcedureCapabilityAdmin.prepopulated_fields

    def test_the_curator_reads_names_not_uuids(self, world) -> None:
        capability = ProcedureCapability.objects.create(template=world["relaxing"], key="readable")
        link = CapabilityGoalLink.objects.create(capability=capability, goal=world["goal"])

        assert str(capability) == "Процедура 2717-А · readable"
        assert str(link) == "Процедура 2717-А · readable → Цель 2717"


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

    @pytest.mark.parametrize("placeholder", ["-", "нет", "TODO", "источник", "n/a", "..."])
    def test_a_placeholder_is_not_a_source(self, admin_client, world, placeholder) -> None:
        response = admin_client.post(
            reverse(ADD_CAPABILITY),
            _capability_form(world["relaxing"], status="approved", source_ref=placeholder),
        )

        assert response.status_code == 200
        assert "source_ref" in response.context["adminform"].form.errors
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
            reverse(CHANGE_CAPABILITY, args=[row.pk]),
            _capability_form(world["relaxing"], status="system_inference", source_ref="DOC-2717"),
        )

        assert response.status_code == 302
        row.refresh_from_db()
        assert (row.status, row.confirmed_by_id, row.confirmed_at) == ("system_inference", None, None)

    def test_an_approved_goal_link_is_signed_too(self, admin_client, owner, world) -> None:
        capability = ProcedureCapability.objects.create(template=world["relaxing"], key="for-link")

        response = admin_client.post(reverse(ADD_LINK), {
            "capability": str(capability.pk), "goal": str(world["goal"].pk),
            "course_pattern": "", "result_horizon": "", "variability_note": "",
            "claim_type": "product",
            "status": "approved", "claim_scope": "supported", "limitations": "",
            "evidence_source": "", "evidence_kind": "", "source_ref": "DOC-2717",
        })

        assert response.status_code == 302, response.context["adminform"].form.errors
        link = CapabilityGoalLink.objects.get()
        assert (link.status, link.confirmed_by_id) == ("approved", owner.pk)
        # Связь подтверждена, возможность — нет: говорить нечего (два решения).
        assert client_facing_goal_links(capability) == ()


class TestSavingWithoutAnEditKeepsTheSignature:
    """«Сохранить» на строке, которую только открыли, не меняет, кто её подтвердил."""

    @pytest.fixture
    def approved_by_rule(self, world) -> ProcedureCapability:
        return ProcedureCapability.objects.create(
            template=world["relaxing"], key="example_effect",
            text_client="Синтетическая формулировка",
            status="approved", claim_type="product", claim_scope="supported", source_ref="DOC-2717",
            confirmed_rule="owner_rule_2717", rule_version="1", confirmed_at=timezone.now(),
        )

    def test_an_untouched_save_leaves_the_rule_in_place(self, admin_client, world, approved_by_rule) -> None:
        stamped_at = approved_by_rule.confirmed_at

        response = admin_client.post(
            reverse(CHANGE_CAPABILITY, args=[approved_by_rule.pk]),
            _capability_form(world["relaxing"], status="approved", source_ref="DOC-2717"),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        approved_by_rule.refresh_from_db()
        assert (approved_by_rule.confirmed_rule, approved_by_rule.confirmed_by_id) == ("owner_rule_2717", None)
        assert approved_by_rule.confirmed_at == stamped_at

    def test_an_edit_drops_the_approval_it_was_given_under(
        self, admin_client, owner, world, approved_by_rule
    ) -> None:
        """До DRF-2726 правка подтверждённой строки переподписывала её
        сохранившим. Требование владельца: смена содержания снимает
        подтверждение — и подпись правила, и статус; подтвердить новую редакцию
        — отдельным сохранением."""
        response = admin_client.post(
            reverse(CHANGE_CAPABILITY, args=[approved_by_rule.pk]),
            _capability_form(
                world["relaxing"], status="approved", source_ref="DOC-2717",
                text_client="Новая формулировка",
            ),
        )

        assert response.status_code == 302, response.context["adminform"].form.errors
        approved_by_rule.refresh_from_db()
        row = approved_by_rule
        assert (row.status, row.text_client, row.confirmed_rule, row.confirmed_by_id) == (
            "system_inference", "Новая формулировка", "", None,
        )


class TestTheCourseIsWordsWithAGround:
    def _form(self, world, **overrides) -> CapabilityGoalLinkAdminForm:
        capability = ProcedureCapability.objects.create(template=world["relaxing"], key="for-link")
        data = {
            "capability": str(capability.pk),
            "goal": str(world["goal"].pk),
            "course_pattern": "",
            "result_horizon": "",
            "variability_note": "",
            "claim_type": "product",
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
            status="approved", claim_type="product", confirmed_by=owner, confirmed_at=timezone.now(),
            source_ref="DOC-2717", text_client="Правка куратора",
        )

        output = _seed(EXAMPLE_FILE)

        assert "+0 capabilities, +0 goal links" in output
        assert "existing left as is: 3 capabilities, 1 goal links" in output
        assert (ProcedureCapability.objects.count(), CapabilityGoalLink.objects.count()) == (3, 1)
        row = ProcedureCapability.objects.get(key="example_supported_effect")
        assert (row.status, row.text_client, row.confirmed_by_id) == ("approved", "Правка куратора", owner.pk)

    def test_without_the_curators_file_there_is_nothing_to_seed(self, world, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(seed_module, "DEFAULT_FILE", tmp_path / "procedure_knowledge.json")

        output = _seed()

        assert "nothing to seed" in output
        assert ProcedureCapability.objects.count() == 0

    def test_the_example_is_not_the_file_the_stand_seeds(self) -> None:
        """Иначе синтетика уехала бы в базу стенда при первой же выкладке."""
        assert seed_module.DEFAULT_FILE != EXAMPLE_FILE
        assert seed_module.DEFAULT_FILE.name == "procedure_knowledge.json"

    def test_a_named_file_that_is_missing_is_an_error(self, world, tmp_path) -> None:
        with pytest.raises(CommandError, match="not found"):
            _seed(tmp_path / "absent.json")

    def test_a_dry_run_writes_nothing(self, world) -> None:
        output = _seed(EXAMPLE_FILE, "--dry-run")

        assert "3 capabilities · 1 goal links" in output
        assert ProcedureCapability.objects.count() == 0

    def test_a_padded_template_code_is_still_found(self, world, tmp_path) -> None:
        _seed(_write(tmp_path, [{"template_code": " 1.1.3 ", "key": "padded"}]))

        assert ProcedureCapability.objects.get().template_id == world["relaxing"].pk


class TestAnImportIsNotAnApproval:
    """Читатель верит полю ``status``. Значит, файл не может сам себя подтвердить."""

    def test_no_rule_may_approve_knowledge_without_a_person_today(self) -> None:
        """Набор пуст НАМЕРЕННО. Добавить сюда правило — решение владельца, и этот
        узел обязан измениться вместе с ним, а не раньше."""
        assert knowledge_intake.KNOWN_CONFIRMATION_RULES == frozenset()

    def test_approved_with_any_made_up_rule_is_refused_and_nothing_is_written(
        self, world, tmp_path
    ) -> None:
        message = _refusal(_write(tmp_path, [
            {"template_code": "1.1.2", "key": "fine", "claim_scope": "supported"},
            APPROVED_BY_RULE,
        ]))

        assert "«owner_rule_2717» не входит в набор правил" in message
        # Годная строка рядом тоже не записана — _refusal сверил пустую базу.

    def test_approved_without_any_rule_is_refused(self, world, tmp_path) -> None:
        row = {k: v for k, v in APPROVED_BY_RULE.items() if k not in ("confirmed_rule", "rule_version")}

        assert "confirmed_rule" in _refusal(_write(tmp_path, [row]))

    def test_a_rule_the_owner_named_does_approve(self, world, tmp_path, monkeypatch) -> None:
        """Положительный контроль: с известным правилом та же строка проходит —
        значит, отказ выше вызван именно неизвестным правилом."""
        monkeypatch.setattr(knowledge_intake, "KNOWN_CONFIRMATION_RULES", frozenset({"owner_rule_2717"}))

        _seed(_write(tmp_path, [APPROVED_BY_RULE]))

        row = ProcedureCapability.objects.get()
        assert (row.status, row.confirmed_rule, row.confirmed_by_id) == ("approved", "owner_rule_2717", None)
        assert client_facing_capabilities(world["relaxing"]).state is KnowledgeState.KNOWN

    def test_even_a_known_rule_needs_a_real_looking_source(self, world, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(knowledge_intake, "KNOWN_CONFIRMATION_RULES", frozenset({"owner_rule_2717"}))

        message = _refusal(_write(tmp_path, [{**APPROVED_BY_RULE, "source_ref": "нет"}]))

        assert "source_ref" in message


class TestABrokenFileWritesNothingAndSaysWhy:
    def test_every_problem_of_the_file_is_named_at_once(self, world, tmp_path) -> None:
        message = _refusal(_write(tmp_path, [
            {"template_code": "9.9.9", "key": "no-such-template"},
            {"template_code": "1.1.3", "key": "bad-links", "goal_links": [
                {"goal": "no-such-goal"},
                {"goal": "relax", "course_pattern": "10"},
            ]},
        ]))

        assert "«9.9.9» не найден" in message
        assert "«no-such-goal» не найдена" in message
        assert "course_pattern" in message

    def test_a_value_of_the_wrong_type_is_named_not_silently_dropped(self, world, tmp_path) -> None:
        """``"status": ["approved"]`` не должен молча стать черновиком."""
        message = _refusal(_write(tmp_path, [
            {"template_code": "1.1.3", "key": "typed", "status": ["approved"], "source_ref": 42},
        ]))

        assert "status — ожидалась строка" in message
        assert "source_ref — ожидалась строка" in message

    def test_a_misspelt_field_is_named(self, world, tmp_path) -> None:
        message = _refusal(_write(tmp_path, [
            {"template_code": "1.1.3", "key": "typo", "claim_scop": "supported"},
        ]))

        assert "неизвестное поле «claim_scop»" in message

    def test_a_date_that_does_not_exist_is_a_file_error_not_a_crash(self, world, tmp_path) -> None:
        message = _refusal(_write(tmp_path, [
            {"template_code": "1.1.3", "key": "dated", "valid_until": "2026-02-30T10:00:00+03:00"},
        ]))

        assert "valid_until" in message

    @pytest.mark.parametrize("key", ["не slug", "x" * 65])
    def test_a_key_the_model_would_refuse_is_refused_before_writing(self, world, tmp_path, key) -> None:
        message = _refusal(_write(tmp_path, [{"template_code": "1.1.3", "key": key}]))

        assert "key —" in message

    def test_a_row_that_is_not_an_object_is_named(self, world, tmp_path) -> None:
        assert "ожидался объект" in _refusal(_write(tmp_path, ["just a string"]))


class TestTheCuratorsFileMustSeed:
    def test_the_curators_file_seeds_on_the_canonical_catalog(self) -> None:
        """Засев на стенде идёт при старте, и его сбой там почти не виден. Поэтому
        негодный файл должен остановиться здесь, в CI.

        Пока куратор файл не положил, узел пропускается. В тот день, когда файл
        появится в репозитории, узел начинает исполняться сам: файл обязан
        пройти засев на эталонном каталоге и на засеянных целях.
        """
        if not seed_module.DEFAULT_FILE.exists():
            pytest.skip("файла куратора (services/seeds/procedure_knowledge.json) ещё нет")

        from services.canonical_code import bootstrap_canonical_codes

        call_command("seed_canonical_catalog", stdout=StringIO())
        # Коды эталонного справочника засев не ставит — их ставит bootstrap
        # миграции 0024 тем шаблонам, что уже были в базе. На стенде они есть;
        # здесь каталог засеян ПОСЛЕ миграций, поэтому тот же шаг делается явно.
        bootstrap_canonical_codes(ServiceTemplate)
        call_command("seed_goal_options", stdout=StringIO())

        first = _seed()
        second = _seed()

        assert "Procedure knowledge seeded" in first
        assert "+0 capabilities, +0 goal links" in second


class TestTheStandGetsTheSeedWithoutAShell:
    def test_the_entrypoint_runs_the_seed_after_the_migrations(self) -> None:
        lines = [
            line.strip() for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")  # закомментированное не считается
        ]

        migrate = next(i for i, line in enumerate(lines) if "manage.py migrate --noinput" in line)
        seed = next(i for i, line in enumerate(lines) if "manage.py seed_procedure_knowledge" in line)
        gunicorn = next(i for i, line in enumerate(lines) if line.startswith("exec gunicorn"))
        assert migrate < seed < gunicorn
        # Сбой засева не должен останавливать сайт: под `set -e` это держит `||`.
        assert re.search(r"seed_procedure_knowledge\s*\|\|", lines[seed])
