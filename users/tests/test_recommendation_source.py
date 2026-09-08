"""Доменный адаптер резолвера — `users/recommendation_source.py`. T6/T8.

Проверяется ровно то, за что адаптер отвечает: **какие факты он говорит**.
Порядок он не задаёт, и тестов на порядок здесь нет по построению — они
живут в `recommendation/tests`, потому что порядок живёт в резолвере.

Главное, что здесь стережётся после §76: адаптер **читает статус связи
полем, а не выводит его из наличия строк**. Синтез и запись — два ответа
на один вопрос, и разошлись бы они в первый же день, когда кто-нибудь
подтвердит связь: поле сказало бы `VERIFIED`, а адаптер продолжал бы
выводить `REVIEW_REQUIRED` из наличия шаблона.

Второе: статус берётся **у совпавшей услуги**, а не лучший по мастеру.
Допустить мастера по проверенной связи услуги Б в ответ на вопрос про
услугу А — подстановка другого предмета (§14.4).
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from recommendation.api import CandidateKind, MappingStatus, MatchLevel, NeedOrigin, NeedSpec, Scope, ScopeMode
from services.models import (
    DraftSalonService,
    Service,
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


def _offer(
    tenant, specialist, category, *, name: str, template=None,
    mapping_status=SalonService.MappingStatus.UNMAPPED,
) -> SalonService:
    """Предложение мастера со статусом связи (§76).

    Умолчание — `UNMAPPED`, а не `VERIFIED`: фикстура не должна раздавать
    допуск молча. Тест, которому нужен допущенный кандидат, называет
    статус вслух, и в его теле видно, за счёт чего он зелёный.
    """
    provenance = {} if mapping_status != SalonService.MappingStatus.VERIFIED else {
        "mapping_confirmed_rule": "test_fixture",
        "mapping_rule_version": "1.0.0",
        "mapping_confirmed_at": timezone.now(),
        "mapping_source_ref": "fixture:test_recommendation_source",
    }
    salon_service = SalonService.objects.create(
        tenant=tenant, name=name, category=category, template=template,
        duration_minutes=60, is_active=True,
        mapping_status=mapping_status, **provenance,
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
class TestCandidateKey:
    """Каким ключом источник называет кандидата. Проверка стоит дороже вида.

    За границей кандидата ищут в зеркале бота по `CatalogMaster.
    ayla_user_id` — это ключ ПОЛЬЗОВАТЕЛЯ. Источник же держит в руках
    профиль, и его первичный ключ — другой UUID того же человека.

    Ошибка здесь не ломает ни один прогон: обе стороны сравниваются
    только на живом контуре, а в фикстурах обе половины кладёт один
    автор — и они, конечно, сходятся. Поэтому замер ниже стоит прямо
    в теле теста: он единственное, что отличает эту проверку
    от проверки согласованности фикстуры.
    """

    def test_candidate_is_named_by_the_user_key_not_the_profile_key(
        self, tenant, category,
    ):
        """Замер пилота 08.09, воспроизведённый на двух строках.

        На боевом контуре множества были по 31 элементу с обеих сторон,
        а пересечение по профильному ключу — **пустое**: перевод не дал
        бы ни одного совпадения ни на каких данных, и полка сказала бы
        человеку «зеркало отстало».

        Здесь то же самое в миниатюре. `mirror` — то, что хранит зеркало
        (пользовательские ключи). Проверяется не «взяли user_id», а что
        **пересечение по профильному ключу пусто, а по пользовательскому
        полно** — то есть ровно та величина, которая была нулём.
        """
        first = _specialist(tenant, suffix="0100", name="Первый")
        second = _specialist(tenant, suffix="0101", name="Второй")
        _offer(tenant, first, category, name="Массаж")
        _offer(tenant, second, category, name="Массаж")

        # Предусловие: ключи РАЗНЫЕ. Без него тест зеленел бы и на схеме,
        # где профиль и пользователь — одно и то же значение.
        assert first.id != first.user_id
        assert second.id != second.user_id

        mirror = {str(first.user_id), str(second.user_id)}
        emitted = {str(f.ref.id) for f in _fetch()}
        by_profile = {str(first.id), str(second.id)}

        assert len(emitted & mirror) == 2, "по пользовательскому ключу — все"
        assert len(by_profile & mirror) == 0, "по профильному — пересечение пусто"
        assert emitted == mirror


@pytest.mark.django_db
class TestMappingStatus:
    def test_status_is_read_from_the_field_not_inferred_from_a_template(
        self, tenant, category,
    ):
        """Наличие шаблона больше ничего не решает — решает записанный статус.

        Раньше адаптер выводил: есть шаблон → `REVIEW_REQUIRED`, нет →
        `UNMAPPED`. После §76 статус записан полем, и вывод обязан уйти:
        два источника одного ответа рано или поздно расходятся.

        Здесь шаблон ЕСТЬ, а статус — `UNMAPPED`. Прежний код сказал бы
        `REVIEW_REQUIRED`, то есть соврал бы про домен в сторону, которая
        выглядит безобиднее, чем есть.
        """
        template = ServiceTemplate.objects.create(name="Массаж классический", category=category)
        master = _specialist(tenant, suffix="0002", name="Шаблон есть, статуса нет")
        _offer(
            tenant, master, category, name="Массаж", template=template,
            mapping_status=SalonService.MappingStatus.UNMAPPED,
        )

        assert [f.mapping_status for f in _fetch()] == [MappingStatus.UNMAPPED]

    def test_verified_is_read_through(self, tenant, category):
        """Положительная стража: `VERIFIED` доезжает, когда он записан.

        Без неё все проверки ниже зеленели бы и на адаптере, который
        просто не умеет отдавать `VERIFIED`, — а именно так он и работал
        до §76, и отличить одно от другого можно только этим тестом.
        """
        master = _specialist(tenant, suffix="0006", name="Подтверждена")
        _offer(
            tenant, master, category, name="Массаж",
            mapping_status=SalonService.MappingStatus.VERIFIED,
        )

        assert [f.mapping_status for f in _fetch()] == [MappingStatus.VERIFIED]

    def test_status_belongs_to_the_matched_service_not_to_the_best_one(
        self, tenant, category,
    ):
        """§14.4: подтверждённая услуга Б не отвечает за вопрос об услуге А.

        Мастер предлагает две услуги: «Массаж» с непроверенной связью и
        «Педикюр» с подтверждённой. Человек спросил массаж. Взять лучший
        статус по мастеру значило бы допустить его к рекомендации за счёт
        услуги, о которой не спрашивали, — то есть подставить другой
        предмет, ровно как «полка услуг молча приняла мастера».
        """
        master = _specialist(tenant, suffix="0007", name="Смешанная")
        _offer(
            tenant, master, category, name="Массаж",
            mapping_status=SalonService.MappingStatus.REVIEW_REQUIRED,
        )
        _offer(
            tenant, master, category, name="Педикюр",
            mapping_status=SalonService.MappingStatus.VERIFIED,
        )

        asked_massage = _fetch(need=_need(raw_text="массаж"))
        asked_pedicure = _fetch(need=_need(raw_text="педикюр"))

        assert [f.mapping_status for f in asked_massage] == [MappingStatus.REVIEW_REQUIRED]
        assert [f.mapping_status for f in asked_pedicure] == [MappingStatus.VERIFIED]

    def test_a_legacy_mirror_does_not_shadow_the_verified_canonical_row(
        self, tenant, category,
    ):
        """Лишняя строка в старом слое не должна решать допуск.

        Регрессия, которую поймал CI, а не рассуждение. Услуга бывает
        продублирована в обоих слоях: каноническая строка с подтверждённой
        связью и легаси-зеркало с тем же названием. Легаси связи не имеет
        по устройству и в списке идёт **первой** — и правило «статус
        у совпавшей услуги», взятое буквально, выбрасывало мастера из
        подбора.

        Причина отказа была бы неправдой: не «связь не проверена»,
        а «у него есть лишняя строка в старом слое». Порядок в списке
        решал бы допуск.

        Поэтому из ОДИНАКОВО совпавших выбирается лучшая по статусу.
        Подменой предмета это не является: выбор идёт только среди тех
        услуг, которые отвечают нужде.
        """
        master = _specialist(tenant, suffix="0009", name="Зеркало в двух слоях")
        _offer(
            tenant, master, category, name="Массаж",
            mapping_status=SalonService.MappingStatus.VERIFIED,
        )
        Service.objects.create(
            specialist=master, category=category, name="Массаж",
            price=Decimal("1500"), duration_minutes=60, is_active=True,
        )

        assert [f.mapping_status for f in _fetch()] == [MappingStatus.VERIFIED]

    def test_a_legacy_mirror_does_not_launder_an_unverified_canonical_row(
        self, tenant, category,
    ):
        """Обратная стража: выбор лучшего не превращается в допуск.

        Без неё правка выше зеленела бы и на коде, который просто
        отдаёт `VERIFIED`, найдя его где угодно у мастера. Здесь
        подтверждённой строки нет ни одной — и лучший из совпавших
        честно остаётся `REVIEW_REQUIRED`.
        """
        master = _specialist(tenant, suffix="0010", name="Зеркало без подтверждения")
        _offer(
            tenant, master, category, name="Массаж",
            mapping_status=SalonService.MappingStatus.REVIEW_REQUIRED,
        )
        Service.objects.create(
            specialist=master, category=category, name="Массаж",
            price=Decimal("1500"), duration_minutes=60, is_active=True,
        )

        assert [f.mapping_status for f in _fetch()] == [MappingStatus.REVIEW_REQUIRED]

    def test_without_a_stated_need_the_best_offer_answers_for_the_master(
        self, tenant, category,
    ):
        """Нужда не названа — предмет сам мастер, и одной проверенной хватает.

        Полка «твои салоны» нужду не передаёт: её якорь — отношения,
        а не то, что человек ищет сейчас. Требовать там совпадения
        значило бы отфильтровать полку по цели, чего она никогда
        не делала.
        """
        master = _specialist(tenant, suffix="0008", name="Смешанная без нужды")
        _offer(
            tenant, master, category, name="Массаж",
            mapping_status=SalonService.MappingStatus.REVIEW_REQUIRED,
        )
        _offer(
            tenant, master, category, name="Педикюр",
            mapping_status=SalonService.MappingStatus.VERIFIED,
        )

        facts = _fetch(need=NeedSpec(origin=NeedOrigin.MEMORY))
        assert [f.mapping_status for f in facts] == [MappingStatus.VERIFIED]

    def test_human_confirmed_draft_still_does_not_grant_the_status(
        self, tenant, category,
    ):
        """Подтверждённый черновик — свидетельство, а не статус.

        Он доезжает как `source_ref` и остаётся видимым, но статус даёт
        только поле. На пилоте это и подтвердилось данными: `confirmed_at`
        заполнен у всех 58 драфтов, `confirmed_by` — ни у одного, то есть
        подтверждение состоялось, а подтвердившего нет (замер 08.09).
        """
        template = ServiceTemplate.objects.create(name="Массаж лимфодренажный", category=category)
        master = _specialist(tenant, suffix="0003", name="Подтверждён человеком")
        salon_service = _offer(
            tenant, master, category, name="Лимфодренаж", template=template,
            mapping_status=SalonService.MappingStatus.REVIEW_REQUIRED,
        )
        DraftSalonService.objects.create(
            tenant=tenant, status=DraftSalonService.Status.CONFIRMED,
            external_name="Лимфодренаж", suggested_template=template,
            confirmed_salon_service=salon_service,
        )

        facts = _fetch(need=_need(raw_text="лимфодренаж"))
        assert facts[0].mapping_status is MappingStatus.REVIEW_REQUIRED
        assert facts[0].source_ref == f"draft_confirmed:{salon_service.id}"


@pytest.mark.django_db
class TestBothCatalogLayers:
    """Допустимость читается по ОБОИМ слоям каталога.

    Репозиторий уже дважды лечил этот дефект (S3-EMPTY, 30.08: «обе
    поверхности теперь читают ОБА слоя»), и адаптер завёл его заново:
    он спрашивал про услуги канонический слой, а про соответствие нужде —
    оба. Мастер с услугами только в легаси выглядел как мастер БЕЗ услуг
    и выбывал на S1 с кодом «неактивен» — притом что каталог его
    показывает и записаться к нему можно.
    """

    def test_legacy_only_master_is_admitted(self, tenant, category):
        master = _specialist(tenant, suffix="0050", name="Только легаси")
        Service.objects.create(
            specialist=master, category=category, name="Массаж",
            price=Decimal("1500"), duration_minutes=60, is_active=True,
        )

        facts = _fetch()
        assert len(facts) == 1
        assert facts[0].is_active_offer is True
        assert facts[0].is_capable is True

    def test_legacy_only_master_is_unmapped_not_unknown(self, tenant, category):
        """Услуга есть, шаблона у неё нет по устройству слоя.

        `UNMAPPED` — факт о связи, а не приговор мастеру: рекомендовать
        его нельзя ровно потому, что связь не проверял никто.
        `UNKNOWN` означал бы «услуг нет вовсе», и это было бы неправдой.
        """
        master = _specialist(tenant, suffix="0051", name="Легаси без шаблона")
        Service.objects.create(
            specialist=master, category=category, name="Массаж",
            price=Decimal("1500"), duration_minutes=60, is_active=True,
        )

        assert _fetch()[0].mapping_status is MappingStatus.UNMAPPED

    def test_master_without_any_service_is_unknown(self, tenant, category):
        """Ни одной услуги ни в одном слое — вот это `UNKNOWN`."""
        _specialist(tenant, suffix="0052", name="Совсем без услуг")

        facts = _fetch()
        assert facts[0].is_active_offer is False
        assert facts[0].mapping_status is MappingStatus.UNKNOWN

    def test_legacy_service_name_matches_the_need(self, tenant, category):
        """Соответствие нужде тоже видит легаси — иначе слои разъедутся."""
        master = _specialist(tenant, suffix="0053", name="Легаси")
        Service.objects.create(
            specialist=master, category=category, name="Массаж",
            price=Decimal("1500"), duration_minutes=60, is_active=True,
        )

        assert _fetch(need=_need(raw_text="массаж"))[0].match_level is MatchLevel.SERVICE_EXACT


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
        assert [f.ref.id for f in facts] == [here.user_id]

    def test_exclude_tenant_scope_narrows_the_other_way(self, tenant, category):
        other = Tenant.objects.create(slug="src-other-2", name="Другой", is_active=True)
        here = _specialist(tenant, suffix="0032", name="Здесь")
        there = _specialist(other, suffix="0033", name="Там")
        _offer(tenant, here, category, name="Массаж")
        _offer(other, there, category, name="Массаж")

        facts = _fetch(scope=Scope(ScopeMode.MARKETPLACE, exclude_tenant_refs=(tenant.id,)))
        assert [f.ref.id for f in facts] == [there.user_id]

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
