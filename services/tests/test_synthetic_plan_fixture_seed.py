"""Засев синтетического тестового набора Плана (``seed_synthetic_plan_fixture``).

Решение владельца 08.10.2026: сквозную проверку механики Плана гонят на
помеченных синтетических данных. Команда — единственное место, где они
создаются. Узлы держат:

* набор создаётся целиком и в том виде, который пройдёт замки базы и
  читателей: синтетика везде, каноны вне Body Care и размечены, услуга в
  демо-салоне «связана, не подтверждена», мастер — новый, с обеими
  половинами связи с салоном и расписанием;
* структура набора та, что нужна механике: три способности на двух канонах,
  у второго канона услуг нет;
* без разрешения набор не виден ни одному читателю знания, под разрешением
  тестовой персоны — виден;
* повторный запуск ничего не создаёт и не правит; расхождение с файлом —
  остановка;
* любой отказ и пробный прогон не оставляют в базе ничего.

Пределы: свободные окна в расписании узлы не считают — проверяется, что
рабочие часы у тест-мастера заведены; запись на него здесь не создаётся.
"""

from __future__ import annotations

import copy
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from appointments.models import SpecialistWorkingHours
from services import body_care_scope, capabilities
from services.models import (
    CapabilityGoalLink, GoalOption, ProcedureCapability, SalonService, ServiceCategory, ServiceTemplate,
    SpecialistService,
)
from services.synthetic import SYNTHETIC_RULE, grant_for
from services.synthetic_fixture import DEFAULT_SPEC, SERVICE_USERNAME, FixtureRefused, load_spec, seed
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User
from users.sellable import sellable_q

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(db):
    return {
        "goal": GoalOption.objects.create(key="event", label="Собраться к событию"),
        "category": ServiceCategory.objects.create(name="Тестовая категория", slug="synthetic-seed-category"),
        "salon": Tenant.objects.create(slug="synthetic-seed-demo", name="Демо-салон", is_demo=True),
        "persona": User.objects.create_user(username="synthetic-seed-persona", password="x", is_test_persona=True),
    }


@pytest.fixture
def spec(world) -> dict:
    """Файл из репозитория с вписанными значениями стенда."""
    filled = load_spec()
    filled.update(
        category_slug=world["category"].slug, salon_id=str(world["salon"].pk),
        test_persona_id=str(world["persona"].pk),
    )
    return filled


def _census() -> dict:
    return {
        "canons": ServiceTemplate.objects.filter(synthetic=True).count(),
        "capabilities": ProcedureCapability.objects.filter(synthetic=True).count(),
        "links": CapabilityGoalLink.objects.filter(synthetic=True).count(),
        "offers": SalonService.objects.filter(synthetic=True).count(),
        "edges": SpecialistService.objects.filter(salon_service__synthetic=True).count(),
        "masters": User.objects.filter(username__startswith="synthetic-test-master").count(),
        "service_users": User.objects.filter(username=SERVICE_USERNAME).count(),
    }


NOTHING = {"canons": 0, "capabilities": 0, "links": 0, "offers": 0, "edges": 0, "masters": 0, "service_users": 0}
SEEDED = {"canons": 2, "capabilities": 3, "links": 3, "offers": 1, "edges": 1, "masters": 1, "service_users": 1}


# ─── файл из репозитория ─────────────────────────────────────────────────────


def test_the_shipped_file_has_the_structure_the_mechanics_need() -> None:
    """Три способности на двух канонах; у второго канона услуг нет — это сценарий, а не недоделка."""
    shipped = load_spec(DEFAULT_SPEC)
    by_canon: dict = {}
    for capability in shipped["capabilities"]:
        for ref in capability["templates"]:
            by_canon.setdefault(ref, []).append(capability["key"])

    assert shipped["goal_key"] == "event"
    assert sorted(len(keys) for keys in by_canon.values()) == [1, 2]
    with_offers = {offer["template"] for offer in shipped["offers"]}
    assert {ref for ref, keys in by_canon.items() if len(keys) == 2} == with_offers
    assert all(row["body_care_scope"] == "not_body_care" for row in shipped["templates"])
    assert all(row["legal_service_class"] == "non_medical_cosmetic" for row in shipped["templates"])
    assert all(row["required_practitioner_class"] is None for row in shipped["templates"])
    assert len({row["text_client"] for row in shipped["capabilities"]}) == 3


def test_the_shipped_file_refuses_to_seed_until_the_stand_values_are_filled(world) -> None:
    with pytest.raises(FixtureRefused) as refused:
        seed(load_spec(DEFAULT_SPEC))

    assert refused.value.reason == "spec_incomplete"
    assert _census() == NOTHING


# ─── что создаётся ───────────────────────────────────────────────────────────


