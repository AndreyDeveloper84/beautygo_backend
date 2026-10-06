"""R0 умного ранжирования — глубина совпадения с целью внутри S2 (DRF-2789).

До R0 при запросе по цели все совпадения были уровнем ``GOAL_CATEGORY`` = 1:
один ярус, порядок решала ротация. Теперь внутри уровня — глубина:

    4  шаблон услуги подтверждённо помогает этой цели (CapabilityGoalLink,
       оба утверждения подтверждены)
    3  основная категория цели (наименьший sort_order связи), связана прямо
    2  лист под основным корнем цели
    1  побочная категория цели, связана прямо
    0  лист под побочным корнем

Основная всегда выше побочной: лист НАСЛЕДУЕТ класс своего корня (ревью —
иначе лист под основным корнем проигрывал прямой побочной связи).

Что заперто:

- через настоящий источник и настоящий резолвер четыре мастера расходятся на
  четыре яруса в этом порядке, ``separation_stage`` = S2 (до R0 — NOT_SPLIT);
- каждая глубина объяснена своим кодом рядом с MATCH_GOAL_CATEGORY;
- уровень доминирует: совпадение по названию выше любой глубины цели;
- неподтверждённое знание глубины 4 не даёт: вывод системы, подтверждённая
  связь при неподтверждённой возможности, истёкшая связь, выключенная цель;
- лист под основным корнем выше прямой побочной категории;
- из двух VERIFIED услуг мастера берётся более глубокая;
- у кандидатов не по цели глубины нет (§29.4: стадия не выдумывает данных);
- из двух услуг мастера в цели берётся VERIFIED, а не глубокая без проверенной связи.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.utils import timezone

from recommendation._pipeline import resolve
from recommendation._reason_codes import ReasonCode
from recommendation._stages import stage_semantic_fit
from recommendation._types import (
    MatchLevel,
    NeedOrigin,
    NeedSpec,
    RecommendationRequest,
    SafetyState,
    Scope,
    ScopeMode,
    SeparationState,
    StageId,
    Surface,
)
from recommendation.tests.conftest import make_facts
from services.models import (
    CapabilityGoalLink,
    GoalOption,
    GoalOptionCategory,
    ProcedureCapability,
    SalonService,
    ServiceCategory,
    ServiceTemplate,
    SpecialistService,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, User
from users.recommendation_source import SpecialistCandidateSource

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="depth-2789", name="Глубина", is_active=True)


@pytest.fixture
def curator(db):
    return User.objects.create_user(username="curator-2789", password="x", role="client", phone="+79995789000")


@pytest.fixture
def tree(db):
    """Цель relax: основная категория A (sort 0), побочная B (sort 1), корень C (sort 2) с листом C1."""
    a = ServiceCategory.objects.create(slug="d2789-a", name="Базовый 2789")
    b = ServiceCategory.objects.create(slug="d2789-b", name="Расслабляющие 2789")
    c = ServiceCategory.objects.create(slug="d2789-c", name="SPA 2789")
    c1 = ServiceCategory.objects.create(slug="d2789-c1", name="SPA-программы 2789", parent=c)
    goal = GoalOption.objects.create(key="relax-2789", label="Расслабиться 2789")
    GoalOptionCategory.objects.create(goal_option=goal, category=a, sort_order=0)
    GoalOptionCategory.objects.create(goal_option=goal, category=b, sort_order=1)
    GoalOptionCategory.objects.create(goal_option=goal, category=c, sort_order=2)
    return {"a": a, "b": b, "c1": c1, "goal": goal}


def _master(tenant, suffix: str) -> SpecialistProfile:
    user = User.objects.create_user(
        username=f"d2789-{suffix}", password="x", role="specialist", phone=f"+79995789{suffix}",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = tenant
    profile.display_name = f"Мастер {suffix}"
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.save()
    return profile


def _offer(tenant, master, category, *, name, verified=True) -> SalonService:
    template = ServiceTemplate.objects.create(
        category=category, name=f"{name} канон {uuid.uuid4().hex[:6]}", name_short=name[:20], duration_default=60,
    )
    provenance = {
        "mapping_confirmed_rule": "test_fixture", "mapping_rule_version": "1.0.0",
        "mapping_confirmed_at": timezone.now(), "mapping_source_ref": "fixture:2789",
    } if verified else {}
    salon_service = SalonService.objects.create(
        tenant=tenant, name=name, category=category, template=template, duration_minutes=60, is_active=True,
        mapping_status=SalonService.MappingStatus.VERIFIED if verified else SalonService.MappingStatus.UNMAPPED,
        **provenance,
    )
    SpecialistService.objects.create(
        specialist=master, salon_service=salon_service, tenant=tenant, price=Decimal("2000"), is_active=True,
    )
    return salon_service


def _signed(curator, approved: bool, **extra) -> dict:
    base = {
        "status": "approved", "claim_type": "product", "claim_scope": "supported",
        "evidence_kind": "professional_consensus", "source_ref": "DOC-2789",
        "confirmed_by": curator, "confirmed_at": timezone.now(),
    } if approved else {"claim_scope": "supported", "source_ref": "DOC-2789"}
    return {**base, **extra}


def _helps(template, goal, curator, *, approved=True, capability_approved=None, link_extra=None) -> None:
    """«Процедура умеет X» и «X помогает цели».

    ``approved=False`` — оба утверждения вывод системы; ``capability_approved``
    отдельно — подтверждена связь, а возможность нет; ``link_extra`` — поля
    связи сверху (например, истёкший ``valid_until``).
    """
    cap_ok = approved if capability_approved is None else capability_approved
    capability = ProcedureCapability.objects.create(
        template=template, key=f"calm-{uuid.uuid4().hex[:6]}", text_client="Синтетическая формулировка",
        **_signed(curator, cap_ok),
    )
    CapabilityGoalLink.objects.create(
        capability=capability, goal=goal, **_signed(curator, approved, **(link_extra or {})),
    )


@pytest.fixture
def four(tenant, tree, curator):
    """Четыре мастера на четырёх глубинах; у «d3» услуга в ПОБОЧНОЙ категории,
    но с подтверждённой связью процедуры с целью — глубина 3 бьёт категорию."""
    masters = {k: _master(tenant, s) for k, s in (("d3", "03"), ("d2", "02"), ("d1", "01"), ("d0", "00"))}
    s3 = _offer(tenant, masters["d3"], tree["b"], name="Ароматерапия")
    _helps(s3.template, tree["goal"], curator)
    _offer(tenant, masters["d2"], tree["a"], name="Классический")
    _offer(tenant, masters["d1"], tree["b"], name="Релакс")
    _offer(tenant, masters["d0"], tree["c1"], name="SPA-ритуал")
    return masters


def _resolve(goal_key):
    need = NeedSpec(origin=NeedOrigin.GOAL, goal_key=goal_key)
    source = SpecialistCandidateSource()
    decision = resolve(RecommendationRequest(
        request_id=str(uuid.uuid4()), subject_ref="subj-2789", surface=Surface.MINIAPP_HOME,
        scope=Scope(ScopeMode.MARKETPLACE), need=need, safety_state=SafetyState.NOT_APPLICABLE,
        tie_break_seed="seed-2789", k=10,
    ), source=source)
    return decision


def _user_id(profile) -> str:
    return str(profile.user_id)


class TestDepthOrdersTheGoal:
    def test_four_depths_make_four_tiers_in_order_and_s2_splits(self, four, tree):
        decision = _resolve(tree["goal"].key)

        by_ref = {str(c.candidate_ref.id): c for c in decision.ordered}
        tiers = {k: by_ref[_user_id(p)].tier for k, p in four.items()}
        assert tiers["d3"] < tiers["d2"] < tiers["d1"] < tiers["d0"], tiers
        assert decision.separation_state is SeparationState.SPLIT
        assert decision.separation_stage is StageId.S2

    def test_each_depth_is_explained_next_to_the_goal_match(self, four, tree):
        decision = _resolve(tree["goal"].key)

        by_ref = {str(c.candidate_ref.id): set(c.reason_codes) for c in decision.ordered}
        expected = {
            "d3": ReasonCode.MATCH_GOAL_CONFIRMED_CAPABILITY,
            "d2": ReasonCode.MATCH_GOAL_PRIMARY_CATEGORY,
            "d1": ReasonCode.MATCH_GOAL_SECONDARY_CATEGORY,
            "d0": ReasonCode.MATCH_GOAL_EXPANDED_CATEGORY,
        }
        for key, code in expected.items():
            codes = by_ref[_user_id(four[key])]
            assert {ReasonCode.MATCH_GOAL_CATEGORY, code} <= codes, (key, codes)

    def test_an_unconfirmed_capability_link_gives_no_depth_three(self, tenant, tree, curator):
        """Вывод системы — не знание: та же связь без подтверждения даёт глубину побочной категории."""
        master = _master(tenant, "09")
        service = _offer(tenant, master, tree["b"], name="Ароматерапия-черновик")
        _helps(service.template, tree["goal"], curator, approved=False)

        [facts] = SpecialistCandidateSource().fetch(
            scope=Scope(ScopeMode.MARKETPLACE), need=NeedSpec(origin=NeedOrigin.GOAL, goal_key=tree["goal"].key),
        )

        assert facts.match_level is MatchLevel.GOAL_CATEGORY
        assert facts.goal_fit_depth == 1

    def test_the_verified_row_is_chosen_over_a_deeper_unverified_one(self, tenant, tree):
        """Две услуги мастера в цели: глубокая (основная категория) без проверенной
        связи и неглубокая (побочная) VERIFIED. Берётся VERIFIED — иначе мастер выпал бы из выдачи."""
        master = _master(tenant, "08")
        _offer(tenant, master, tree["a"], name="Основная-непроверенная", verified=False)
        verified = _offer(tenant, master, tree["b"], name="Побочная-проверенная")

        [facts] = SpecialistCandidateSource().fetch(
            scope=Scope(ScopeMode.MARKETPLACE), need=NeedSpec(origin=NeedOrigin.GOAL, goal_key=tree["goal"].key),
        )

        assert facts.matched_service_ref == verified.pk
        assert facts.goal_fit_depth == 1


class TestTheLevelStillDominates:
    def test_a_name_match_outranks_the_deepest_goal_match(self):
        name_match = make_facts(match_level=MatchLevel.SERVICE_PARTIAL, matched_service_ref=uuid.uuid4())
        deep_goal = make_facts(
            match_level=MatchLevel.GOAL_CATEGORY, matched_service_ref=uuid.uuid4(),
            matched_goal_category_ref=uuid.uuid4(), goal_fit_depth=4,
        )
        out = stage_semantic_fit([name_match, deep_goal], NeedSpec(origin=NeedOrigin.GOAL, goal_key="relax"))

        assert out.keys[name_match.ref.id] > out.keys[deep_goal.ref.id]

    def test_candidates_outside_the_goal_level_carry_no_depth_code(self):
        name_match = make_facts(
            match_level=MatchLevel.SERVICE_PARTIAL, matched_service_ref=uuid.uuid4(), goal_fit_depth=4,
        )
        out = stage_semantic_fit([name_match], NeedSpec(origin=NeedOrigin.USER_EXPLICIT, raw_text="массаж"))

        assert out.codes[name_match.ref.id] == frozenset({ReasonCode.MATCH_SERVICE_PARTIAL})
        assert out.keys[name_match.ref.id] == 3.0, "глубина вне уровня цели ключа не трогает"


class TestReviewFindings:
    def test_a_leaf_under_the_primary_root_beats_a_direct_secondary_category(self, tenant):
        """Основной корень P (sort 0) с листом P1, побочный лист S (sort 1) — форма
        body_shape на пилоте. Услуга на P1 выше услуги на S."""
        p = ServiceCategory.objects.create(slug="d2789-p", name="Аппаратный 2789")
        p1 = ServiceCategory.objects.create(slug="d2789-p1", name="RF 2789", parent=p)
        s = ServiceCategory.objects.create(slug="d2789-s", name="Лимфодренаж 2789")
        goal = GoalOption.objects.create(key="shape-2789", label="Фигура 2789")
        GoalOptionCategory.objects.create(goal_option=goal, category=p, sort_order=0)
        GoalOptionCategory.objects.create(goal_option=goal, category=s, sort_order=1)
        under_primary = _master(tenant, "21")
        secondary = _master(tenant, "22")
        _offer(tenant, under_primary, p1, name="RF-лифтинг тела")
        _offer(tenant, secondary, s, name="Лимфодренажный")

        decision = _resolve(goal.key)

        tier = {str(c.candidate_ref.id): c.tier for c in decision.ordered}
        assert tier[_user_id(under_primary)] < tier[_user_id(secondary)], tier

    def test_of_two_verified_rows_the_deeper_one_is_chosen(self, tenant, tree):
        """Две VERIFIED услуги мастера в цели; побочная идёт ПЕРВОЙ в порядке каталога
        (по имени), основная — второй. Берётся основная: глубина решает при равном статусе."""
        master = _master(tenant, "23")
        _offer(tenant, master, tree["b"], name="А-побочная")
        primary = _offer(tenant, master, tree["a"], name="Я-основная")

        [facts] = SpecialistCandidateSource().fetch(
            scope=Scope(ScopeMode.MARKETPLACE), need=NeedSpec(origin=NeedOrigin.GOAL, goal_key=tree["goal"].key),
        )

        assert facts.matched_service_ref == primary.pk
        assert facts.goal_fit_depth == 3

    @pytest.mark.parametrize("case", ["capability_unapproved", "link_expired", "goal_inactive"])
    def test_knowledge_that_is_not_client_facing_gives_no_top_depth(self, case, tenant, tree, curator):
        master = _master(tenant, "24")
        service = _offer(tenant, master, tree["b"], name="Ароматерапия-проверка")
        if case == "capability_unapproved":
            _helps(service.template, tree["goal"], curator, capability_approved=False)
        elif case == "link_expired":
            _helps(service.template, tree["goal"], curator,
                   link_extra={"valid_until": timezone.now() - timezone.timedelta(days=1)})
        else:
            _helps(service.template, tree["goal"], curator)
            GoalOption.objects.filter(pk=tree["goal"].pk).update(is_active=False)

        [facts] = SpecialistCandidateSource().fetch(
            scope=Scope(ScopeMode.MARKETPLACE), need=NeedSpec(origin=NeedOrigin.GOAL, goal_key=tree["goal"].key),
        )

        assert facts.goal_fit_depth == 1, case

    def test_positive_control_the_same_link_approved_gives_top_depth(self, tenant, tree, curator):
        master = _master(tenant, "25")
        service = _offer(tenant, master, tree["b"], name="Ароматерапия-контроль")
        _helps(service.template, tree["goal"], curator)

        [facts] = SpecialistCandidateSource().fetch(
            scope=Scope(ScopeMode.MARKETPLACE), need=NeedSpec(origin=NeedOrigin.GOAL, goal_key=tree["goal"].key),
        )

        assert facts.goal_fit_depth == 4
