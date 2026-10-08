"""Пометка «синтетика»: тестовые данные, которые не становятся настоящими.

Решение владельца 08.10.2026 (сквозная проверка Плана на подготовленных
данных). Требования, которые держат узлы:

* сборка читает подтверждённое + помеченное, а не «любое неподтверждённое»;
* пометка никогда не превращается в подтверждённое знание и подтверждённую
  связь с каноном — замками базы, включая запись мимо модели;
* читать синтетику разрешает СЕРВЕР по личности, а не поле запроса: право —
  объект, который выдаёт только ``grant_for``; булево правом не становится;
* вся цепочка тестовая: синтетическое привязано только к синтетическому,
  услуга — только с каноном и только в демо-салоне, предложение — только у
  мастера этого салона.

Что узлы НЕ держат: допуск синтетического предложения в подбор (ветка в
``users.admission``), пометку на самом плане и сторож записи — это соседние
окна.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from services import capabilities
from services.models import (
    CapabilityGoalLink, CapabilityTemplate, GoalOption, ProcedureCapability, SalonService, ServiceCategory,
    ServiceTemplate, SpecialistService,
)
from services.synthetic import (
    SYNTHETIC_RULE, SyntheticGrant, grant_for, knowledge_q, offer_reads_as_synthetic,
    offers_reading_as_synthetic, reads_synthetic, real_offer_q, real_template_q, synthetic_data_enabled,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

APPROVED = {
    "status": "approved", "claim_type": "product", "claim_scope": "supported",
    "evidence_kind": "professional_consensus", "source_ref": "DOC-synthetic-mark",
}


@pytest.fixture
def tester(db):
    """Тестовый субъект: пользователь каталога с признаком тестовой персоны."""
    return User.objects.create_user(username="synthetic-mark-tester", password="x", is_test_persona=True)


@pytest.fixture
def grant(settings, tester):
    """Все три серверных условия выполнены — разрешение выдано."""
    settings.SYNTHETIC_TEST_DATA_ENABLED = True
    settings.SYNTHETIC_TEST_SUBJECT_IDS = [str(tester.pk)]
    issued = grant_for(tester)
    assert issued is not None
    return issued


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Синтетика", slug="synthetic-mark")


@pytest.fixture
def demo_salon(db):
    return Tenant.objects.create(slug="synthetic-mark-demo", name="Демо-салон", is_demo=True)


@pytest.fixture
def real_salon(db):
    return Tenant.objects.create(slug="synthetic-mark-real", name="Настоящий салон")


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


def _master(salon, username) -> SpecialistProfile:
    user = User.objects.create_user(username=username, password="x", role="specialist")
    SpecialistProfile.objects.filter(user=user).update(
        tenant=salon, display_name="Мастер", status=SpecialistProfile.ProfileStatus.ACTIVE,
    )
    return SpecialistProfile.objects.get(user=user)


def _edge(master, offer) -> SpecialistService:
    return SpecialistService.objects.create(
        salon_service=offer, specialist=master, duration_minutes=60, price=Decimal("3000"),
    )


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


# ─── разрешение: выдаёт сервер, по личности ──────────────────────────────────


def test_the_stand_is_closed_by_default(settings) -> None:
    assert settings.SYNTHETIC_TEST_DATA_ENABLED is False
    assert tuple(settings.SYNTHETIC_TEST_SUBJECT_IDS) == ()
    assert synthetic_data_enabled() is False


@pytest.mark.parametrize("flag,listed,persona,granted", [
    (True, True, True, True),
    (False, True, True, False),
    (True, False, True, False),
    (True, True, False, False),
    (False, False, False, False),
])
def test_a_grant_needs_all_three_server_side_conditions(settings, tester, flag, listed, persona, granted) -> None:
    settings.SYNTHETIC_TEST_DATA_ENABLED = flag
    settings.SYNTHETIC_TEST_SUBJECT_IDS = [str(tester.pk)] if listed else ["00000000-0000-0000-0000-000000000000"]
    tester.is_test_persona = persona

    issued = grant_for(tester)

    assert (issued is not None) is granted
    if granted:
        assert issued.subject_id == str(tester.pk)
        assert reads_synthetic(issued) is True


def test_nobody_and_an_anonymous_user_get_no_grant_and_cost_no_query(
    settings, tester, django_assert_num_queries,
) -> None:
    settings.SYNTHETIC_TEST_DATA_ENABLED = True
    settings.SYNTHETIC_TEST_SUBJECT_IDS = [str(tester.pk)]

    with django_assert_num_queries(0):
        assert grant_for(None) is None
        assert grant_for(AnonymousUser()) is None
        assert grant_for(tester) is not None  # положительная пара — и тоже без запроса


def test_a_grant_cannot_be_built_around_the_issuer(tester) -> None:
    with pytest.raises(TypeError, match="grant_for"):
        SyntheticGrant(str(tester.pk))
    with pytest.raises(TypeError, match="grant_for"):
        SyntheticGrant(str(tester.pk), _issued_by=object())


def test_a_grant_cannot_be_repointed_at_another_subject(grant) -> None:
    with pytest.raises(AttributeError):
        grant.subject_id = "someone-else"
    with pytest.raises(AttributeError):
        grant._subject_id = "someone-else"


@pytest.mark.parametrize("forged", [True, 1, "1", "true", {"include_synthetic": True}])
def test_a_value_from_a_request_body_is_not_a_grant(settings, tester, world, goal, forged) -> None:
    """Требование владельца: «просьба вызывающего» — не поле запроса. Булево правом не становится по построению."""
    settings.SYNTHETIC_TEST_DATA_ENABLED = True
    settings.SYNTHETIC_TEST_SUBJECT_IDS = [str(tester.pk)]

    with pytest.raises(TypeError, match="SyntheticGrant"):
        reads_synthetic(forged)
    with pytest.raises(TypeError, match="SyntheticGrant"):
        capabilities.capability_keys_helping_goal(goal.key, include_synthetic=forged)
    with pytest.raises(TypeError, match="SyntheticGrant"):
        offers_reading_as_synthetic([], include_synthetic=forged)


@pytest.mark.parametrize("falsy", [False, 0, ""])
def test_a_falsy_value_from_a_request_body_is_refused_too(falsy) -> None:
    """«Нет» из тела — тоже не разрешение и не его отсутствие: принимается только объект или None."""
    with pytest.raises(TypeError, match="SyntheticGrant"):
        reads_synthetic(falsy)


@pytest.mark.parametrize("revoked_by", ["flag", "list"])
def test_an_issued_grant_dies_when_the_server_withdraws_it(settings, grant, world, goal, revoked_by) -> None:
    """Право перепроверяется при каждом чтении, а не один раз при выдаче."""
    assert list(capabilities.capability_keys_helping_goal(goal.key, include_synthetic=grant)) == [
        "real_effect", "synthetic_effect",
    ]

    if revoked_by == "flag":
        settings.SYNTHETIC_TEST_DATA_ENABLED = False
    else:
        settings.SYNTHETIC_TEST_SUBJECT_IDS = []

    assert reads_synthetic(grant) is False
    assert list(capabilities.capability_keys_helping_goal(goal.key, include_synthetic=grant)) == ["real_effect"]


def test_a_grant_is_checked_by_its_own_subject(settings, grant, tester) -> None:
    """В списке другой субъект — разрешение первого не действует: передать «за другого» нечем."""
    other = User.objects.create_user(username="synthetic-mark-other", password="x", is_test_persona=True)
    settings.SYNTHETIC_TEST_SUBJECT_IDS = [str(other.pk)]

    assert reads_synthetic(grant) is False
    assert reads_synthetic(grant_for(other)) is True


# ─── чтение знания ───────────────────────────────────────────────────────────


def test_every_reader_is_blind_to_synthetic_without_a_grant(grant, world, goal) -> None:
    """Сегодняшние вызовы — без разрешения — синтетики не видят, даже когда стенд открыт."""
    assert capabilities.client_facing_capabilities(world["fake_canon"]).capabilities == ()
    assert capabilities.client_facing_goal_links(world["fake"]) == ()
    assert capabilities.template_ids_helping_goal(goal.key) == frozenset({world["real_canon"].pk})
    assert list(capabilities.capability_keys_helping_goal(goal.key)) == ["real_effect"]
    assert capabilities.capability_labels(["synthetic_effect"])["synthetic_effect"].state.value == "unknown"


def test_every_reader_returns_synthetic_marked_under_a_grant(grant, world, goal) -> None:
    rows = capabilities.client_facing_capabilities(world["fake_canon"], include_synthetic=grant).capabilities
    assert [(row.key, row.synthetic) for row in rows] == [("synthetic_effect", True)]

    links = capabilities.client_facing_goal_links(world["fake"], include_synthetic=grant)
    assert [link.synthetic for link in links] == [True]

    assert capabilities.template_ids_helping_goal(goal.key, include_synthetic=grant) == frozenset(
        {world["real_canon"].pk, world["fake_canon"].pk}
    )
    assert list(capabilities.capability_keys_helping_goal(goal.key, include_synthetic=grant)) == [
        "real_effect", "synthetic_effect",
    ]
    labels = capabilities.capability_labels(["real_effect", "synthetic_effect"], include_synthetic=grant)
    assert {key: (label.label, label.synthetic) for key, label in labels.items()} == {
        "real_effect": ("Текст real_effect", False), "synthetic_effect": ("Текст synthetic_effect", True),
    }


def test_the_real_half_reads_the_same_with_and_without_a_grant(grant, world) -> None:
    plain = capabilities.client_facing_capabilities(world["real_canon"]).capabilities
    asked = capabilities.client_facing_capabilities(world["real_canon"], include_synthetic=grant).capabilities

    assert [(row.key, row.synthetic) for row in plain] == [("real_effect", False)]
    assert [row.pk for row in asked] == [row.pk for row in plain]


def test_unconfirmed_knowledge_without_the_mark_is_never_read(grant, category, goal) -> None:
    """«Подтверждённое + помеченное», а не «любое неподтверждённое»."""
    canon = _canon(category, "Канон с черновиком")
    draft = ProcedureCapability.objects.create(
        templates=[canon], key="draft_effect", text_client="Черновик", claim_scope="supported",
    )
    CapabilityGoalLink.objects.create(capability=draft, goal=goal, claim_scope="supported")

    assert capabilities.client_facing_capabilities(canon, include_synthetic=grant).capabilities == ()
    assert list(capabilities.capability_keys_helping_goal(goal.key, include_synthetic=grant)) == []


def test_scope_and_expiry_apply_to_synthetic_too(grant, category) -> None:
    canon = _canon(category, "Синтетический канон", synthetic=True)
    _capability(canon, "prohibited", synthetic=True, claim_scope="prohibited_claim", prohibited_statement="нельзя")
    _capability(canon, "unsupported", synthetic=True, claim_scope="not_supported")
    _capability(canon, "expired", synthetic=True, valid_until=timezone.now() - timedelta(days=1))
    _capability(canon, "alive", synthetic=True, valid_until=timezone.now() + timedelta(days=1))

    rows = capabilities.client_facing_capabilities(canon, include_synthetic=grant).capabilities

    assert [row.key for row in rows] == ["alive"]


def test_the_shared_filter_is_the_one_the_readers_use(grant, world) -> None:
    now = timezone.now()

    plain = ProcedureCapability.objects.filter(knowledge_q(now)).values_list("key", flat=True)
    asked = ProcedureCapability.objects.filter(knowledge_q(now, include_synthetic=grant)).values_list("key", flat=True)
    through_link = CapabilityGoalLink.objects.filter(
        knowledge_q(now, "capability__", include_synthetic=grant),
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


def test_a_synthetic_salon_service_is_never_verified(demo_salon, category, curator) -> None:
    canon = _canon(category, "Синтетический канон", synthetic=True)
    offer = _offer(demo_salon, category, synthetic=True, template=canon)
    verified = {
        "mapping_status": SalonService.MappingStatus.VERIFIED, "mapping_confirmed_by": curator,
        "mapping_confirmed_at": timezone.now(), "mapping_source_ref": "разбор оператора",
    }

    _refused("salonservice_synthetic_is_never_verified", lambda: SalonService.objects.filter(
        pk=offer.pk).update(**verified))

    # Положительная пара: настоящей услуге то же подтверждение доступно.
    real = _offer(demo_salon, category, name="Настоящая", template=_canon(category, "Настоящий канон"))
    SalonService.objects.filter(pk=real.pk).update(**verified)
    assert SalonService.objects.get(pk=real.pk).mapping_status == SalonService.MappingStatus.VERIFIED


def test_a_synthetic_salon_service_may_wait_for_review(demo_salon, category) -> None:
    """Статус сида: «связана, не подтверждена» — на нём допуск отвечает исходом SYNTHETIC."""
    canon = _canon(category, "Синтетический канон", synthetic=True)

    offer = _offer(
        demo_salon, category, synthetic=True, template=canon,
        mapping_status=SalonService.MappingStatus.REVIEW_REQUIRED,
    )

    assert SalonService.objects.get(pk=offer.pk).mapping_status == SalonService.MappingStatus.REVIEW_REQUIRED


@pytest.mark.parametrize("model", ["canon", "offer", "capability", "link"])
@pytest.mark.parametrize("born", [True, False])
def test_the_mark_never_changes_even_past_the_model(demo_salon, category, curator, goal, model, born) -> None:
    """Иначе «снял пометку → подтвердил» обходило бы оба замка; и наоборот — настоящее не становится тестовым."""
    canon = _canon(category, "Канон", synthetic=born)
    capability = _capability(canon, "effect", synthetic=born, curator=curator)
    row = {
        "canon": lambda: canon,
        "offer": lambda: _offer(demo_salon, category, synthetic=born, template=canon),
        "capability": lambda: capability,
        "link": lambda: _link(capability, goal, synthetic=born, curator=curator),
    }[model]()

    _refused("synthetic_mark_is_immutable", lambda: type(row).objects.filter(pk=row.pk).update(synthetic=not born))

    def through_the_model():
        row.synthetic = not born
        row.save()

    _refused("synthetic_mark_is_immutable", through_the_model)
    assert type(row).objects.get(pk=row.pk).synthetic is born


def test_other_edits_of_marked_rows_still_work(demo_salon, category) -> None:
    """Положительная пара к неизменяемости: триггер не запирает строку целиком."""
    canon = _canon(category, "Синтетический канон", synthetic=True)
    offer = _offer(demo_salon, category, synthetic=True, template=canon)

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
def test_a_salon_service_maps_only_to_a_canon_of_its_own_kind(demo_salon, category, offer_synthetic) -> None:
    own = _canon(category, "Свой канон", synthetic=offer_synthetic)
    other = _canon(category, "Чужой канон", synthetic=not offer_synthetic)

    _refused("synthetic_binds_only_to_synthetic", lambda: _offer(
        demo_salon, category, synthetic=offer_synthetic, template=other))

    offer = _offer(demo_salon, category, synthetic=offer_synthetic, template=own)
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


def test_a_real_canon_never_becomes_a_candidate_of_a_synthetic_step(grant, world, goal) -> None:
    """Довод, ради которого введён синтетический канон: утечка в настоящие предложения невозможна по построению."""
    helping = capabilities.template_ids_helping_goal(goal.key, include_synthetic=grant)
    synthetic_keys = {
        row.key for canon_id in helping
        for row in capabilities.client_facing_capabilities(
            ServiceTemplate.objects.get(pk=canon_id), include_synthetic=grant,
        ).capabilities if row.synthetic
    }
    canons_of_synthetic = set(
        ProcedureCapability.objects.filter(key__in=synthetic_keys).values_list("templates__pk", flat=True)
    )

    assert synthetic_keys == {"synthetic_effect"}
    assert canons_of_synthetic == {world["fake_canon"].pk}
    assert ServiceTemplate.objects.filter(real_template_q(), pk__in=canons_of_synthetic).count() == 0


# ─── вся цепочка тестовая: канон, демо-салон, мастер этого салона ────────────


def test_a_synthetic_salon_service_always_has_a_canon(demo_salon, category) -> None:
    canon = _canon(category, "Синтетический канон", synthetic=True)

    _refused("salonservice_synthetic_has_a_canon", lambda: _offer(demo_salon, category, synthetic=True))

    offer = _offer(demo_salon, category, synthetic=True, template=canon)
    _refused("salonservice_synthetic_has_a_canon", lambda: SalonService.objects.filter(
        pk=offer.pk).update(template=None))
    # Положительная пара: настоящая услуга без канона по-прежнему возможна.
    assert _offer(demo_salon, category, name="Настоящая без канона").template_id is None


def test_a_synthetic_salon_service_lives_only_in_a_demo_salon(demo_salon, real_salon, category) -> None:
    canon = _canon(category, "Синтетический канон", synthetic=True)

    _refused("synthetic_lives_only_in_a_demo_salon", lambda: _offer(
        real_salon, category, synthetic=True, template=canon))

    offer = _offer(demo_salon, category, synthetic=True, template=canon)
    _refused("synthetic_lives_only_in_a_demo_salon", lambda: SalonService.objects.filter(
        pk=offer.pk).update(tenant=real_salon))
    assert SalonService.objects.get(pk=offer.pk).tenant_id == demo_salon.pk


def test_a_demo_salon_keeps_its_mark_while_it_holds_synthetic_services(demo_salon, category) -> None:
    """Без этого замок «только в демо-салоне» обходился бы задним числом."""
    empty_demo = Tenant.objects.create(slug="synthetic-mark-empty-demo", name="Пустой демо", is_demo=True)
    canon = _canon(category, "Синтетический канон", synthetic=True)
    _offer(demo_salon, category, synthetic=True, template=canon)

    _refused("demo_salon_holds_synthetic_services", lambda: Tenant.all_objects.filter(
        pk=demo_salon.pk).update(is_demo=False))

    # Положительные пары: другие правки салона проходят; демо без синтетики признак снимает.
    Tenant.all_objects.filter(pk=demo_salon.pk).update(name="Демо-салон, переименован")
    Tenant.all_objects.filter(pk=empty_demo.pk).update(is_demo=False)
    assert Tenant.all_objects.get(pk=demo_salon.pk).is_demo is True
    assert Tenant.all_objects.get(pk=empty_demo.pk).is_demo is False


def test_a_synthetic_offer_is_opened_only_by_a_master_of_its_salon(demo_salon, real_salon, category) -> None:
    """Правило «мастер и услуга одного салона» для синтетики стоит в базе, а не только в коде модели."""
    canon = _canon(category, "Синтетический канон", synthetic=True)
    offer = _offer(demo_salon, category, synthetic=True, template=canon)
    own, stranger = _master(demo_salon, "synthetic-mark-own"), _master(real_salon, "synthetic-mark-stranger")

    def past_the_model():
        SpecialistService.objects.bulk_create([SpecialistService(
            salon_service=offer, specialist=stranger, tenant=demo_salon, duration_minutes=60, price=Decimal("3000"),
        )])

    _refused("synthetic_offer_only_by_a_master_of_its_salon", past_the_model)

    edge = _edge(own, offer)
    _refused("synthetic_offer_only_by_a_master_of_its_salon", lambda: SpecialistService.objects.filter(
        pk=edge.pk).update(specialist=stranger))
    assert SpecialistService.objects.get(pk=edge.pk).specialist_id == own.pk


# ─── предикаты для подбора ───────────────────────────────────────────────────


def test_the_real_predicates_exclude_marked_rows(demo_salon, category) -> None:
    real_canon = _canon(category, "Настоящий канон")
    fake_canon = _canon(category, "Синтетический канон", synthetic=True)
    real = _offer(demo_salon, category, name="Настоящая", template=real_canon)
    fake = _offer(demo_salon, category, name="Синтетическая", synthetic=True, template=fake_canon)
    master = _master(demo_salon, "synthetic-mark-master")
    edges = [_edge(master, row) for row in (real, fake)]

    assert list(SalonService.objects.filter(real_offer_q(), pk__in=[real.pk, fake.pk])) == [real]
    assert list(ServiceTemplate.objects.filter(real_template_q(), pk__in=[real_canon.pk, fake_canon.pk])) == [
        real_canon
    ]
    assert list(SpecialistService.objects.filter(
        real_offer_q("salon_service__"), pk__in=[edge.pk for edge in edges],
    )) == [edges[0]]
    assert list(SalonService.objects.filter(real_template_q("template__"), pk__in=[real.pk, fake.pk])) == [real]


def test_an_offer_reads_as_synthetic_only_under_a_live_grant(settings, grant, demo_salon, category) -> None:
    canon = _canon(category, "Синтетический канон", synthetic=True)
    fake = _offer(demo_salon, category, synthetic=True, template=canon)
    real = _offer(demo_salon, category, name="Настоящая")

    assert offer_reads_as_synthetic(fake, include_synthetic=grant) is True
    assert offer_reads_as_synthetic(real, include_synthetic=grant) is False
    assert offer_reads_as_synthetic(fake, include_synthetic=None) is False
    assert offers_reading_as_synthetic([fake.pk, real.pk], include_synthetic=grant) == frozenset({fake.pk})
    assert offers_reading_as_synthetic([fake.pk, real.pk], include_synthetic=None) == frozenset()

    settings.SYNTHETIC_TEST_DATA_ENABLED = False
    assert offer_reads_as_synthetic(fake, include_synthetic=grant) is False
    assert offers_reading_as_synthetic([fake.pk, real.pk], include_synthetic=grant) == frozenset()


def test_the_batch_reader_asks_the_database_nothing_without_a_live_grant(
    settings, grant, demo_salon, category, django_assert_num_queries,
) -> None:
    canon = _canon(category, "Синтетический канон", synthetic=True)
    fake = _offer(demo_salon, category, synthetic=True, template=canon)
    settings.SYNTHETIC_TEST_SUBJECT_IDS = []

    with django_assert_num_queries(0):
        assert offers_reading_as_synthetic([fake.pk], include_synthetic=None) == frozenset()
        assert offers_reading_as_synthetic([fake.pk], include_synthetic=grant) == frozenset()


# ─── ответ о проверке здоровья ───────────────────────────────────────────────


SYNTHETIC_ANSWER = {
    "requires_health_check": False, "health_check_origin": "confirmed",
    "health_check_confirmed_rule": SYNTHETIC_RULE, "health_check_rule_version": "1",
    "health_check_source_ref": "тестовый набор",
}


def test_a_synthetic_health_answer_counts_only_under_a_live_grant(settings, grant, demo_salon, category) -> None:
    canon = _canon(category, "Синтетический канон", synthetic=True)
    offer = _offer(
        demo_salon, category, synthetic=True, template=canon, health_check_confirmed_at=timezone.now(),
        **SYNTHETIC_ANSWER,
    )
    edge = _edge(_master(demo_salon, "synthetic-mark-edge"), offer)

    assert edge.resolved_health_check_with_origin(include_synthetic=grant) == (False, "salon", True)
    assert edge.resolved_health_check_with_origin() == (False, "salon", False)

    settings.SYNTHETIC_TEST_DATA_ENABLED = False
    assert edge.resolved_health_check_with_origin(include_synthetic=grant) == (False, "salon", False)
    with pytest.raises(TypeError, match="SyntheticGrant"):
        edge.resolved_health_check_with_origin(include_synthetic=True)


def test_a_real_health_answer_does_not_depend_on_the_grant(grant, demo_salon, category, curator) -> None:
    offer = _offer(
        demo_salon, category, requires_health_check=False, health_check_origin="confirmed",
        health_check_confirmed_by=curator, health_check_confirmed_at=timezone.now(),
        health_check_source_ref="ответ администратора",
    )
    edge = _edge(_master(demo_salon, "synthetic-mark-real-edge"), offer)

    assert edge.resolved_health_check_with_origin() == (False, "salon", True)
    assert edge.resolved_health_check_with_origin(include_synthetic=grant) == (False, "salon", True)


def test_a_synthetic_row_is_confirmed_only_by_the_synthetic_rule(demo_salon, category, curator) -> None:
    canon = _canon(category, "Синтетический канон", synthetic=True)
    by_a_person = {
        "requires_health_check": False, "health_check_origin": "confirmed", "health_check_confirmed_by": curator,
        "health_check_confirmed_at": timezone.now(), "health_check_source_ref": "человек",
    }

    _refused("salonservice_synthetic_health_only_by_synthetic_rule", lambda: _offer(
        demo_salon, category, synthetic=True, template=canon, **by_a_person))
    _refused("servicetemplate_synthetic_health_only_by_synthetic_rule", lambda: ServiceTemplate.objects.filter(
        pk=canon.pk).update(**{k: v for k, v in by_a_person.items() if k != "requires_health_check"}))


def test_the_synthetic_rule_is_refused_on_real_rows(demo_salon, category) -> None:
    canon = _canon(category, "Настоящий канон")
    answer = dict(SYNTHETIC_ANSWER, health_check_confirmed_at=timezone.now())

    _refused("salonservice_synthetic_rule_only_on_synthetic", lambda: _offer(
        demo_salon, category, template=canon, **answer))
    _refused("servicetemplate_synthetic_rule_only_on_synthetic", lambda: ServiceTemplate.objects.filter(
        pk=canon.pk).update(**{k: v for k, v in answer.items() if k != "requires_health_check"}))


# ─── админка ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("model", [ServiceTemplate, SalonService, ProcedureCapability, CapabilityGoalLink])
def test_the_admin_shows_the_mark_and_does_not_let_it_be_set(model, curator) -> None:
    from django.contrib import admin as django_admin
    from django.test import RequestFactory

    curator.is_superuser = True
    curator.save()
    request = RequestFactory().get("/")
    request.user = curator
    field = django_admin.site._registry[model].get_form(request).base_fields.get("synthetic")

    # У знания набор полей формы задан явно, и пометки в нём нет — поставить
    # её нечем. У канона и услуги салона поле на форме есть и отключено.
    if model in (ServiceTemplate, SalonService):
        assert field is not None, "пометку должно быть видно на форме"
    assert field is None or field.disabled is True


def test_a_canon_created_through_the_admin_form_is_never_synthetic(category, curator) -> None:
    from django.contrib import admin as django_admin
    from django.test import RequestFactory

    curator.is_superuser = True
    curator.save()
    request = RequestFactory().get("/")
    request.user = curator
    form_class = django_admin.site._registry[ServiceTemplate].get_form(request)
    initial = {name: field.initial for name, field in form_class.base_fields.items() if field.initial is not None}

    form = form_class(data={
        **initial, "category": str(category.pk), "name": "Заведён формой", "name_short": "Формой",
        "synthetic": "on",
    })

    assert form.is_valid(), form.errors
    assert form.save().synthetic is False


# ─── миграция ────────────────────────────────────────────────────────────────


#: Перепись ВСЕХ замков-триггеров этого рода в базе — по одному на строку, по
#: алфавиту. Новый триггер о синтетике (в любом приложении) обязан сюда
#: попасть: узел краснеет, пока его не назвали.
SYNTHETIC_TRIGGERS = [
    "capabilitygoallink_synthetic_mark_is_immutable",
    "capabilitygoallink_synthetic_stays_apart",
    "capabilitytemplate_synthetic_stays_apart",
    "procedurecapability_synthetic_mark_is_immutable",
    "salonservice_synthetic_mark_is_immutable",
    "salonservice_synthetic_stays_apart",
    "servicetemplate_synthetic_mark_is_immutable",
    "specialistservice_synthetic_only_by_a_master_of_its_salon",
    "tenant_keeps_demo_while_it_holds_synthetic",
]


def test_the_migration_marks_nothing_and_installs_the_triggers() -> None:
    assert not ServiceTemplate.objects.filter(synthetic=True).exists()
    assert not SalonService.objects.filter(synthetic=True).exists()
    assert not ProcedureCapability.objects.filter(synthetic=True).exists()
    assert not CapabilityGoalLink.objects.filter(synthetic=True).exists()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname LIKE %s ORDER BY tgname",
            ["%synthetic%"],
        )
        installed = [row[0] for row in cursor.fetchall()]

    assert installed == SYNTHETIC_TRIGGERS
