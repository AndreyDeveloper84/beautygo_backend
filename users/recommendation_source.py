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

Каким ключом называется кандидат
--------------------------------
`CandidateRef(PROVIDER, ...)` несёт **ключ пользователя** (`user_id`),
а не первичный ключ профиля. У одного человека это два разных UUID,
и за границей знают только первый: зеркало бота хранит
`CatalogMaster.ayla_user_id` и по нему же решает, продаётся ли мастер
(предикат `AVAILABLE`, DRF-1540).

Здесь стоял `specialist.id`, и это давало **пустое пересечение**
с зеркалом: замер пилота 08.09 — по 31 элементу с обеих сторон,
общих ноль. Перевод не сработал бы никогда, ни на каких данных,
а человеку полка сказала бы «зеркало отстало».

Ошибка держалась на правдоподобии: `ayla_user_id` читается и как
«идентификатор пользователя Ayla», и как «идентификатор из Ayla».
Контракт §5 теперь говорит это производителю ключа, а не только
потребителю.

Про статус маппинга — важное
----------------------------
Статус **читается полем** `SalonService.mapping_status` (§76). Раньше он
здесь синтезировался из наличия строк — «есть шаблон, значит
`REVIEW_REQUIRED`». Синтез и запись — два ответа на один вопрос, и они
разошлись бы в первый же день, когда кто-нибудь подтвердит связь: поле
сказало бы `VERIFIED`, а источник продолжал бы выводить
`REVIEW_REQUIRED` из шаблона.

**Статус берётся у той услуги, которая совпала с нуждой**, а не лучший
по мастеру. Разница не формальная: допустить мастера по проверенной
связи услуги Б, когда человек спросил про услугу А, — это подстановка
другого предмета (канон §14.4), тот же класс, что «полка услуг молча
приняла мастера». Если нужда не названа, спрашивается лучший статус
среди активных предложений: тогда предмет — сам мастер, и достаточно
одной проверенной способности.

Легаси-услуга канонической связи не имеет **по устройству слоя** —
значит `UNMAPPED`. Это факт о связи, а не приговор мастеру.

Отсутствие предложений вообще — `UNKNOWN`, а не `UNMAPPED`: «мы не
знаем» и «мы знаем, что связи нет» — разные утверждения, и оба
fail-closed, но по разным причинам.

