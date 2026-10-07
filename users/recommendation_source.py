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

from goals.wiring import goal_category_positions_for_key
from recommendation.api import (
    CandidateFacts,
    CandidateKind,
    CandidateRef,
    LegalGate,
    MappingStatus,
    MatchLevel,
    NeedSpec,
    RatingValue,
    ScheduleState,
    Scope,
    ScopeMode,
)
from services import body_care_address, body_care_license, body_care_qualification
from services.body_care_address import address_states
from services.body_care_license import license_states
from services.body_care_qualification import qualification_states
from services.body_care_validation import NOT_SUBJECT, READY_FOR_SCREENING, VALIDATION_STATES, validation_states
from services.capabilities import template_ids_helping_goal
from services.catalog_reads import catalog_services_for, catalog_services_prefetch
from services.offer_sellable import sellable_offer_q
from services.models import DraftSalonService, SpecialistService
from users.models import SpecialistProfile

logger = logging.getLogger(__name__)


def build_candidate_source(*, viewer=None) -> "SpecialistCandidateSource":
    """Фабрика для ``settings.RECOMMENDATION_CANDIDATE_SOURCE``.

    Новый экземпляр на каждый вызов: источник держит запросы и разрешение
    цели на время одного решения, и переиспользовать его между запросами
    значило бы кешировать доменную правду дольше, чем она верна.

    ``viewer`` — тот, кто спрашивает (DRF-2420): от него зависит, попадут ли в
    пул демонстрационные салоны. Ключевой аргумент необязателен, и отсутствие
    его означает правило обычного клиента — демо скрыто.
    """
    return SpecialistCandidateSource(viewer=viewer)


def config_readiness(salon_service_ids) -> dict[UUID, bool | None]:
    """Готовность конфигурации строк пула — CAT-10 (DRF-2820), шов с CAT-6.

    Одно пакетное чтение :func:`services.body_care_validation.validation_states`
    на весь пул; лестницу состояний здесь никто не повторяет — читается итог.

    ===========================================  ==========================
    ответ CAT-6                                  ``config_ready``
    ===========================================  ==========================
    ``not_subject`` (вне Body Care)              ``None`` — гейт выключен
    ``ready_for_screening``                      ``True``
    остальные состояния §7                       ``False``
    ключа нет, незнакомое значение, сбой чтения  ``False`` + ERROR в лог
    ===========================================  ==========================

    ``None`` рождается ТОЛЬКО из ``not_subject``: ``None`` значит «гейт
    выключен», и у body-care строки это был бы fail-open. Неопределённость
    закрывает строку и пишет в лог, а полку не роняет.
    """
    ids = set(salon_service_ids)
    if not ids:
        return {}
    try:
        states = validation_states(ids)
    except Exception:  # noqa: BLE001 — сбой чтения закрывает строки, а не роняет полку
        logger.exception("recommendation.source config_readiness_failed rows=%d", len(ids))
        return dict.fromkeys(ids, False)

    out: dict[UUID, bool | None] = {}
    undetermined: list[str] = []
    for pk in ids:
        state = states.get(pk)
        if state == NOT_SUBJECT:
            out[pk] = None
        elif state == READY_FOR_SCREENING:
            out[pk] = True
        else:
            out[pk] = False
            if state not in VALIDATION_STATES:
                undetermined.append(f"{pk}={state!r}")
    if undetermined:
        logger.error(
            "recommendation.source config_readiness_undetermined rows=%d closed=%s",
            len(undetermined), ",".join(sorted(undetermined)),
        )
    return out


#: Ответ §7A-2 (лицензия салона, по предложению) → значение гейта.
_LICENSE_GATE = {
    body_care_license.NOT_REQUIRED: LegalGate.CLEARED,
    body_care_license.VERIFIED: LegalGate.CLEARED,
    body_care_license.CLASS_UNCONFIRMED: LegalGate.CLASS_UNCONFIRMED,
    body_care_license.NOT_VERIFIED: LegalGate.LICENSE_NOT_VERIFIED,
    body_care_license.SCOPE_MISMATCH: LegalGate.LICENSE_SCOPE_MISMATCH,
}