def test_the_whole_set_is_seeded(spec, world) -> None:
    report = seed(spec)

    assert _census() == SEEDED
    assert len(report.created) == 13 and report.found == []

    look = ServiceTemplate.objects.get(name="Образ к событию: укладка и макияж (тест)")
    manicure = ServiceTemplate.objects.get(name="Маникюр к событию (тест)")
    assert sorted(look.capabilities.values_list("key", flat=True)) == [
        "synthetic_event_hair", "synthetic_event_makeup",
    ]
    assert list(manicure.capabilities.values_list("key", flat=True)) == ["synthetic_event_hands"]
    assert SalonService.objects.filter(template=manicure).count() == 0  # сценарий «нет предложений»

    offer = SalonService.objects.get(synthetic=True)
    assert (offer.tenant_id, offer.template_id) == (world["salon"].pk, look.pk)
    assert offer.mapping_status == SalonService.MappingStatus.REVIEW_REQUIRED
    assert (offer.requires_health_check, offer.health_check_origin, offer.health_check_confirmed_rule) == (
        False, "confirmed", SYNTHETIC_RULE,
    )
    assert offer.health_check_confirmed_by_id is None


@pytest.mark.parametrize("fail_closed", [False, True])
def test_the_seeded_canons_pass_the_real_scope_readers(settings, spec, fail_closed) -> None:
    settings.BODY_CARE_UNCLASSIFIED_FAIL_CLOSED = fail_closed
    seed(spec)

    for canon in ServiceTemplate.objects.filter(synthetic=True):
        facts = {"scope": canon.body_care_scope, "family": canon.service_family, "legal_class": canon.legal_service_class}
        assert body_care_scope.unenforced_checks(has_canon=True, **facts) == frozenset(), canon.name
        assert body_care_scope.scope_of(has_canon=True, scope=facts["scope"], family=facts["family"]) == (
            body_care_scope.NOT_SUBJECT
        )
        assert canon.scope_confirmed_by_id is None and canon.scope_confirmed_rule == SYNTHETIC_RULE
        assert canon.legal_class_confirmed_by.username == SERVICE_USERNAME
        assert canon.legal_class_confirmed_by.is_active is False


def test_the_test_master_is_new_sellable_and_belongs_to_the_salon_both_ways(spec, world) -> None:
    report = seed(spec)

    master = SpecialistProfile.objects.get(pk=report.master_id)
    assert master.tenant_id == world["salon"].pk
    assert SpecialistProfile.objects.filter(sellable_q(), pk=master.pk).exists()
    assert TenantUserRelationship.objects.filter(
        user=master.user, tenant=world["salon"], is_active=True, role=TenantUserRelationship.Role.STAFF,
    ).count() == 1
    assert master.user.has_usable_password() is False
    assert SpecialistWorkingHours.objects.filter(specialist=master, is_working_day=True).count() == 7
    assert SpecialistService.objects.get(salon_service__synthetic=True).specialist_id == master.pk


def test_nobody_elses_schedule_is_touched(spec, world) -> None:
    resident_user = User.objects.create_user(
        username="synthetic-seed-resident", password="x", role="specialist", tenant=world["salon"],
    )
    resident = SpecialistProfile.objects.get(user=resident_user)

    seed(spec)

    assert SpecialistWorkingHours.objects.filter(specialist=resident).count() == 0
    assert SpecialistService.objects.filter(specialist=resident).count() == 0


# ─── видимость ───────────────────────────────────────────────────────────────


def test_the_seeded_knowledge_is_invisible_without_a_grant_and_visible_under_one(settings, spec, world) -> None:
    without = seed(spec)
    assert without.visible_without_grant == []
    assert without.visible_under_grant is None and "настройки стенда" in without.grant_note
    assert list(capabilities.capability_keys_helping_goal("event")) == []

    settings.SYNTHETIC_TEST_DATA_ENABLED = True
    settings.SYNTHETIC_TEST_SUBJECT_IDS = [str(world["persona"].pk)]
    under = seed(spec)

    assert under.visible_without_grant == []
    assert under.visible_under_grant == ["synthetic_event_hair", "synthetic_event_hands", "synthetic_event_makeup"]
    grant = grant_for(world["persona"])
    labels = capabilities.capability_labels(under.visible_under_grant, include_synthetic=grant)
    assert {key: (label.label, label.synthetic) for key, label in labels.items()} == {
        "synthetic_event_hair": ("Причёска к событию", True),
        "synthetic_event_hands": ("Ухоженные руки к событию", True),
        "synthetic_event_makeup": ("Макияж к событию", True),
    }


# ─── повторный запуск и расхождения ──────────────────────────────────────────


def test_a_second_run_creates_and_changes_nothing(spec) -> None:
    first = seed(spec)
    stamps = list(ProcedureCapability.objects.filter(synthetic=True).values_list("key", "updated_at"))

    second = seed(spec)

    assert _census() == SEEDED
    assert second.created == [] and len(second.found) == len(first.created) - 1  # часы — не отдельная находка
    assert second.master_id == first.master_id
    assert list(ProcedureCapability.objects.filter(synthetic=True).values_list("key", "updated_at")) == stamps


