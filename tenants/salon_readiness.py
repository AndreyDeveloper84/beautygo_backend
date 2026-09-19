"""Салонная готовность поимённо — агрегат по мастерам (DRF-2117, §50 п.4).

Владелец салона жмёт «Проверить готовность» и получает не «всё хорошо», а
список: «Анна — не настроен график; Иван — не назначены услуги». Здесь —
половина ответа, которую знает КАТАЛОГ; вторую половину (подтверждение
графика §83 ``schedule_confirmed_at``, ``catalog_specialist_id``,
``SoloIdentityLink``, ``MasterService.sellable``) знает только бот — эти
столбцы живут в его зеркале, и бот накладывает их на этот ответ сам.
Поэтому этот модуль не обещает «салон готов» в одиночку: ``ready`` здесь —
«каталог не видит препятствий», и бот вправе сузить.

Пять проверок на мастера, каждая ``ok`` / ``problem`` / ``unknown`` /
``skipped``:

==============  =======================  ==========================================
проверка        код проблемы             когда
==============  =======================  ==========================================
publication     not_published            не опубликован (``users.sellable.is_published``)
                hidden_from_catalog      ACTIVE, но ``is_available=False``
                booking_paused           ACTIVE, но ``is_booking_enabled=False``
schedule        schedule_missing         нет рабочего дня с началом и концом
                                         (тот же предикат, что ``no_working_day``
                                         в ``users.publication``)
services        services_missing         ни одного продаваемого ребра
                                         (``services.offer_sellable``)
catalog_link    identity_not_linked      MAX-личность не привязана к аккаунту
                                         мастера (``users.publication.is_linked``)
slots           no_free_slots            ни одного окна на ближайшие
                                         :data:`HORIZON_DAYS` дней
                slots_unknown            вычислитель упал — проблема, не «ок»
services        service_duration_missing у первого продаваемого ребра нет
                                         длительности ни на услуге, ни на ребре,
                                         ни на шаблоне — путь записи такую откажет
салон           no_masters               ни одного мастера — ``ready`` ложь
==============  =======================  ==========================================

**UNKNOWN = проблема.** Отказ вычислителя не превращается в «всё хорошо»:
он попадает в список с текстом «не удалось проверить …», а класс
исключения — в лог (поправка главного окна 20.09).

**Слоты — есть/нет по одной услуге и с названным пределом.** Источник —
``AvailabilityQueryService`` (единственный внутри каталога; DRF-1637 —
контракт доступности между каталогом и ботом ещё не написан), длительность и
буфер — первого продаваемого ребра мастера, как их резолвит путь записи
(``services.service_resolver``). Считается только когда график и услуги на
месте: без них окон нет по построению, и второе слово о том же было бы шумом
(``skipped``). Пределы перечислены в :data:`LIMITS` и уезжают в ответ.

**Про §83 каталог молчит.** «Не настроен график» здесь — нет рабочего дня;
подтверждён ли он владельцем (``MASTER_SCHEDULE_CONFIRMATION_REQUIRED``) —
факт бота, и список у бота длиннее при включённом гейте. Названо в контракте.

Имена — без склонений: «Анна — не настроен график», а не «у Анны». Морфологии
в каталоге нет (как и в боте — ``marketplace/discovery.py``), и форма
«имя — причина» верна для любого имени. Названо пределом.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from django.utils import timezone as dj_timezone

from appointments.models import SpecialistWorkingHours
from services.models import SpecialistService
from services.offer_sellable import sellable_offer_q
from tenants.models import Tenant
from users.models import SpecialistProfile
from users.publication import is_linked
from users.sellable import is_published

logger = logging.getLogger(__name__)

#: Сколько дней вперёд ищутся свободные окна.
HORIZON_DAYS = 7

CHECK_PUBLICATION = "publication"
CHECK_SCHEDULE = "schedule"
CHECK_SERVICES = "services"
CHECK_CATALOG_LINK = "catalog_link"
CHECK_SLOTS = "slots"

CHECKS = (CHECK_PUBLICATION, CHECK_SCHEDULE, CHECK_SERVICES, CHECK_CATALOG_LINK, CHECK_SLOTS)

OK = "ok"
PROBLEM = "problem"
UNKNOWN = "unknown"
SKIPPED = "skipped"

NOT_PUBLISHED = "not_published"
HIDDEN_FROM_CATALOG = "hidden_from_catalog"
BOOKING_PAUSED = "booking_paused"
SCHEDULE_MISSING = "schedule_missing"
SERVICES_MISSING = "services_missing"
IDENTITY_NOT_LINKED = "identity_not_linked"
NO_FREE_SLOTS = "no_free_slots"
SLOTS_UNKNOWN = "slots_unknown"
SERVICE_DURATION_MISSING = "service_duration_missing"
#: Уровень салона: ни одного мастера — «готов» здесь был бы ложью.
NO_MASTERS = "no_masters"

#: Текст причины по коду — «{name} — …». Бот держит свою таблицу по тем же
#: кодам и падает на этот текст только для незнакомого кода.
TEXTS: dict[str, str] = {
    NOT_PUBLISHED: "{name} — профиль не опубликован",
    HIDDEN_FROM_CATALOG: "{name} — скрыт из каталога",
    BOOKING_PAUSED: "{name} — приём записей на паузе",
    SCHEDULE_MISSING: "{name} — не настроен график",
    SERVICES_MISSING: "{name} — не назначены услуги",
    IDENTITY_NOT_LINKED: "{name} — не привязана личность MAX",
    NO_FREE_SLOTS: "{name} — нет свободных окон на ближайшие {days} дней",
    SLOTS_UNKNOWN: "{name} — не удалось проверить свободные окна",
    SERVICE_DURATION_MISSING: "{name} — у услуги не задана длительность",
    NO_MASTERS: "В салоне нет ни одного мастера",
}

#: Какая проверка выдаёт какой код — для ``checks`` в ответе.
CODE_CHECK: dict[str, str] = {
    NOT_PUBLISHED: CHECK_PUBLICATION,
    HIDDEN_FROM_CATALOG: CHECK_PUBLICATION,
    BOOKING_PAUSED: CHECK_PUBLICATION,
    SCHEDULE_MISSING: CHECK_SCHEDULE,
    SERVICES_MISSING: CHECK_SERVICES,
    IDENTITY_NOT_LINKED: CHECK_CATALOG_LINK,
    NO_FREE_SLOTS: CHECK_SLOTS,
    SLOTS_UNKNOWN: CHECK_SLOTS,
    SERVICE_DURATION_MISSING: CHECK_SERVICES,
}

#: Названные пределы — уезжают в ответ как есть.
LIMITS: tuple[str, ...] = (
    "slots: только «есть/нет», по длительности первой продаваемой услуги мастера, "
    f"{HORIZON_DAYS} дней от сегодня в поясе мастера; контракт доступности — DRF-1637",
    "slots: не считаются, пока не настроены график и услуги (skipped)",
    "schedule: «не настроен» = нет рабочего дня в каталоге; подтверждение владельцем (§83) "
    "знает только бот и добавляет при включённом MASTER_SCHEDULE_CONFIRMATION_REQUIRED",
    "catalog_link: здесь — привязка MAX-личности к аккаунту мастера; "
    "связь строки зеркала с каталогом (catalog_specialist_id) знает только бот",
    "имена без склонений: «Анна — не настроен график»",
    "стоимость: до 7 вычислений окон на мастера за вызов — кнопка, не цикл",
    "slots: кэш окон 60 с (SlotCacheService) — правка графика видна не сразу",
)


@dataclass(frozen=True)
class Problem:
    #: ``None`` — проблема уровня салона (``no_masters``), не мастера.
    master_id: str | None
    master_name: str
    code: str
    text: str

    def as_dict(self) -> dict[str, Any]:
        master = (
            None if self.master_id is None
            else {"id": self.master_id, "name": self.master_name}
        )
        return {"master": master, "code": self.code, "text": self.text}


@dataclass(frozen=True)
class MasterReadiness:
    id: str
    user_id: str
    name: str
    checks: dict[str, str]
    problems: tuple[Problem, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "checks": dict(self.checks),
            "problems": [{"code": p.code, "text": p.text} for p in self.problems],
        }


@dataclass(frozen=True)
class SalonReadiness:
    tenant: Tenant
    checked_at: datetime
    masters: tuple[MasterReadiness, ...] = ()
    limits: tuple[str, ...] = field(default_factory=lambda: LIMITS)

    @property
    def problems(self) -> tuple[Problem, ...]:
        if not self.masters:
            return (Problem(None, "", NO_MASTERS, TEXTS[NO_MASTERS]),)
        return tuple(p for m in self.masters for p in m.problems)

    @property
    def unknown(self) -> bool:
        return any(UNKNOWN in m.checks.values() for m in self.masters)

    @property
    def ready(self) -> bool:
        """«Готов» — только при пустом списке и без единого ``unknown``."""
        return not self.problems and not self.unknown

    def as_dict(self) -> dict[str, Any]:
        return {
            "salon": {"slug": self.tenant.slug, "name": self.tenant.name},
            "ready": self.ready,
            "checked_at": self.checked_at.isoformat(),
            "horizon_days": HORIZON_DAYS,
            "masters": [m.as_dict() for m in self.masters],
            "problems": [p.as_dict() for p in self.problems],
            "limits": list(self.limits),
        }


def _text(code: str, name: str) -> str:
    return TEXTS[code].format(name=name, days=HORIZON_DAYS)


def _display_name(profile: SpecialistProfile) -> str:
    name = (profile.display_name or "").strip()
    if name:
        return name.split()[0]
    return "мастер"


def active_masters(tenant: Tenant):
    """Мастера салона, о которых есть смысл спрашивать: живой аккаунт, не удалён."""
    return (
        SpecialistProfile.objects
        .filter(tenant=tenant, user__is_active=True, user__deleted_at__isnull=True)
        .select_related("user")
        .order_by("display_name", "id")
    )


def _publication_code(profile: SpecialistProfile) -> str | None:
    # ``users.sellable`` — единственное написание «опубликован» (перепись DRF-1845).
    if not is_published(profile):
        return NOT_PUBLISHED
    if not profile.is_available:
        return HIDDEN_FROM_CATALOG
    if not profile.is_booking_enabled:
        return BOOKING_PAUSED
    return None


def _has_working_day(profile: SpecialistProfile) -> bool:
    return SpecialistWorkingHours.objects.filter(
        specialist=profile,
        is_working_day=True,
        start_time__isnull=False,
        end_time__isnull=False,
    ).exists()


def _first_sellable_edge(profile: SpecialistProfile) -> SpecialistService | None:
    return (
        SpecialistService.objects
        # Тот же предикат, что у пути записи (``services.service_resolver``):
        # ребро продаётся И его услуга — этого тенанта.
        .filter(sellable_offer_q(), specialist=profile, salon_service__tenant_id=profile.tenant_id)
        .select_related("salon_service")
        .order_by("created_at", "id")
        .first()
    )


def _edge_duration(edge: SpecialistService) -> int | None:
    """Длительность, как её резолвит путь записи: услуга салона, иначе каскад ребра."""
    salon = edge.salon_service
    if salon.duration_minutes is not None:
        return salon.duration_minutes
    return edge.resolved_duration()


def _has_free_slot(
    profile: SpecialistProfile, edge: SpecialistService, duration: int, *, today: date,
) -> bool:
    """Есть ли хоть одно окно на ``HORIZON_DAYS`` дней — тем же вычислителем,
    что и путь записи (``users.specialists_api.compute_specialist_day_slots``)."""
    from appointments.application.dto import GetAvailabilityDTO
    from appointments.application.services.availability_query_service import (
        AvailabilityQueryService,
    )
    from appointments.domain.booking_window import booking_horizon_end

    salon = edge.salon_service
    horizon_end = booking_horizon_end()
    service = AvailabilityQueryService()
    for offset in range(HORIZON_DAYS):
        day = today + timedelta(days=offset)
        result = service.get_day_availability(
            GetAvailabilityDTO(specialist_id=profile.pk, target_date=day, service_id=salon.pk),
            duration_override=duration,
            buffer_override=edge.buffer_after_minutes,
        )
        if not result.is_working_day:
            continue
        if any(slot.start_at <= horizon_end for slot in result.slots):
            return True
    return False


def master_readiness(profile: SpecialistProfile, *, today: date | None = None) -> MasterReadiness:
    name = _display_name(profile)
    checks: dict[str, str] = {}
    problems: list[Problem] = []

    def problem(code: str) -> None:
        checks[CODE_CHECK[code]] = PROBLEM
        problems.append(Problem(str(profile.pk), name, code, _text(code, name)))

    publication = _publication_code(profile)
    if publication is None:
        checks[CHECK_PUBLICATION] = OK
    else:
        problem(publication)

    schedule_ok = _has_working_day(profile)
    if schedule_ok:
        checks[CHECK_SCHEDULE] = OK
    else:
        problem(SCHEDULE_MISSING)

    edge = _first_sellable_edge(profile)
    duration = _edge_duration(edge) if edge is not None else None
    if edge is None:
        problem(SERVICES_MISSING)
    elif duration is None:
        # Вычислитель без длительности ушёл бы в маркетплейс-ветку и упал
        # ``DoesNotExist`` — «не удалось проверить» вместо починяемого факта.
        problem(SERVICE_DURATION_MISSING)
    else:
        checks[CHECK_SERVICES] = OK

    if is_linked(profile):
        checks[CHECK_CATALOG_LINK] = OK
    else:
        problem(IDENTITY_NOT_LINKED)

    if not schedule_ok or edge is None or duration is None:
        checks[CHECK_SLOTS] = SKIPPED
    else:
        if today is None:
            try:
                today = datetime.now(tz=ZoneInfo(profile.timezone)).date()
            except Exception:  # noqa: BLE001 — незнакомый пояс не должен ронять проверку
                today = datetime.now(tz=timezone.utc).date()
        try:
            has_slot = _has_free_slot(profile, edge, duration, today=today)
        except Exception as exc:  # noqa: BLE001 — UNKNOWN = проблема, класс — в лог
            logger.warning(
                "salon_readiness.slots_unknown specialist=%s tenant=%s exc=%s",
                profile.pk, profile.tenant_id, type(exc).__name__, exc_info=True,
            )
            checks[CHECK_SLOTS] = UNKNOWN
            problems.append(
                Problem(str(profile.pk), name, SLOTS_UNKNOWN, _text(SLOTS_UNKNOWN, name))
            )
        else:
            if has_slot:
                checks[CHECK_SLOTS] = OK
            else:
                problem(NO_FREE_SLOTS)

    return MasterReadiness(
        id=str(profile.pk),
        user_id=str(profile.user_id),
        name=name,
        checks=checks,
        problems=tuple(problems),
    )


def salon_readiness(tenant: Tenant, *, today: date | None = None) -> SalonReadiness:
    masters = tuple(master_readiness(p, today=today) for p in active_masters(tenant))
    return SalonReadiness(tenant=tenant, checked_at=dj_timezone.now(), masters=masters)


__all__ = [
    "BOOKING_PAUSED",
    "CHECKS",
    "CODE_CHECK",
    "HIDDEN_FROM_CATALOG",
    "HORIZON_DAYS",
    "IDENTITY_NOT_LINKED",
    "LIMITS",
    "NOT_PUBLISHED",
    "NO_FREE_SLOTS",
    "NO_MASTERS",
    "OK",
    "PROBLEM",
    "SCHEDULE_MISSING",
    "SERVICES_MISSING",
    "SERVICE_DURATION_MISSING",
    "SKIPPED",
    "SLOTS_UNKNOWN",
    "TEXTS",
    "UNKNOWN",
    "MasterReadiness",
    "Problem",
    "SalonReadiness",
    "active_masters",
    "master_readiness",
    "salon_readiness",
]