#: Ответ §7A-3 (адрес, по паре мастер × предложение) → значение гейта.
#: ``no_covering_license`` сюда доходит, только если гейт лицензии выше
#: ответил «открыто» — два чтения разошлись, и это не толкуется.
_ADDRESS_GATE = {
    body_care_address.NOT_REQUIRED: LegalGate.CLEARED,
    body_care_address.VERIFIED: LegalGate.CLEARED,
    body_care_address.CLASS_UNCONFIRMED: LegalGate.CLASS_UNCONFIRMED,
    body_care_address.LOCATION_UNKNOWN: LegalGate.LOCATION_UNKNOWN,
    body_care_address.ADDRESS_MISMATCH: LegalGate.ADDRESS_MISMATCH,
}

#: Ответ §7A-4 (квалификация, по паре мастер × канон) → значение гейта.
_QUALIFICATION_GATE = {
    body_care_qualification.NOT_REQUIRED: LegalGate.CLEARED,
    body_care_qualification.VERIFIED: LegalGate.CLEARED,
    body_care_qualification.REQUIREMENT_UNCONFIRMED: LegalGate.QUALIFICATION_REQUIREMENT_UNCONFIRMED,
    body_care_qualification.NOT_VERIFIED: LegalGate.QUALIFICATION_NOT_VERIFIED,
}


def legal_gates(rows) -> dict[tuple[UUID, UUID], LegalGate]:
    """Юридические условия §7A строк пула — CAT-10-ext (DRF-2843).

    ``rows`` — тройки ``(specialist_id, salon_service_id, template_id)``;
    ответ — по паре ``(specialist_id, salon_service_id)``. Три пакетных
    чтения на весь пул, правила §7A здесь не повторяются — читается итог:
    лицензия салона (§7A-2), адрес мастера (§7A-3), квалификация мастера
    (§7A-4). Значение называет ПЕРВОЕ несошедшееся условие в этом порядке.

    Все три функции судят по подтверждённому классу канона, а не по
    семейству, поэтому гейт закрывает и медицинский класс вне Body Care.
    У строки без канона требования к квалификации нет — её не спрашивают.

    Ключа нет, незнакомое значение, сбой чтения — ``UNDETERMINED`` и ERROR
    в лог: строка закрыта, полка не падает.
    """
    rows = list(rows)
    if not rows:
        return {}
    pairs = {(specialist_id, salon_id) for specialist_id, salon_id, _ in rows}
    try:
        licenses = license_states({salon_id for _, salon_id in pairs})
        addresses = address_states(pairs)
        qualifications = qualification_states(
            {(specialist_id, template_id) for specialist_id, _, template_id in rows if template_id is not None}
        )
    except Exception:  # noqa: BLE001 — сбой чтения закрывает строки, а не роняет полку
        logger.exception("recommendation.source legal_gates_failed rows=%d", len(pairs))
        return dict.fromkeys(pairs, LegalGate.UNDETERMINED)

    out: dict[tuple[UUID, UUID], LegalGate] = {}
    undetermined: list[str] = []
    for specialist_id, salon_id, template_id in rows:
        answers = [
            _LICENSE_GATE.get(licenses.get(salon_id), LegalGate.UNDETERMINED),
            _ADDRESS_GATE.get(addresses.get((specialist_id, salon_id)), LegalGate.UNDETERMINED),
        ]
        if template_id is not None:
            qualification = qualifications.get((specialist_id, template_id))
            answers.append(
                _QUALIFICATION_GATE.get(getattr(qualification, "state", None), LegalGate.UNDETERMINED)
            )
        gate = next((a for a in answers if a is not LegalGate.CLEARED), LegalGate.CLEARED)
        out[(specialist_id, salon_id)] = gate
        if gate is LegalGate.UNDETERMINED:
            undetermined.append(f"{specialist_id}:{salon_id}")
    if undetermined:
        logger.error(
            "recommendation.source legal_gates_undetermined rows=%d closed=%s",
            len(undetermined), ",".join(sorted(undetermined)),
        )
    return out


def _category_refs(services) -> frozenset[UUID]:
    """Категории услуг — лист и его родитель (O-1, DRF-2816)."""
    refs: set[UUID] = set()
    for s in services:
        refs.update(r for r in (s.category_id, s.category_parent_id) if r)
    return frozenset(refs)


