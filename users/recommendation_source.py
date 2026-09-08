"""Доменная правда для резолвера рекомендаций — реализация порта `CandidateSource`.

Живёт в домене, а не в `recommendation/`, и это не вкусовщина. Резолвер
не ходит в ORM: он принимает факты и возвращает решение. Ровно это
свойство позволит вынести его в `ayla-ai-core` после пилота, не переписав
ни одной стадии (OD §53). Стой адаптер внутри резолвера — вынос означал
бы разрыв по импортам, и «модуль за границей» оказался бы сросшимся
с доменом.

Направление зависимости одностороннее: **домен знает про резолвер,
резолвер про домен — нет.**

Что этот модуль делает и чего не делает
---------------------------------------
Делает: отвечает, какие кандидаты существуют в объявленном scope и что
про них знает домен — активность, способность, цена, статус маппинга,
совпадение с нуждой, курируемая связь «цель → категории», рейтинг вместе
с числом отзывов, признак медицинской проверки.

Не делает: **не упорядочивает**. Порядок целиком в резолвере. Порядок,
в котором этот модуль вернёт кандидатов, резолвером стирается
канонической пересортировкой — иначе источник влиял бы на выдачу,
оставаясь «просто источником», то есть был бы четвёртым авторитетом.

Про статус маппинга — важное
----------------------------
Шкалы `VERIFIED / REVIEW_REQUIRED / UNMAPPED` в схеме **нет** (замер
главного окна 07.09: 206 из 265 услуг связаны с шаблоном, признака
доверия к связи не существует как поля). Поэтому:

* нет шаблона → `UNMAPPED`;
* шаблон есть → `REVIEW_REQUIRED`, **даже когда связь подтверждена
  человеком** через `DraftSalonService(status=confirmed)`.

Второе — сознательный отказ решать за владельца. Машина подтверждения
человеком действительно похожа на `REVIEW_REQUIRED → VERIFIED`, но
объявить её таковой значило бы ответить реализацией на открытый вопрос
владельца (OD §40.4 п.1) — тем же способом, каким литерал рейтинга
в сиде однажды стал «свидетельством» на экране. Факт подтверждения
доезжает до резолвера как `source_ref`, то есть виден в свидетельстве
и готов к тому, чтобы владелец его повысил.

Следствие сегодня: `recommendation_eligible` нет ни у кого, выдача
пуста, и это **честное** состояние по §10.3, а не поломка. Включается
одним решением владельца — политикой `mapping_override_enabled` (§10.4).
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Sequence
from uuid import UUID

from goals.wiring import goal_category_ids_for_key
from recommendation.api import (
    CandidateFacts,
    CandidateKind,
    CandidateRef,
    MappingStatus,
    MatchLevel,
    NeedSpec,
    RatingValue,
    ScheduleState,
    Scope,
)
from services.catalog_reads import catalog_services_for, catalog_services_prefetch
from services.models import DraftSalonService, SpecialistService
from users.models import SpecialistProfile

logger = logging.getLogger(__name__)


def build_candidate_source() -> "SpecialistCandidateSource":
    """Фабрика для ``settings.RECOMMENDATION_CANDIDATE_SOURCE``.

    Новый экземпляр на каждый вызов: источник держит запросы и разрешение
    цели на время одного решения, и переиспользовать его между запросами
    значило бы кешировать доменную правду дольше, чем она верна.
    """
    return SpecialistCandidateSource()


class SpecialistCandidateSource:
    """`CandidateSource` над `SpecialistProfile`. Провайдеры, `kind=PROVIDER`."""

    def fetch(self, *, scope: Scope, need: NeedSpec) -> Sequence[CandidateFacts]:
        pool = self._pool(scope)
        specialists = list(pool)
        if not specialists:
            return []

        mapping = self._mapping_by_specialist([s.id for s in specialists])
        goal_categories = goal_category_ids_for_key(need.goal_key)
        needle = (need.raw_text or "").strip().casefold()

        facts = [
            self._facts_for(
                specialist,
                mapping=mapping.get(specialist.id, _MappingFacts()),
                needle=needle,
                goal_categories=goal_categories,
            )
            for specialist in specialists
        ]
        logger.info(
            "recommendation.source pool=%d goal_key=%r goal_categories=%d needle=%r",
            len(facts), need.goal_key, len(goal_categories or ()), needle[:32],
        )
        return facts

    # -- scope: границы поиска, не баллы ----------------------------------

    def _pool(self, scope: Scope):
        """Активный бронируемый мастер в живом салоне — и только это.

        Те же условия, что у прежнего `_base_pool`: доменная допустимость,
        а не политика. Любое дополнительное условие здесь стало бы
        отбором, то есть политикой, то есть авторитетом.
        """
        qs = (
            SpecialistProfile.objects
            .filter(
                is_available=True,
                is_booking_enabled=True,
                status=SpecialistProfile.ProfileStatus.ACTIVE,
                tenant__is_active=True,
            )
            .select_related("tenant")
            .prefetch_related(*catalog_services_prefetch())
        )
        if scope.tenant_refs:
            qs = qs.filter(tenant_id__in=scope.tenant_refs)
        if scope.exclude_tenant_refs:
            qs = qs.exclude(tenant_id__in=scope.exclude_tenant_refs)
        if scope.city:
            qs = qs.filter(tenant__city__iexact=scope.city)
        return qs

    # -- маппинг и медицинская проверка -----------------------------------

    def _mapping_by_specialist(self, specialist_ids: list[UUID]) -> dict[UUID, "_MappingFacts"]:
        """Статус маппинга и признак health-check — одним проходом.

        Два запроса на весь пул, а не по одному на мастера: пул на пилоте
        мал, но форма запроса не должна зависеть от того, мал он сегодня
        или нет.
        """
        links = list(
            SpecialistService.objects
            .filter(
                specialist_id__in=specialist_ids,
                is_active=True,
                salon_service__is_active=True,
            )
            .select_related("salon_service", "salon_service__template")
        )
        confirmed_salon_ids = set(
            DraftSalonService.objects
            .filter(
                status=DraftSalonService.Status.CONFIRMED,
                confirmed_salon_service_id__in={link.salon_service_id for link in links},
            )
            .exclude(suggested_template__isnull=True)
            .values_list("confirmed_salon_service_id", flat=True)
        )

        out: dict[UUID, _MappingFacts] = {}
        for link in links:
            salon = link.salon_service
            facts = out.setdefault(link.specialist_id, _MappingFacts())
            facts.has_service = True
            if salon.template_id is not None:
                facts.has_template = True
                if salon.id in confirmed_salon_ids:
                    facts.human_confirmed_ref = f"draft_confirmed:{salon.id}"
            if link.resolved_requires_health_check():
                facts.requires_health_check = True
        return out

    # -- факты про одного кандидата ---------------------------------------

    def _facts_for(
        self,
        specialist: SpecialistProfile,
        *,
        mapping: "_MappingFacts",
        needle: str,
        goal_categories: tuple[UUID, ...] | None,
    ) -> CandidateFacts:
        # ОБА слоя каталога, а не только канонический.
        #
        # Здесь стояло `mapping.has_service`, то есть наличие строки
        # `SpecialistService`. Мастер, у которого услуги только в легаси
        # `Service`, выглядел как мастер БЕЗ услуг и выбывал на S1 с кодом
        # «неактивен» — притом что каталог его показывает и записаться
        # к нему можно.
        #
        # Это ровно тот дефект, который репозиторий уже дважды лечил
        # (S3-EMPTY, 30.08: «обе поверхности теперь читают ОБА слоя»),
        # и я завёл его заново, читая один слой для допустимости и оба —
        # для соответствия нужде. Один источник на оба вопроса.
        services = catalog_services_for(specialist)
        has_offer = bool(services)

        match_level, matched_service_id, matched_category_id = self._match(
            services, needle=needle, goal_categories=goal_categories,
        )
        return CandidateFacts(
            ref=CandidateRef(CandidateKind.PROVIDER, specialist.id),
            tenant_ref=specialist.tenant_id,
            city=getattr(specialist.tenant, "city", None),
            # География: координаты у профиля есть, но расстояние резолвер
            # использует ТОЛЬКО как жёсткий предел радиуса (§3.3). Слагаемым
            # оно не станет ни на одной стадии, и считать его без явного
            # радиуса незачем.
            distance_km=None,
            is_active_offer=has_offer,
            is_capable=has_offer,
            mapping_status=mapping.status(has_offer=has_offer),
            safety_blocked=False,
            price=None,
            match_level=match_level,
            matched_service_ref=matched_service_id,
            matched_goal_category_ref=matched_category_id,
            is_bookable=bool(specialist.is_booking_enabled),
            # Расписание: подтверждать нечем — `WorkingHours` заполнены
            # у четырёх мастеров из тридцати одного (§29.5). Третье
            # состояние, а не «занят».
            schedule_state=ScheduleState.UNCONFIRMED,
            prior_completed_visit=False,
            prior_completed_same_category=False,
            rating=self._rating(specialist),
            source_ref=mapping.human_confirmed_ref,
        )

    def _match(
        self,
        services: list,
        *,
        needle: str,
        goal_categories: tuple[UUID, ...] | None,
    ) -> tuple[MatchLevel, UUID | None, UUID | None]:
        """Соответствие нужде на сегодняшних данных. Честно слабое.

        Настоящая точность совпадения — стемминг и `_match_precision` из
        бота — приезжает с T11 (DRF-1572). До неё здесь подстрока по
        названию, и выдавать её за большее нельзя: `SERVICE_EXACT`
        выдаётся только при полном совпадении названия, всё остальное —
        `SERVICE_PARTIAL`.

        Курируемая связь «цель → категории» (T8) сильнее подстроки по
        происхождению — это знание владельца, а не догадка о словах, —
        но слабее прямого совпадения услуги: человек, назвавший услугу,
        сказал о себе больше, чем выбравший цель когда-то.

        Услуги приходят аргументом, а не читаются заново: два чтения — два
        ответа на один вопрос, и рано или поздно они разойдутся.
        """
        if needle:
            for service in services:
                if service.name.strip().casefold() == needle:
                    return MatchLevel.SERVICE_EXACT, service.id, None
            for service in services:
                if needle in service.name.casefold():
                    return MatchLevel.SERVICE_PARTIAL, service.id, None
                if needle in (service.category_name or "").casefold():
                    return MatchLevel.SERVICE_PARTIAL, service.id, None
                if needle in (service.category_slug or "").casefold():
                    return MatchLevel.SERVICE_PARTIAL, service.id, None

        if goal_categories:
            allowed = set(goal_categories)
            for service in services:
                if service.category_id in allowed:
                    return MatchLevel.GOAL_CATEGORY, service.id, service.category_id

        return MatchLevel.UNDETERMINED, None, None

    @staticmethod
    def _rating(specialist: SpecialistProfile) -> RatingValue | None:
        """Оценка ВМЕСТЕ с числом отзывов — порознь они не ходят (§8.4 E2).

        **Ноль — это отсутствие данных, а не низкая оценка** (контракт §3.5,
        решение владельца §29.4). Поле в схеме `NOT NULL` с умолчанием
        `0.0`, то есть «оценки нет» и «оценка ноль» физически неотличимы
        на уровне столбца — и соседний репозиторий уже закрепил ту же
        трактовку словами: «0.00 = нет данных» (DRF-1535).

        Разница не косметическая. Пропусти мы её — мастер без единой
        оценки получил бы свидетельство `UNSUBSTANTIATED` («оценка есть,
        но не подтверждена») вместо `UNKNOWN` («оценки нет»), то есть мы
        бы сообщили о нём то, чего никто не измерял.
        """
        if specialist.rating is None or Decimal(specialist.rating) == 0:
            return None
        return RatingValue(Decimal(specialist.rating), specialist.reviews_count or 0)


class _MappingFacts:
    """Что домен знает про каноническую связь услуг мастера."""

    def __init__(self) -> None:
        self.has_service = False
        self.has_template = False
        self.requires_health_check = False
        self.human_confirmed_ref: str | None = None

    def status(self, *, has_offer: bool | None = None) -> MappingStatus:
        """Шкалы доверия в схеме нет — значит `VERIFIED` не выдаётся никому.

        Не «пока не выдаётся из осторожности»: выдать его сейчас значило бы
        назвать проверенным то, чего никто не проверял именно в этом
        смысле. Признак подтверждения человеком уезжает в `source_ref`
        и ждёт решения владельца (§40.4 п.1).
        """
        offered = self.has_service if has_offer is None else has_offer
        if not offered:
            return MappingStatus.UNKNOWN
        # Легаси-услуга шаблона не имеет по устройству слоя — значит
        # UNMAPPED. Это факт о связи, а не приговор мастеру: рекомендовать
        # его нельзя ровно потому, что связь не проверял никто.
        return MappingStatus.REVIEW_REQUIRED if self.has_template else MappingStatus.UNMAPPED


__all__ = ["SpecialistCandidateSource", "build_candidate_source"]