@pytest.mark.parametrize("drift", ["capability_text", "canon_scope_rule", "offer_status", "bindings"])
def test_a_row_that_differs_from_the_file_stops_the_run(spec, drift) -> None:
    """Молча «починить» синтетическую строку нельзя: её основания и пометка неизменяемы по замыслу."""
    seed(spec)
    changed = copy.deepcopy(spec)
    if drift == "capability_text":
        ProcedureCapability.objects.filter(key="synthetic_event_hair").update(text_client="Переписано руками")
    elif drift == "canon_scope_rule":
        changed["templates"][0]["body_care_scope"] = "body_care"
    elif drift == "offer_status":
        SalonService.objects.filter(synthetic=True).update(mapping_status=SalonService.MappingStatus.UNMAPPED)
    else:
        changed["capabilities"][2]["templates"] = ["event_look"]

    with pytest.raises(FixtureRefused) as refused:
        seed(changed)

    assert refused.value.reason == "differs_from_spec"
    assert _census() == SEEDED


# ─── отказы до записи ────────────────────────────────────────────────────────


def _refusal(spec, world, case: str) -> dict:
    broken = copy.deepcopy(spec)
    if case == "goal_not_found":
        broken["goal_key"] = "no-such-goal"
    elif case == "category_not_found":
        broken["category_slug"] = "no-such-category"
    elif case == "salon_not_found":
        broken["salon_id"] = "00000000-0000-0000-0000-000000000000"
    elif case == "test_persona_not_found":
        broken["test_persona_id"] = "00000000-0000-0000-0000-000000000000"
    elif case == "not_a_test_persona":
        broken["test_persona_id"] = str(User.objects.create_user(username="synthetic-seed-plain", password="x").pk)
    elif case == "salon_is_not_demo":
        broken["salon_id"] = str(Tenant.objects.create(slug="synthetic-seed-real", name="Настоящий салон").pk)
    elif case == "unknown_template_ref":
        broken["capabilities"][0]["templates"] = ["no_such_ref"]
    elif case == "goal_has_real_knowledge":
        curator = User.objects.create_user(username="synthetic-seed-curator", password="x")
        approved = {
            "status": "approved", "claim_type": "product", "claim_scope": "supported",
            "evidence_kind": "professional_consensus", "source_ref": "DOC-real", "confirmed_by": curator,
            "confirmed_at": timezone.now(),
        }
        canon = ServiceTemplate.objects.create(category=world["category"], name="Настоящий канон", name_short="Наст")
        real = ProcedureCapability.objects.create(templates=[canon], key="real_event_effect", **approved)
        CapabilityGoalLink.objects.create(capability=real, goal=world["goal"], **approved)
    return broken


@pytest.mark.parametrize("case", [
    "goal_not_found", "category_not_found", "salon_not_found", "test_persona_not_found", "not_a_test_persona",
    "salon_is_not_demo", "unknown_template_ref", "goal_has_real_knowledge",
])
def test_a_refusal_names_its_reason_and_leaves_nothing_behind(spec, world, case) -> None:
    broken = _refusal(spec, world, case)

    with pytest.raises(FixtureRefused) as refused:
        seed(broken)

    assert refused.value.reason == case
    assert _census() == NOTHING


# ─── команда ─────────────────────────────────────────────────────────────────


def _run(tmp_path, spec, *args) -> str:
    import json

    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    out = StringIO()
    call_command("seed_synthetic_plan_fixture", "--spec", str(path), *args, stdout=out)
    return out.getvalue()


def test_a_dry_run_prints_the_plan_and_writes_nothing(tmp_path, spec) -> None:
    output = _run(tmp_path, spec, "--dry-run")

    assert "ПРОБНЫЙ ПРОГОН — в базе ничего не изменено." in output
    assert "Было бы создано (13):" in output
    assert "способность synthetic_event_hands" in output
    assert "Что видит обычный читатель знания у этой цели (без разрешения): ничего" in output
    assert _census() == NOTHING


def test_the_command_seeds_and_reports(tmp_path, spec) -> None:
    output = _run(tmp_path, spec)

    assert output.startswith("Набор засеян.")
    assert "Создано (13):" in output and "Уже было, совпадает с файлом (0):" in output
    assert _census() == SEEDED


def test_the_command_fails_loudly_and_says_nothing_was_written(tmp_path, spec) -> None:
    spec["goal_key"] = "no-such-goal"

    with pytest.raises(CommandError, match="Набор НЕ засеян, в базе ничего не изменено. Причина — goal_not_found"):
        _run(tmp_path, spec)

    assert _census() == NOTHING
