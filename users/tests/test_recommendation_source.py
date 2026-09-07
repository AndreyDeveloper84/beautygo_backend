"""Доменный адаптер резолвера — `users/recommendation_source.py`. T6/T8.

Проверяется ровно то, за что адаптер отвечает: **какие факты он говорит**.
Порядок он не задаёт, и тестов на порядок здесь нет по построению — они
живут в `recommendation/tests`, потому что порядок живёт в резолвере.

Главное, что здесь стережётся: адаптер **не выдаёт `VERIFIED` никому**.
Шкалы доверия в схеме нет; выдать статус, которого никто не присваивал,
значило бы ответить реализацией на открытый вопрос владельца (§40.4 п.1) —
тем же способом, каким литерал рейтинга из сида стал «свидетельством»
на экране.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from recommendation.api import CandidateKind, MappingStatus, MatchLevel, NeedOrigin, NeedSpec, Scope, ScopeMode
from services.models import (
    DraftSalonService,
    SalonService,
    ServiceCategory,
    ServiceTemplate,
    SpecialistService,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, User
from users.recommendation_source import SpecialistCandidateSource, build_candidate_source


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="src-tenant", name="Источник", is_active=True)


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(slug="massage", name="Массаж")


def _specialist(tenant, *, suffix: str, name: str, rating=None, reviews=0) -> SpecialistProfile:
    """Мастер с профилем.

    Профиль НЕ создаётся здесь: его заводит сигнал `post_save` на
    `User(role="specialist")`. Создать второй — нарушить уникальность
    `user_id`; поэтому найденный обновляется.

    `rating=None` означает «оценки нет», и в базе это **ноль**: столбец
    `NOT NULL` с умолчанием `0.0`. Ноль и отсутствие в схеме неотличимы —
    ровно поэтому адаптер трактует ноль как отсутствие данных (§29.4).
    """
    user = User.objects.create_user(
        username=f"src-{suffix}", password="x", role="specialist", phone=f"+7999500{suffix}",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = tenant
    profile.display_name = name
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.rating = Decimal(rating) if rating is not None else Decimal("0.0")
    profile.reviews_count = reviews
    profile.save()
    return profile


def _offer(tenant, specialist, category, *, name: str, template=None) -> SalonService:
    salon_service = SalonService.objects.create(
        tenant=tenant, name=name, category=category, template=template,
        duration_minutes=60, is_active=True,
    )
    SpecialistService.objects.create(
        specialist=specialist, salon_service=salon_service,
        tenant=tenant, price=Decimal("2000"), is_active=True,
    )
    return salon_service


def _need(**overrides) -> NeedSpec:
    return NeedSpec(origin=NeedOrigin.USER_EXPLICIT, **overrides)


def _fetch(scope=None, need=None):
    return SpecialistCandidateSource().fetch(
        scope=scope or Scope(ScopeMode.MARKETPLACE),
        need=need or _need(raw_text="массаж"),
    )


@pytest.mark.django_db
class TestMappingStatus:
    def test_no_template_is_unmapped(self, tenant, category):
        master = _specialist(tenant, suffix="0001", name="Без шаблона")
        _offer(tenant, master, category, name="Массаж спины")

        facts = _fetch()
        assert [f.mapping_status for f in facts] == [MappingStatus.UNMAPPED]

    def test_template_alone_is_review_required_not_verified(self, tenant, category):
        """Связь есть, доверия к ней нет. Это разные вещи, и поле — одно.

        Замер 07.09: 206 из 265 услуг связаны с шаблоном. Связь — не
        признак проверки; шкалы проверки в схеме не существует.
        """
        template = ServiceTemplate.objects.create(name="Массаж классический", category=category)
        master = _specialist(tenant, suffix="0002", name="С шаблоном")
        _offer(tenant, master, category, name="Массаж", template=template)

        assert [f.mapping_status for f in _fetch()] == [MappingStatus.REVIEW_REQUIRED]

    def test_human_confirmation_does_not_become_verified_by_itself(self, tenant, category):
        """Подтверждённый черновик — свидетельство, а НЕ статус доверия.

        Машина `DraftSalonService(confirmed)` похожа на
        `REVIEW_REQUIRED → VERIFIED`, но человек подтверждал строку
        онбординга, а не пригодность к рекомендации. Превратить один клик
        в признак доверия — тот самый механизм, которым число из сида
        стало причиной на экране. Факт доезжает как `source_ref`.
        """
        template = ServiceTemplate.objects.create(name="Массаж лимфодренажный", category=category)
        master = _specialist(tenant, suffix="0003", name="Подтверждён человеком")
        salon_service = _offer(tenant, master, category, name="Лимфодренаж", template=template)
        DraftSalonService.objects.create(
            tenant=tenant, status=DraftSalonService.Status.CONFIRMED,
            external_name="Лимфодренаж", suggested_template=template,
            confirmed_salon_service=salon_service,
        )

        facts = _fetch()
        assert facts[0].mapping_status is MappingStatus.REVIEW_REQUIRED
        assert facts[0].source_ref == f"draft_confirmed:{salon_service.id}"

    def test_verified_is_issued_to_nobody(self, tenant, category):
        """Сквозная проверка: `VERIFIED` не выдаётся ни в одной комбинации."""
        template = ServiceTemplate.objects.create(name="Шаблон", category=category)
        plain = _specialist(tenant, suffix="0004", name="Без шаблона")
        mapped = _specialist(tenant, suffix="0005", name="С шаблоном")
        _offer(tenant, plain, category, name="Услуга А")
        _offer(tenant, mapped, category, name="Услуга Б", template=template)

        assert all(f.mapping_status is not MappingStatus.VERIFIED for f in _fetch())


@pytest.mark.django_db
class TestSemanticFacts:
    def test_full_name_match_is_exact(self, tenant, category):
        master = _specialist(tenant, suffix="0010", name="Мастер")
        _offer(tenant, master, category, name="Массаж")

        facts = _fetch(need=_need(raw_text="массаж"))
        assert facts[0].match_level is MatchLevel.SERVICE_EXACT
        assert facts[0].matched_service_ref is not None

    def test_substring_is_partial_not_exact(self, tenant, category):
        """Подстрока — не точность.

        Настоящая точность совпадения приезжает с T11; выдавать за неё
        подстроку значило бы обещать больше, чем делает код.
        """
        master = _specialist(tenant, suffix="0011", name="Мастер")
        _offer(tenant, master, category, name="Массаж спины глубокий")

        assert _fetch(need=_need(raw_text="массаж"))[0].match_level is MatchLevel.SERVICE_PARTIAL

    def test_no_need_leaves_match_undetermined(self, tenant, category):
        """Нужда не названа — соответствие НЕ вычислялось, а не равно нулю."""
        master = _specialist(tenant, suffix="0012", name="Мастер")
        _offer(tenant, master, category, name="Массаж")

        facts = _fetch(need=_need())
        assert facts[0].match_level is MatchLevel.UNDETERMINED

    def test_unrelated_need_leaves_match_undetermined(self, tenant, category):
        master = _specialist(tenant, suffix="0013", name="Мастер")
        _offer(tenant, master, category, name="Массаж")

        assert _fetch(need=_need(raw_text="маникюр"))[0].match_level is MatchLevel.UNDETERMINED


@pytest.mark.django_db
class TestRatingFacts:
    def test_rating_travels_with_review_count(self, tenant, category):
        master = _specialist(tenant, suffix="0020", name="С рейтингом", rating="4.9", reviews=0)
        _offer(tenant, master, category, name="Массаж")

        rating = _fetch()[0].rating
        assert rating.rating == Decimal("4.9")
        assert rating.review_count == 0

    def test_zero_rating_is_absence_of_data_not_a_low_score(self, tenant, category):
        """Ноль — «оценки нет», а не «оценка ноль» (§29.4, DRF-1535).

        Столбец `NOT NULL` с умолчанием `0.0`: на уровне схемы отсутствие
        и ноль неотличимы. Пропусти адаптер эту разницу — мастер без
        единой оценки получил бы свидетельство UNSUBSTANTIATED («оценка
        есть, но не подтверждена») вместо UNKNOWN («оценки нет»), то есть
        мы сообщили бы о нём то, чего никто не измерял.
        """
        master = _specialist(tenant, suffix="0021", name="Без рейтинга")
        _offer(tenant, master, category, name="Массаж")

        assert _fetch()[0].rating is None


@pytest.mark.django_db
class TestScopeIsAFilter:
    def test_tenant_scope_narrows(self, tenant, category):
        other = Tenant.objects.create(slug="src-other", name="Другой", is_active=True)
        here = _specialist(tenant, suffix="0030", name="Здесь")
        there = _specialist(other, suffix="0031", name="Там")
        _offer(tenant, here, category, name="Массаж")
        _offer(other, there, category, name="Массаж")

        facts = _fetch(scope=Scope(ScopeMode.MARKETPLACE, tenant_refs=(tenant.id,)))
        assert [f.ref.id for f in facts] == [here.id]

    def test_exclude_tenant_scope_narrows_the_other_way(self, tenant, category):
        other = Tenant.objects.create(slug="src-other-2", name="Другой", is_active=True)
        here = _specialist(tenant, suffix="0032", name="Здесь")
        there = _specialist(other, suffix="0033", name="Там")
        _offer(tenant, here, category, name="Массаж")
        _offer(other, there, category, name="Массаж")

        facts = _fetch(scope=Scope(ScopeMode.MARKETPLACE, exclude_tenant_refs=(tenant.id,)))
        assert [f.ref.id for f in facts] == [there.id]

    def test_inactive_tenant_is_out_of_the_pool(self, category):
        dead = Tenant.objects.create(slug="src-dead", name="Отключён", is_active=False)
        master = _specialist(dead, suffix="0034", name="В мёртвом салоне")
        _offer(dead, master, category, name="Массаж")

        assert _fetch() == []


@pytest.mark.django_db
class TestSourceDoesNotRank:
    def test_facts_carry_no_score(self, tenant, category):
        """У фактов нет балла. Не «не используется» — его нет как поля."""
        master = _specialist(tenant, suffix="0040", name="Мастер", rating="5.0", reviews=99)
        _offer(tenant, master, category, name="Массаж")

        facts = _fetch()
        assert not hasattr(facts[0], "score")
        assert not hasattr(facts[0], "match_score")

    def test_candidates_are_providers_and_say_so(self, tenant, category):
        master = _specialist(tenant, suffix="0041", name="Мастер")
        _offer(tenant, master, category, name="Массаж")

        assert _fetch()[0].ref.kind is CandidateKind.PROVIDER


@pytest.mark.django_db
def test_factory_returns_a_fresh_source_each_call():
    """Источник живёт одно решение: доменная правда не кешируется дольше."""
    assert build_candidate_source() is not build_candidate_source()
