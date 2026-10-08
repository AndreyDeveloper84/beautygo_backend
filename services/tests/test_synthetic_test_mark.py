"""Пометка «синтетика»: тестовые данные, которые не становятся настоящими.

Решение владельца 08.10.2026 (сквозная проверка Плана на подготовленных
данных). Требования, которые держат узлы:

* сборка читает подтверждённое + помеченное, а не «любое неподтверждённое»;
* пометка никогда не превращается в подтверждённое знание и подтверждённую
  связь с каноном — замками базы, включая запись мимо модели;
* синтетика читается только под ДВУМЯ факторами: флаг стенда и явный запрос;
* синтетическое привязано только к синтетическому — настоящие предложения
  кандидатами синтетического шага не становятся.

Что узлы НЕ держат: допуск синтетического предложения в подбор (ветка в
``users.admission``) и пометку на самом плане — это соседние окна.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from services import capabilities
from services.models import (
    CapabilityGoalLink, CapabilityTemplate, GoalOption, ProcedureCapability, SalonService, ServiceCategory,
    ServiceTemplate, SpecialistService,
)
from services.synthetic import (
    SYNTHETIC_RULE, knowledge_q, offer_reads_as_synthetic, offers_reading_as_synthetic, reads_synthetic,
    real_offer_q, real_template_q, synthetic_data_enabled,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

APPROVED = {
    "status": "approved", "claim_type": "product", "claim_scope": "supported",
    "evidence_kind": "professional_consensus", "source_ref": "DOC-synthetic-mark",
}


@pytest.fixture
def stand(settings):
    """Первый фактор: флаг стенда включён."""
    settings.SYNTHETIC_TEST_DATA_ENABLED = True


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Синтетика", slug="synthetic-mark")


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="synthetic-mark-salon", name="Тестовый салон")


@pytest.fixture
def curator(db):
    return User.objects.create_user(username="synthetic-mark-curator", password="x", is_staff=True)


@pytest.fixture
def goal(db):
    return GoalOption.objects.create(key="synthetic-mark-goal", label="Цель")


def _canon(category, name, *, synthetic=False, **over) -> ServiceTemplate:
    return ServiceTemplate.objects.create(
        category=category, name=name, name_short=name[:40], synthetic=synthetic, **over,
    )


def _capability(canon, key, *, synthetic=False, curator=None, **over) -> ProcedureCapability:
    fields = {"templates": [canon], "key": key, "text_client": f"Текст {key}", "synthetic": synthetic}
    if synthetic:
        fields.update(claim_scope="supported")
    else:
        fields.update(APPROVED, confirmed_by=curator, confirmed_at=timezone.now())
    fields.update(over)
    return ProcedureCapability.objects.create(**fields)


def _link(capability, goal, *, synthetic=False, curator=None, **over) -> CapabilityGoalLink:
    fields = {"capability": capability, "goal": goal, "synthetic": synthetic}
    if synthetic:
        fields.update(claim_scope="supported")
    else:
        fields.update(APPROVED, confirmed_by=curator, confirmed_at=timezone.now())
    fields.update(over)
    return CapabilityGoalLink.objects.create(**fields)


def _offer(salon, category, *, synthetic=False, **over) -> SalonService:
    fields = {
        "tenant": salon, "category": category, "name": "Услуга салона",
        "duration_minutes": 60, "base_price": Decimal("3000"), "synthetic": synthetic,
    }
    fields.update(over)
    return SalonService.objects.create(**fields)


def _refused(marker: str, action) -> None:
    with pytest.raises(IntegrityError, match=marker):
        with transaction.atomic():
            action()


@pytest.fixture
def world(category, curator, goal):
    """Настоящая и синтетическая половины рядом: по канону, способности и связи с одной целью."""
    real_canon = _canon(category, "Настоящий канон")
    fake_canon = _canon(category, "Синтетический канон", synthetic=True)
    real = _capability(real_canon, "real_effect", curator=curator)
    fake = _capability(fake_canon, "synthetic_effect", synthetic=True)
    _link(real, goal, curator=curator)
    _link(fake, goal, synthetic=True)
    return {"real_canon": real_canon, "fake_canon": fake_canon, "real": real, "fake": fake}


# ─── два фактора ─────────────────────────────────────────────────────────────


def test_the_stand_flag_is_off_by_default(settings) -> None:
    assert settings.SYNTHETIC_TEST_DATA_ENABLED is False
    assert synthetic_data_enabled() is False


@pytest.mark.parametrize("flag,asked,expected", [
    (False, False, False), (True, False, False), (False, True, False), (True, True, True),
])
def test_synthetic_reads_only_when_both_factors_hold(settings, flag, asked, expected) -> None:
    settings.SYNTHETIC_TEST_DATA_ENABLED = flag

    assert reads_synthetic(asked) is expected


@pytest.mark.parametrize("flag,asked,expected", [
    (False, False, ["real_effect"]), (True, False, ["real_effect"]),
    (False, True, ["real_effect"]), (True, True, ["real_effect", "synthetic_effect"]),
])
def test_the_goal_reader_needs_both_factors(settings, world, goal, flag, asked, expected) -> None:
    settings.SYNTHETIC_TEST_DATA_ENABLED = flag

    assert list(capabilities.capability_keys_helping_goal(goal.key, include_synthetic=asked)) == expected


def test_every_reader_is_blind_to_synthetic_without_the_request(stand, world, goal) -> None:
    """Сегодняшние вызовы — без параметра — синтетики не видят, даже при включённом флаге."""
    assert capabilities.client_facing_capabilities(world["fake_canon"]).capabilities == ()
    assert capabilities.client_facing_goal_links(world["fake"]) == ()
    assert capabilities.template_ids_helping_goal(goal.key) == frozenset({world["real_canon"].pk})
    assert list(capabilities.capability_keys_helping_goal(goal.key)) == ["real_effect"]
    assert capabilities.capability_labels(["synthetic_effect"])["synthetic_effect"].state.value == "unknown"


def test_every_reader_returns_synthetic_marked_under_both_factors(stand, world, goal) -> None:
    rows = capabilities.client_facing_capabilities(world["fake_canon"], include_synthetic=True).capabilities
    assert [(row.key, row.synthetic) for row in rows] == [("synthetic_effect", True)]

    links = capabilities.client_facing_goal_links(world["fake"], include_synthetic=True)
    assert [link.synthetic for link in links] == [True]

    assert capabilities.template_ids_helping_goal(goal.key, include_synthetic=True) == frozenset(
        {world["real_canon"].pk, world["fake_canon"].pk}
    )
    labels = capabilities.capability_labels(["real_effect", "synthetic_effect"], include_synthetic=True)
    assert {key: (label.label, label.synthetic) for key, label in labels.items()} == {
        "real_effect": ("Текст real_effect", False), "synthetic_effect": ("Текст synthetic_effect", True),
    }


def test_the_real_half_reads_the_same_with_and_without_the_request(stand, world) -> None:
    plain = capabilities.client_facing_capabilities(world["real_canon"]).capabilities
    asked = capabilities.client_facing_capabilities(world["real_canon"], include_synthetic=True).capabilities

    assert [(row.key, row.synthetic) for row in plain] == [("real_effect", False)]
    assert [row.pk for row in asked] == [row.pk for row in plain]


def test_unconfirmed_knowledge_without_the_mark_is_never_read(stand, category, goal) -> None:
    """«Подтверждённое + помеченное», а не «любое неподтверждённое»."""
    canon = _canon(category, "Канон с черновиком")
    draft = ProcedureCapability.objects.create(
        templates=[canon], key="draft_effect", text_client="Черновик", claim_scope="supported",
    )
    CapabilityGoalLink.objects.create(capability=draft, goal=goal, claim_scope="supported")

    assert capabilities.client_facing_capabilities(canon, include_synthetic=True).capabilities == ()
    assert list(capabilities.capability_keys_helping_goal(goal.key, include_synthetic=True)) == []


def test_scope_and_expiry_apply_to_synthetic_too(stand, category) -> None:
    canon = _canon(category, "Синтетический канон", synthetic=True)
    _capability(canon, "prohibited", synthetic=True, claim_scope="prohibited_claim", prohibited_statement="нельзя")
    _capability(canon, "unsupported", synthetic=True, claim_scope="not_supported")
    _capability(canon, "expired", synthetic=True, valid_until=timezone.now() - timedelta(days=1))
    _capability(canon, "alive", synthetic=True, valid_until=timezone.now() + timedelta(days=1))

    rows = capabilities.client_facing_capabilities(canon, include_synthetic=True).capabilities

    assert [row.key for row in rows] == ["alive"]


def test_the_shared_filter_is_the_one_the_readers_use(stand, world) -> None:
    now = timezone.now()

    plain = ProcedureCapability.objects.filter(knowledge_q(now)).values_list("key", flat=True)
    asked = ProcedureCapability.objects.filter(knowledge_q(now, include_synthetic=True)).values_list("key", flat=True)
    through_link = CapabilityGoalLink.objects.filter(
        knowledge_q(now, "capability__", include_synthetic=True),
    ).values_list("capability__key", flat=True)

    assert sorted(plain) == ["real_effect"]
    assert sorted(asked) == ["real_effect", "synthetic_effect"]
    assert sorted(through_link) == ["real_effect", "synthetic_effect"]


# ─── синтетика не становится настоящей ───────────────────────────────────────


@pytest.mark.parametrize("model", ["capability", "link"])
def test_synthetic_knowledge_is_never_approved(category, curator, goal, model) -> None:
    canon = _canon(category, "Синтетический канон", synthetic=True)
    capability = _capability(canon, "synthetic_effect", synthetic=True)
    approval = dict(APPROVED, confirmed_by=curator, confirmed_at=timezone.now())

    if model == "capability":
        _refused("procedurecapability_synthetic_is_never_approved", lambda: ProcedureCapability.objects.filter(
            pk=capability.pk).update(**approval))
        _refused("procedurecapability_synthetic_is_never_approved", lambda: ProcedureCapability.objects.create(
            templates=[canon], key="born_approved", text_client="x", synthetic=True, **approval))
    else:
        link = _link(capability, goal, synthetic=True)
        _refused("capabilitygoallink_synthetic_is_never_approved", lambda: CapabilityGoalLink.objects.filter(
            pk=link.pk).update(**approval))


def test_a_synthetic_salon_service_is_never_verified(salon, category, curator) -> None:
    canon = _canon(category, "Синтетический канон", synthetic=True)
    offer = _offer(salon, category, synthetic=True, template=canon)
    verified = {
        "mapping_status": SalonService.MappingStatus.VERIFIED, "mapping_confirmed_by": curator,
        "mapping_confirmed_at": timezone.now(), "mapping_source_ref": "разбор оператора",
    }

    _refused("salonservice_synthetic_is_never_verified", lambda: SalonService.objects.filter(
        pk=offer.pk).update(**verified))

    # Положительная пара: настоящей услуге то же подтверждение доступно.
    real = _offer(salon, category, name="Настоящая", template=_canon(category, "Настоящий канон"))
    SalonService.objects.filter(pk=real.pk).update(**verified)
    assert SalonService.objects.get(pk=real.pk).mapping_status == SalonService.MappingStatus.VERIFIED


@pytest.mark.parametrize("model", ["canon", "offer", "capability", "link"])
@pytest.mark.parametrize("born", [True, False])
def test_the_mark_never_changes_even_past_the_model(salon, category, curator, goal, model, born) -> None:
    """Иначе «снял пометку → подтвердил» обходило бы оба замка; и наоборот — настоящее не становится тестовым."""
    canon = _canon(category, "Канон", synthetic=born)
    capability = _capability(canon, "effect", synthetic=born, curator=curator)
    row = {
        "canon": lambda: canon,
        "offer": lambda: _offer(salon, category, synthetic=born, template=canon),
        "capability": lambda: capability,
        "link": lambda: _link(capability, goal, synthetic=born, curator=curator),
    }[model]()

    _refused("synthetic_mark_is_immutable", lambda: type(row).objects.filter(pk=row.pk).update(synthetic=not born))

    def through_the_model():
        row.synthetic = not born
        row.save()

    _refused("synthetic_mark_is_immutable", through_the_model)
    assert type(row).objects.get(pk=row.pk).synthetic is born


def test_other_edits_of_marked_rows_still_work(salon, category) -> None:
    """Положительная пара к неизменяемости: триггер не запирает строку целиком."""
    canon = _canon(category, "Синтетический канон", synthetic=True)
    offer = _offer(salon, category, synthetic=True, template=canon)

    ServiceTemplate.objects.filter(pk=canon.pk).update(name_short="Короче")
    offer.name = "Переименована"
    offer.save()

    assert ServiceTemplate.objects.get(pk=canon.pk).name_short == "Короче"
    assert SalonService.objects.get(pk=offer.pk).name == "Переименована"


# ─── синтетическое только к синтетическому ───────────────────────────────────


@pytest.mark.parametrize("capability_synthetic", [True, False])
def test_a_capability_binds_only_to_a_canon_of_its_own_kind(category, curator, capability_synthetic) -> None:
    own = _canon(category, "Свой канон", synthetic=capability_synthetic)
    other = _canon(category, "Чужой канон", synthetic=not capability_synthetic)
    capability = _capability(own, "effect", synthetic=capability_synthetic, curator=curator)

    _refused("synthetic_binds_only_to_synthetic", lambda: CapabilityTemplate.objects.create(
        capability=capability, template=other))

    assert list(capability.templates.values_list("pk", flat=True)) == [own.pk]


@pytest.mark.parametrize("offer_synthetic", [True, False])
def test_a_salon_service_maps_only_to_a_canon_of_its_own_kind(salon, category, offer_synthetic) -> None:
    own = _canon(category, "Свой канон", synthetic=offer_synthetic)
    other = _canon(category, "Чужой канон", synthetic=not offer_synthetic)

    _refused("synthetic_binds_only_to_synthetic", lambda: _offer(
        salon, category, synthetic=offer_synthetic, template=other))

    offer = _offer(salon, category, synthetic=offer_synthetic, template=own)
    _refused("synthetic_binds_only_to_synthetic", lambda: SalonService.objects.filter(
        pk=offer.pk).update(template=other))
    assert SalonService.objects.get(pk=offer.pk).template_id == own.pk


@pytest.mark.parametrize("capability_synthetic", [True, False])
def test_a_goal_link_carries_the_mark_of_its_capability(category, curator, goal, capability_synthetic) -> None:
    canon = _canon(category, "Канон", synthetic=capability_synthetic)
    capability = _capability(canon, "effect", synthetic=capability_synthetic, curator=curator)

    _refused("synthetic_binds_only_to_synthetic", lambda: _link(
        capability, goal, synthetic=not capability_synthetic, curator=curator))

    assert _link(capability, goal, synthetic=capability_synthetic, curator=curator).pk is not None


def test_a_real_canon_never_becomes_a_candidate_of_a_synthetic_step(stand, world, goal) -> None:
    """Довод, ради которого введён синтетический канон: утечка в настоящие предложения невозможна по построению."""
    helping = capabilities.template_ids_helping_goal(goal.key, include_synthetic=True)
    synthetic_keys = {
        row.key for canon_id in helping
        for row in capabilities.client_facing_capabilities(
            ServiceTemplate.objects.get(pk=canon_id), include_synthetic=True,
        ).capabilities if row.synthetic
    }
    canons_of_synthetic = set(
        ProcedureCapability.objects.filter(key__in=synthetic_keys).values_list("templates__pk", flat=True)
    )

    assert synthetic_keys == {"synthetic_effect"}
    assert canons_of_synthetic == {world["fake_canon"].pk}
    assert ServiceTemplate.objects.filter(real_template_q(), pk__in=canons_of_synthetic).count() == 0


# ─── предикаты для подбора ───────────────────────────────────────────────────


def test_the_real_predicates_exclude_marked_rows(salon, category) -> None:
    real_canon = _canon(category, "Настоящий канон")
    fake_canon = _canon(category, "Синтетический канон", synthetic=True)
    real = _offer(salon, category, name="Настоящая", template=real_canon)
    fake = _offer(salon, category, name="Синтетическая", synthetic=True, template=fake_canon)
    master = SpecialistProfile.objects.create(
        user=User.objects.create_user(username="synthetic-mark-master", password="x"), tenant=salon,
        display_name="Мастер",
    )
    edges = [
        SpecialistService.objects.create(salon_service=row, specialist=master, duration_minutes=60, price=3000)
        for row in (real, fake)
    ]

    assert list(SalonService.objects.filter(real_offer_q(), pk__in=[real.pk, fake.pk])) == [real]
    assert list(ServiceTemplate.objects.filter(real_template_q(), pk__in=[real_canon.pk, fake_canon.pk])) == [
        real_canon
    ]
    assert list(SpecialistService.objects.filter(
        real_offer_q("salon_service__"), pk__in=[edge.pk for edge in edges],
    )) == [edges[0]]
    assert list(SalonService.objects.filter(real_template_q("template__"), pk__in=[real.pk, fake.pk])) == [real]


@pytest.mark.parametrize("flag,asked,expected", [
    (False, False, False), (True, False, False), (False, True, False), (True, True, True),
])
def test_an_offer_reads_as_synthetic_only_under_both_factors(settings, salon, category, flag, asked, expected) -> None:
    settings.SYNTHETIC_TEST_DATA_ENABLED = flag
    fake = _offer(salon, category, synthetic=True)
    real = _offer(salon, category, name="Настоящая")

    assert offer_reads_as_synthetic(fake, include_synthetic=asked) is expected
    assert offer_reads_as_synthetic(real, include_synthetic=asked) is False
    assert offers_reading_as_synthetic([fake.pk, real.pk], include_synthetic=asked) == (
        frozenset({fake.pk}) if expected else frozenset()
    )


def test_the_batch_reader_asks_the_database_nothing_without_the_factors(
    salon, category, django_assert_num_queries,
) -> None:
    fake = _offer(salon, category, synthetic=True)

    with django_assert_num_queries(0):
        assert offers_reading_as_synthetic([fake.pk], include_synthetic=True) == frozenset()


# ─── ответ о проверке здоровья ───────────────────────────────────────────────


def _edge(salon, offer, username):
    master = SpecialistProfile.objects.create(
        user=User.objects.create_user(username=username, password="x"), tenant=salon, display_name="Мастер",
        status=SpecialistProfile.ProfileStatus.ACTIVE,
    )
    return SpecialistService.objects.create(
        salon_service=offer, specialist=master, duration_minutes=60, price=Decimal("3000"),
    )


SYNTHETIC_ANSWER = {
    "requires_health_check": False, "health_check_origin": "confirmed",
    "health_check_confirmed_rule": SYNTHETIC_RULE, "health_check_rule_version": "1",
    "health_check_source_ref": "тестовый набор",
}


@pytest.mark.parametrize("flag,asked,expected", [
    (False, False, False), (True, False, False), (False, True, False), (True, True, True),
])
def test_a_synthetic_health_answer_counts_only_under_both_factors(
    settings, salon, category, flag, asked, expected,
) -> None:
    settings.SYNTHETIC_TEST_DATA_ENABLED = flag
    canon = _canon(category, "Синтетический канон", synthetic=True)
    offer = _offer(
        salon, category, synthetic=True, template=canon, health_check_confirmed_at=timezone.now(), **SYNTHETIC_ANSWER,
    )
    edge = _edge(salon, offer, "synthetic-mark-edge")

    assert edge.resolved_health_check_with_origin(include_synthetic=asked) == (False, "salon", expected)
    if not asked:
        assert edge.resolved_health_check_with_origin() == (False, "salon", False)


def test_a_real_health_answer_does_not_depend_on_the_factors(salon, category, curator) -> None:
    offer = _offer(
        salon, category, requires_health_check=False, health_check_origin="confirmed",
        health_check_confirmed_by=curator, health_check_confirmed_at=timezone.now(),
        health_check_source_ref="ответ администратора",
    )
    edge = _edge(salon, offer, "synthetic-mark-real-edge")

    assert edge.resolved_health_check_with_origin() == (False, "salon", True)
    assert edge.resolved_health_check_with_origin(include_synthetic=True) == (False, "salon", True)


def test_a_synthetic_row_is_confirmed_only_by_the_synthetic_rule(salon, category, curator) -> None:
    canon = _canon(category, "Синтетический канон", synthetic=True)
    by_a_person = {
        "requires_health_check": False, "health_check_origin": "confirmed", "health_check_confirmed_by": curator,
        "health_check_confirmed_at": timezone.now(), "health_check_source_ref": "человек",
    }

    _refused("salonservice_synthetic_health_only_by_synthetic_rule", lambda: _offer(
        salon, category, synthetic=True, template=canon, **by_a_person))
    _refused("servicetemplate_synthetic_health_only_by_synthetic_rule", lambda: ServiceTemplate.objects.filter(
        pk=canon.pk).update(**{k: v for k, v in by_a_person.items() if k != "requires_health_check"}))


def test_the_synthetic_rule_is_refused_on_real_rows(salon, category) -> None:
    canon = _canon(category, "Настоящий канон")
    answer = dict(SYNTHETIC_ANSWER, health_check_confirmed_at=timezone.now())

    _refused("salonservice_synthetic_rule_only_on_synthetic", lambda: _offer(
        salon, category, template=canon, **answer))
    _refused("servicetemplate_synthetic_rule_only_on_synthetic", lambda: ServiceTemplate.objects.filter(
        pk=canon.pk).update(**{k: v for k, v in answer.items() if k != "requires_health_check"}))


# ─── миграция ────────────────────────────────────────────────────────────────


def test_the_migration_marks_nothing_and_installs_the_triggers() -> None:
    assert not ServiceTemplate.objects.filter(synthetic=True).exists()
    assert not SalonService.objects.filter(synthetic=True).exists()
    assert not ProcedureCapability.objects.filter(synthetic=True).exists()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname LIKE %s ORDER BY tgname",
            ["%synthetic%"],
        )
        installed = [row[0] for row in cursor.fetchall()]

    assert installed == [
        "capabilitygoallink_synthetic_mark_is_immutable", "capabilitygoallink_synthetic_stays_apart",
        "capabilitytemplate_synthetic_stays_apart", "procedurecapability_synthetic_mark_is_immutable",
        "salonservice_synthetic_mark_is_immutable", "salonservice_synthetic_stays_apart",
        "servicetemplate_synthetic_mark_is_immutable",
    ]