class SpecialistCandidateSource:
    """`CandidateSource` над `SpecialistProfile`. Провайдеры, `kind=PROVIDER`."""

    def __init__(self, *, viewer=None) -> None:
        # Кто спрашивает. Нужен ровно для границ показа демо-салонов
        # (DRF-2420) и больше ни для чего: баллы и порядок — не здесь.
        self._viewer = viewer

    def fetch(self, *, scope: Scope, need: NeedSpec) -> Sequence[CandidateFacts]:
        pool = self._pool(scope)
        specialists = list(pool)
        if not specialists:
            return []

        mapping = self._mapping_by_specialist([s.id for s in specialists])
        # Одно раскрытие цели на оба вопроса: какие категории (фильтр
        # совпадения) и где каждая стоит в цели (глубина, DRF-2789).
        goal_positions = goal_category_positions_for_key(need.goal_key)
        goal_categories = tuple(goal_positions) or None
        helping_templates = (
            template_ids_helping_goal(need.goal_key) if goal_categories else frozenset()
        )
        needle = (need.raw_text or "").strip().casefold()

        facts = [
            self._facts_for(
                specialist,
                mapping=mapping.get(specialist.id, _MappingFacts()),
                needle=needle,
                goal_categories=goal_categories,
                goal_positions=goal_positions,
                helping_templates=helping_templates,
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
        from users.sellable import demo_visibility_q, sellable_q

        # DRF-2420 — демо-салон живой, но не для обычного клиента. Это всё ещё
        # доменная допустимость, а не политика отбора: вопрос «кому этот салон
        # вообще показывают», а не «кто лучше». Условие не своё: один предикат
        # на все пять пулов каталога, иначе разойдётся, как разошлось «продаётся».
        qs = (
            SpecialistProfile.objects
            .filter(sellable_q(), demo_visibility_q(self._viewer), tenant__is_active=True)
            .select_related("tenant")
            .prefetch_related(*catalog_services_prefetch())
        )
        # O-1b: «свои салоны» — ограничение и при пустом списке (нет своих
        # салонов → пустой пул, а не весь маркетплейс).
        if scope.tenant_refs or scope.mode is ScopeMode.OWN_SALONS:
            qs = qs.filter(tenant_id__in=scope.tenant_refs)
        if scope.exclude_tenant_refs:
            qs = qs.exclude(tenant_id__in=scope.exclude_tenant_refs)
        if scope.city:
            qs = qs.filter(tenant__city__iexact=scope.city)
        return qs

    # -- маппинг и медицинская проверка -----------------------------------

    def _mapping_by_specialist(self, specialist_ids: list[UUID]) -> dict[UUID, "_MappingFacts"]:
        """Статус маппинга, готовность конфигурации и health-check — одним проходом.

        Постоянное число запросов на весь пул, а не по одному на мастера:
        пул на пилоте мал, но форма запроса не должна зависеть от того, мал
        он сегодня или нет.
        """
        links = list(
            SpecialistService.objects
            .filter(
                sellable_offer_q(),
                specialist_id__in=specialist_ids,
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
        # CAT-10: готовность конфигурации ВСЕХ строк пула одним чтением.
        readiness = config_readiness(link.salon_service_id for link in links)
        # CAT-10-ext: юридические условия §7A всех пар мастер × строка — так же.
        legal = legal_gates(
            (link.specialist_id, link.salon_service_id, link.salon_service.template_id) for link in links
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
            # Строка, про которую шов не ответил, закрыта, а не «вне гейта».
            facts.config_ready_by_service[salon.id] = readiness.get(salon.id, False)
            facts.legal_by_service[salon.id] = legal.get((link.specialist_id, salon.id), LegalGate.UNDETERMINED)
            if salon.template_id is not None:
                facts.template_by_service[salon.id] = salon.template_id
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
        goal_positions=None,
        helping_templates: frozenset = frozenset(),
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

        match_level, matched_service_id, matched_category_id, goal_fit_depth = self._match(
            services, needle=needle, goal_categories=goal_categories, mapping=mapping,
            goal_positions=goal_positions, helping_templates=helping_templates,
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
            # CAT-10: готовность ТОЙ строки, чей статус связи стоит выше.
            config_ready=mapping.config_ready(
                has_offer=has_offer, matched_service_ref=matched_service_id,
            ),
            # CAT-10-ext: юридические условия той же строки для этого мастера.
            legal_gate=mapping.legal_gate(
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
            goal_fit_depth=goal_fit_depth,
            # O-1: категории предложений мастера — лист И родитель: услуги
            # висят на листьях, клиент называет корень («массаж»).
            category_refs=_category_refs(services),
            matched_category_refs=_category_refs(
                [s for s in services if s.id == matched_service_id] if matched_service_id else [],
            ),
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
        goal_positions=None,
        helping_templates: frozenset = frozenset(),
    ) -> tuple[MatchLevel, UUID | None, UUID | None, int | None]:
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
                return MatchLevel.SERVICE_EXACT, mapping.best_of(exact), None, None

            partial = [
                s for s in services
                if needle in s.name.casefold()
                or needle in (s.category_name or "").casefold()
                or needle in (s.category_slug or "").casefold()
            ]
            if partial:
                return MatchLevel.SERVICE_PARTIAL, mapping.best_of(partial), None, None

        if goal_categories:
            allowed = set(goal_categories)
            in_goal = [s for s in services if s.category_id in allowed]
            if in_goal:
                # DRF-2789: из услуг мастера в цели — лучшая по статусу связи,
                # затем по готовности конфигурации и юридическим условиям
                # (CAT-10, CAT-10-ext), затем по глубине.
                # Это выбор строки, которой мастер отвечает
                # на цель, а не порядок мастеров: порядок — дело резолвера.
                def depth(s) -> int:
                    return goal_fit_depth(
                        s, positions=goal_positions or {},
                        helping_templates=helping_templates, mapping=mapping,
                    )

                chosen = mapping.best_of_with(in_goal, secondary=depth)
                return MatchLevel.GOAL_CATEGORY, chosen.id, chosen.category_id, depth(chosen)

        return MatchLevel.UNDETERMINED, None, None, None

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


def goal_fit_depth(service, *, positions, helping_templates, mapping) -> int:
    """Насколько глубоко услуга отвечает цели — DRF-2789 (R0 умного ранжирования).

    4 — шаблон услуги подтверждённо помогает этой цели (``CapabilityGoalLink``,
    оба утверждения подтверждены: ``services.capabilities``);
    3 — основная категория цели, связана прямо;
    2 — лист под основным корнем цели (досталась раскрытием);
    1 — побочная категория цели, связана прямо;
    0 — лист под побочным корнем.
    Основная всегда выше побочной; раскрытие различает только внутри класса
    (см. ``goals.resolution.GoalCategoryPosition``).

    Только для услуги, уже совпавшей по цели: допуска это не меняет, это
    глубина внутри уровня ``GOAL_CATEGORY``. Легаси-строка шаблона не имеет —
    у неё глубина только по категории.
    """
    template_id = mapping.template_by_service.get(service.id)
    if template_id is not None and template_id in helping_templates:
        return 4
    position = positions.get(service.category_id)
    if position is None:
        return 0
    return position.rank


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
        #: `SalonService.id` → `template_id` (DRF-2789: подтверждённая связь
        #: процедуры с целью ищется по шаблону услуги).
        self.template_by_service: dict[UUID, UUID] = {}
        self.requires_health_check = False
        self.human_confirmed_ref: str | None = None
        #: `SalonService.id` → статус связи, как он записан в домене.
        self.status_by_service: dict[UUID, str] = {}
        #: `SalonService.id` → готовность конфигурации (CAT-10), как её
        #: отдал :func:`config_readiness`. Легаси-строки здесь нет: канона у
        #: неё не бывает, значит и Body Care она не подлежит.
        self.config_ready_by_service: dict[UUID, bool | None] = {}
        #: `SalonService.id` → юридические условия §7A этой строки для ЭТОГО
        #: мастера (CAT-10-ext), как их отдал :func:`legal_gates`. Легаси-строки
        #: здесь нет: канона у неё не бывает.
        self.legal_by_service: dict[UUID, LegalGate] = {}

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

    def config_ready(
        self,
        *,
        has_offer: bool | None = None,
        matched_service_ref: UUID | None = None,
    ) -> bool | None:
        """Готовность конфигурации той же строки, что отвечает в :meth:`status`.

        Нужда названа и услуга совпала — готовность **именно этой** услуги:
        готовая услуга Б не допускает мастера, совпавшего неготовой А
        (§14.4, как у статуса связи).

        Нужда не названа — предмет сам мастер, и отвечает за него ОДНА
        строка (:meth:`_deciding_row`): готовность и юридические условия
        читаются у неё обе, а не у двух разных строк.
        """
        row = self._answering_row(has_offer=has_offer, matched_service_ref=matched_service_ref)
        return None if row is None else self.config_ready_by_service.get(row)

    def legal_gate(
        self,
        *,
        has_offer: bool | None = None,
        matched_service_ref: UUID | None = None,
    ) -> LegalGate | None:
        """Юридические условия §7A той же строки, что отвечает в :meth:`config_ready`."""
        row = self._answering_row(has_offer=has_offer, matched_service_ref=matched_service_ref)
        return None if row is None else self.legal_by_service.get(row)

    def _answering_row(self, *, has_offer: bool | None, matched_service_ref: UUID | None) -> UUID | None:
        offered = self.has_service if has_offer is None else has_offer
        if not offered:
            return None
        if matched_service_ref is not None:
            return matched_service_ref
        return self._deciding_row()

    def _deciding_row(self) -> UUID | None:
        """Строка, которая отвечает за мастера, когда нужда не названа.

        Смотрятся только строки с лучшим статусом связи (те, что и решают
        допуск). Среди них берётся рекомендуемая — готовая по конфигурации
        И прошедшая юридические условия; одной достаточно. Условия
        проверяются у одной строки: готовая без лицензии и лицензированная
        неготовая вместе мастера не открывают. Если рекомендуемой нет,
        отвечает лучшая по ``_gates_rank`` — и называет причину.
        """
        if not self.status_by_service:
            return None
        best = max(self._RANK[self._as_status(v)] for v in self.status_by_service.values())
        deciding = [
            pk for pk, value in self.status_by_service.items()
            if self._RANK[self._as_status(value)] == best
        ]
        return max(deciding, key=self._gates_rank)

    def _gates_rank(self, pk: UUID) -> tuple[int, int]:
        """(конфигурация не закрыта, юридические условия не закрыты)."""
        return (
            0 if self.config_ready_by_service.get(pk) is False else 1,
            0 if self.legal_by_service.get(pk, LegalGate.CLEARED) is not LegalGate.CLEARED else 1,
        )

    def _row_rank(self, service) -> tuple[int, int, int]:
        """(статус связи, готовность конфигурации, юридические условия) — ключ выбора строки.

        Оба гейта стоят сразу за статусом: закрытая строка выбила бы
        мастера на S1, когда у него есть открытая с тем же совпадением.
        """
        return (
            self._RANK[self._as_status(self.status_by_service.get(service.id))],
            *self._gates_rank(service.id),
        )

    def best_of(self, services) -> UUID | None:
        """Из одинаково совпавших услуг — та, чья связь доказана лучше;
        при равной связи — та, чья конфигурация готова (CAT-10).

        Выбор идёт **только среди совпавших**, поэтому подменой предмета
        не является: человек спросил про эту услугу, и мы отвечаем той
        её строкой, у которой связь подтверждена.

        При равенстве статусов побеждает первая — порядок
        `catalog_services_for` стабилен между репликами, значит и выбор
        воспроизводим.
        """
        if not services:
            return None
        return max(services, key=self._row_rank).id

    def best_of_with(self, services, *, secondary):
        """Как :meth:`best_of`, но при равных связи и готовности — по ``secondary``.

        Сначала статус: допуск к рекомендации требует VERIFIED у той самой
        услуги, которой мастер совпал, и строка с лучшей глубиной, но без
        проверенной связи, выбила бы мастера из выдачи. Готовность — до
        глубины по той же причине (CAT-10). При полном равенстве — первая,
        как у ``best_of``. Возвращает строку, не id.
        """
        return max(
            services,
            key=lambda s: (*self._row_rank(s), secondary(s)),
        )

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