Следствие на пилоте: `VERIFIED` после миграции нет ни у кого, выдача
пуста, и это **штатное состояние с именем** (§76), а не поломка.
Снимается оно подтверждением связей, а не настройкой: пути, которым
непроверенная связь попадала бы в подбор, больше нет (T16).
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
            # Ключ — `salon.id`, потому что именно его кладёт в `id`
            # канонический слой `catalog_services_for`. Совпадение по
            # нужде вернёт этот же ключ, и статус найдётся по нему —
            # без второго чтения и без догадки, какая услуга совпала.
            facts.status_by_service[salon.id] = salon.mapping_status
            if salon.template_id is not None:
                facts.has_template = True
                if salon.id in confirmed_salon_ids:
                    facts.human_confirmed_ref = f"draft_confirmed:{salon.id}"
            # `resolved_requires_health_check()` трёхзначен: True / False /
            # None («домен не знает»). Здесь `None` схлопывается в `False`
            # ЯВНО, а не падением в falsy.
            #
            # **Решение владельца 09.09.2026 (§72): оставляем как есть, гейт
            # стоит на записи.** Витрина показывает кандидата, про здоровье
            # которого домен не знает; закрывает такую бронь сторож на пути
            # создания записи (`appointments/application/services/
            # _booking_guards.py`), а не эта полка.
            #
            # Причина, без которой вердикт читается как недосмотр. Погасшая
            # полка ничего не предотвращает: тот же мастер виден и бронируем
            # в обычном каталоге одним тапом, поэтому закрытым оказывается
            # ОБЪЯСНЕНИЕ, а не действие. Поднять `None` до «чувствительно»
            # означало бы погасить полку для каждой услуги без шаблона — на
            # пилоте это все 58 услуг живого салона — и выдать за
            # безопасность то, что безопасностью не является.
            #
            # Поэтому: не «пока не решили», а «решено, и вот почему». Менять
            # это поведение — значит переоткрывать §72, а не чинить недосмотр.
            if link.resolved_requires_health_check() is True:
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
            services, needle=needle, goal_categories=goal_categories, mapping=mapping,
        )
        return CandidateFacts(
            # Ключ ПОЛЬЗОВАТЕЛЯ, а не профиля. Разница не косметическая:
            # это два разных UUID одного человека, и за границей знают
            # только первый.
            #
            # Зеркало бота хранит `CatalogMaster.ayla_user_id` и по нему
            # же решает, продаётся ли мастер (предикат `AVAILABLE`,
            # DRF-1540). То есть пользовательский ключ — канонический
            # ключ продажи; профильный каноничен только внутри Ayla.
            #
            # Замер пилота 08.09: множества по 31 элементу с обеих
            # сторон, пересечение по профильному ключу — ПУСТОЕ,
            # по пользовательскому — все 31. Перевелось бы ноль, всегда,
            # на любых данных, а человеку полка сказала бы «зеркало
            # отстало» (§78: имя обвиняет источник, причина у нас).
            #
            # Резолверу этот ключ непрозрачен — он служит ключом словаря,
            # группировки и ротации, и ни одна стадия из него ничего
            # не выводит. Поэтому смена безопасна внутри и обязательна
            # снаружи.
            ref=CandidateRef(CandidateKind.PROVIDER, specialist.user_id),
            tenant_ref=specialist.tenant_id,
            city=getattr(specialist.tenant, "city", None),
            # География: координаты у профиля есть, но расстояние резолвер
            # использует ТОЛЬКО как жёсткий предел радиуса (§3.3). Слагаемым
            # оно не станет ни на одной стадии, и считать его без явного
            # радиуса незачем.
            distance_km=None,
            is_active_offer=has_offer,
            is_capable=has_offer,
            mapping_status=mapping.status(
                has_offer=has_offer, matched_service_ref=matched_service_id,
            ),
            safety_blocked=False,
            # Признак медицинской проверки доезжает до резолвера: он
            # отменяет заявление NOT_APPLICABLE (§4.1). Витрина, в которой
            # есть услуга с противопоказаниями, витриной уже не является.
            requires_health_check=mapping.requires_health_check,
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
        mapping: "_MappingFacts",
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

        **Из одинаково совпавших выбирается лучшая по статусу связи.**
        Это не «лучший статус по мастеру» — выбор идёт только среди тех
        услуг, которые действительно отвечают нужде, поэтому предмет
        не подменяется.

        Правило вынужденное, и случай, который его потребовал, стоит
        назвать: услуга, продублированная в обоих слоях каталога.
        Легаси-строка канонической связи не имеет по устройству, а в
        списке идёт первой, — и без этого правила она **перекрывала бы
        подтверждённую каноническую услугу с тем же названием**. Мастер
        выбывал бы из подбора не потому, что связь не проверена, а
        потому, что у него есть лишняя строка в старом слое: порядок
        в списке решал бы допуск.
        """
        if needle:
            exact = [s for s in services if s.name.strip().casefold() == needle]
            if exact:
                return MatchLevel.SERVICE_EXACT, mapping.best_of(exact), None

            partial = [
                s for s in services
                if needle in s.name.casefold()
                or needle in (s.category_name or "").casefold()
                or needle in (s.category_slug or "").casefold()
            ]
            if partial:
                return MatchLevel.SERVICE_PARTIAL, mapping.best_of(partial), None

        if goal_categories:
            allowed = set(goal_categories)
            in_goal = [s for s in services if s.category_id in allowed]
            if in_goal:
                chosen_id = mapping.best_of(in_goal)
                chosen = next(s for s in in_goal if s.id == chosen_id)
                return MatchLevel.GOAL_CATEGORY, chosen.id, chosen.category_id

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

    #: Порядок «лучшести» статуса. Нужен только там, где нужда не названа
    #: и предмет — сам мастер: тогда достаточно одной проверенной связи.
    #: `NOT_RECOMMENDABLE` стоит НИЖЕ `UNKNOWN`, и это не придирка.
    #: Порядок спрашивается там, где нужда не названа и предмет — сам
    #: мастер: берётся лучший статус среди его предложений. Услуга, про
    #: которую решено «канонической связи не будет», поднять мастера не
    #: может никогда, а услуга с неизвестным статусом — может завтра,
    #: когда её разберут. Закрытая дверь хуже неоткрытой.
    _RANK = {
        MappingStatus.VERIFIED: 4,
        MappingStatus.REVIEW_REQUIRED: 3,
        MappingStatus.UNMAPPED: 2,
        MappingStatus.UNKNOWN: 1,
        MappingStatus.NOT_RECOMMENDABLE: 0,
    }

    def __init__(self) -> None:
        self.has_service = False
        self.has_template = False
        self.requires_health_check = False
        self.human_confirmed_ref: str | None = None
        #: `SalonService.id` → статус связи, как он записан в домене.
        self.status_by_service: dict[UUID, str] = {}

    def status(
        self,
        *,
        has_offer: bool | None = None,
        matched_service_ref: UUID | None = None,
    ) -> MappingStatus:
        """Статус связи — прочитанный, а не выведенный (§76).

        Когда нужда названа и услуга совпала, спрашивается статус
        **именно этой** услуги. Взять лучший по мастеру значило бы
        допустить его по проверенной связи услуги Б в ответ на вопрос
        про услугу А — подстановка другого предмета (§14.4).

        Когда нужда не названа, предмет — сам мастер, и берётся лучший
        статус среди его предложений: одной проверенной способности
        достаточно, чтобы мастера было чем рекомендовать.

        Третий случай — нужда названа, но не совпало ничего — сюда тоже
        приходит с лучшим статусом, и это безвредно: такой кандидат
        выбывает раньше, на проверке способности (S1 исключает его как
        `NOT_CAPABLE`), и до вопроса о связи дело не доходит. Считать
        здесь что-то более точное значило бы отвечать на вопрос, который
        никто не задаёт.
        """
        offered = self.has_service if has_offer is None else has_offer
        if not offered:
            # «Мы не знаем» — не то же, что «мы знаем, что связи нет».
            # Оба fail-closed, но причины разные, и в свидетельстве это
            # видно.
            return MappingStatus.UNKNOWN

        if matched_service_ref is not None:
            # Легаси-строки в карте нет по устройству слоя: канонической
            # связи у неё не бывает, значит UNMAPPED — факт о связи,
            # а не приговор мастеру.
            return self._as_status(self.status_by_service.get(matched_service_ref))

        if not self.status_by_service:
            return MappingStatus.UNMAPPED
        return max(
            (self._as_status(value) for value in self.status_by_service.values()),
            key=lambda status: self._RANK[status],
        )

    def best_of(self, services) -> UUID | None:
        """Из одинаково совпавших услуг — та, чья связь доказана лучше.

        Выбор идёт **только среди совпавших**, поэтому подменой предмета
        не является: человек спросил про эту услугу, и мы отвечаем той
        её строкой, у которой связь подтверждена.

        При равенстве статусов побеждает первая — порядок
        `catalog_services_for` стабилен между репликами, значит и выбор
        воспроизводим.
        """
        if not services:
            return None
        return max(
            services,
            key=lambda s: self._RANK[self._as_status(self.status_by_service.get(s.id))],
        ).id

    @classmethod
    def _as_status(cls, raw: str | None) -> MappingStatus:
        """Значение домена в значение контракта. Незнакомое — не «наверное да».

        Домен и резолвер живут в одном процессе, но словари у них свои,
        и совпадение имён — не доказательство совпадения смысла. Статус,
        которого нет в контракте, читается как `UNMAPPED`: неизвестное
        не толкуется в пользу допуска.
        """
        if raw is None:
            return MappingStatus.UNMAPPED
        try:
            return MappingStatus(raw.upper())
        except ValueError:
            return MappingStatus.UNMAPPED


__all__ = ["SpecialistCandidateSource", "build_candidate_source"]
