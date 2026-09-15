"""Готовность салона после заведения оператором — проекция фактов, только чтение (DRF-1977).

Раздел Q решений владельца (docs/OWNER_QUESTIONS_2026-09-12.md): после 13 шагов
operator-assisted onboarding оператор проверяет салон одной командой. Это **не
сущность и не поле**: в базе не появляется ``ready=true``, команда ничего не
пишет и флага записи у неё нет. Каждый запуск заново читает факты каталога.

Что печатает
------------

Строку на каждый факт, ``ключ=значение``, затем итог из закрытого словаря:

* ``RESULT: READY`` — клиент может найти услугу, увидеть мастера, получить слот
  и записаться;
* ``RESULT: READY_WITHOUT_DISTANCE`` — записаться можно, расстояние и «рядом»
  не считаются (место подтверждено без координат — DaData салон не блокирует);
* ``RESULT: NOT_READY`` — записаться нельзя.

``reasons:`` — причины итога; ``info:`` — сведения, которые итог не меняют.

Чего команда не видит — и печатает это строкой
----------------------------------------------

* привязку салона к боту (``bot=not_checked_from_catalog``) — она живёт в боте;
* чтение расписания движком записи (``engine_read=not_checked_from_catalog``) —
  здесь только строки расписания каталога.

Определения — те же, что у пути продажи
---------------------------------------

* мастер продаётся — ``users.sellable.sellable_q`` ∧ активный аккаунт;
* ребро продаётся — ``services.offer_sellable.sellable_offer_q`` (DRF-1962) у
  продаваемого мастера, длительность определяется, признак проверки здоровья
  известен (``resolved_requires_health_check() is not None``); ребро с неизвестным
  признаком запись отклонит ``HEALTH_CHECK_UNKNOWN`` — оно считается отдельно;
* место участвует в расстоянии — ``tenants.distance.participating_place_q``;
* рабочий день — то же условие, что у ворот публикации
  (``users/publication.py``: ``is_working_day`` ∧ начало ∧ конец);
* администратор — то же условие, что у ``IsTenantAdmin`` (активная связь с ролью
  ``admin``).

``--require-ready`` превращает ``NOT_READY`` в ненулевой код выхода (для
скриптов); ``READY_WITHOUT_DISTANCE`` — готов. Соло-workspace отклоняется: его
готовность — ворота публикации мастера, а не эта проекция.
"""
from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from appointments.models import SpecialistWorkingHours
from core.measurement_subject import gather_pulse, subject_lines
from services.geocoding import reverse_geocoding_readiness
from services.models import SalonService, SpecialistService
from services.offer_sellable import sellable_offer_q
from tenants.distance import participating_place_q
from tenants.models import LocationStatus, ServiceLocation, Tenant
from users.models import SpecialistProfile, TenantUserRelationship
from users.sellable import catalog_pool_q, sellable_q

READY = "READY"
READY_WITHOUT_DISTANCE = "READY_WITHOUT_DISTANCE"
NOT_READY = "NOT_READY"

#: Причины ``NOT_READY`` — в порядке шагов раздела Q.
NOT_READY_REASONS = (
    "tenant_inactive",
    "city_missing",
    "no_confirmed_location",
    "no_bookable_masters",
    "no_schedule",
    "no_active_services",
    "no_verified_services",
    "no_bookable_edges",
    "no_admin",
)
#: Причины ``READY_WITHOUT_DISTANCE``.
DISTANCE_REASONS = ("no_coordinates", "masters_without_place")
#: Сведения, которые итог не меняют.
INFO = (
    "bot_not_checked",
    "specialists_pending",
    "health_check_unknown_edges",
    "verified_partial",
    "geocoder_not_configured",
)


def _flag(value: bool) -> str:
    return "true" if value else "false"


class Command(BaseCommand):
    help = "Готовность салона: проекция фактов каталога и итог READY / READY_WITHOUT_DISTANCE / NOT_READY. Ничего не пишет."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--tenant", required=True, help="Slug салона.")
        parser.add_argument(
            "--require-ready", action="store_true",
            help="NOT_READY → ненулевой код выхода. READY_WITHOUT_DISTANCE считается готовым.",
        )

    def handle(self, *args, **options) -> None:
        slug = options["tenant"]
        tenant = Tenant.all_objects.filter(slug=slug).first()
        if tenant is None:
            raise CommandError(f"салона {slug!r} нет")
        if tenant.kind == Tenant.Kind.SOLO:
            raise CommandError(
                f"{slug!r} — solo workspace: его готовность — ворота публикации мастера, не эта команда"
            )

        for line in subject_lines(pulses=gather_pulse()):
            self.stdout.write(line)
        self.stdout.write("")
        self.stdout.write(f"== ГОТОВНОСТЬ САЛОНА: {slug} · только чтение, в базу не пишет ==")

        reasons: list[str] = []
        info: list[str] = ["bot_not_checked"]

        # 1–2. Салон и город.
        city_set = bool((tenant.city or "").strip())
        self._line("салон", f"is_active={_flag(tenant.is_active)} kind={tenant.kind}")
        self._line("город", f"city_set={_flag(city_set)}")
        if not tenant.is_active:
            reasons.append("tenant_inactive")
        if not city_set:
            reasons.append("city_missing")

        # 3–4. Места и координаты.
        places = ServiceLocation.objects.filter(tenant=tenant)
        by_status = Counter(places.values_list("status", flat=True))
        confirmed = by_status.get(LocationStatus.CONFIRMED, 0)
        participating = places.filter(participating_place_q(prefix="")).count()
        self._line(
            "места",
            f"confirmed={confirmed} review_required={by_status.get(LocationStatus.REVIEW_REQUIRED, 0)} "
            f"inactive={by_status.get(LocationStatus.INACTIVE, 0)}",
        )
        if confirmed == 0:
            reasons.append("no_confirmed_location")

        # 5. Мастера.
        profiles = SpecialistProfile.objects.filter(tenant=tenant)
        sellable_masters = profiles.filter(sellable_q(), user__is_active=True)
        sellable_count = sellable_masters.count()
        placed_count = sellable_masters.filter(participating_place_q()).count()
        statuses = Counter(profiles.values_list("status", flat=True))
        draft = statuses.get(SpecialistProfile.ProfileStatus.DRAFT, 0)
        pending = statuses.get(SpecialistProfile.ProfileStatus.PENDING, 0)
        geocoder_refusal = reverse_geocoding_readiness()
        self._line(
            "расстояние",
            f"participating_places={participating} masters_with_participating_place={placed_count} "
            f"geocoder={'ready' if geocoder_refusal is None else 'not_configured'}",
        )
        self._line(
            "мастера",
            f"sellable={sellable_count} pool={profiles.filter(catalog_pool_q(), user__is_active=True).count()} "
            f"draft={draft} pending={pending}",
        )
        if sellable_count == 0:
            reasons.append("no_bookable_masters")
        if confirmed and participating == 0:
            reasons.append("no_coordinates")
        elif participating and placed_count < sellable_count:
            reasons.append("masters_without_place")
        if geocoder_refusal is not None:
            info.append("geocoder_not_configured")
        if draft or pending:
            info.append("specialists_pending")

        # 6. Расписание — строки каталога; чтение движком отсюда не проверяется.
        with_working_day = (
            SpecialistWorkingHours.objects.filter(
                specialist__in=sellable_masters,
                is_working_day=True,
                start_time__isnull=False,
                end_time__isnull=False,
            )
            .values("specialist")
            .distinct()
            .count()
        )
        self._line(
            "расписание",
            f"source=catalog with_working_day={with_working_day} of_sellable={sellable_count} "
            "engine_read=not_checked_from_catalog",
        )
        if sellable_count and with_working_day == 0:
            reasons.append("no_schedule")

        # 7–8. Услуги салона и VERIFIED.
        services = SalonService.objects.filter(tenant=tenant)
        active = services.filter(is_active=True)
        active_count = active.count()
        mapping = Counter(active.values_list("mapping_status", flat=True))
        verified = mapping.get("verified", 0)
        self._line(
            "услуги",
            f"active_services={active_count} total_services={services.count()} "
            f"with_template={active.filter(template__isnull=False).count()}",
        )
        self._line(
            "VERIFIED",
            f"verified={verified} of={active_count} review_required={mapping.get('review_required', 0)} "
            f"not_recommendable={mapping.get('not_recommendable', 0)}",
        )
        if active_count == 0:
            reasons.append("no_active_services")
        if verified == 0:
            reasons.append("no_verified_services")
        elif verified < active_count:
            info.append("verified_partial")

        # 9. Рёбра, на которые можно записаться.
        edges = (
            SpecialistService.objects.filter(salon_service__tenant=tenant)
            .filter(sellable_offer_q())
            .filter(sellable_q("specialist"), specialist__user__is_active=True)
            .select_related("salon_service__template", "specialist")
        )
        bookable = health_unknown = 0
        for edge in edges:
            if edge.resolved_duration() is None:
                continue
            if edge.resolved_requires_health_check() is None:
                health_unknown += 1
            else:
                bookable += 1
        self._line("рёбра", f"bookable_edges={bookable} health_unknown_edges={health_unknown}")
        if bookable == 0:
            reasons.append("no_bookable_edges")
        if health_unknown:
            info.append("health_check_unknown_edges")

        # 10. Администратор.
        admins = TenantUserRelationship.objects.filter(
            tenant=tenant, role=TenantUserRelationship.Role.ADMIN, is_active=True,
        ).count()
        self._line("администратор", f"active_admins={admins}")
        if admins == 0:
            reasons.append("no_admin")

        # 11. Бот — не из каталога.
        self._line("бот", "bot=not_checked_from_catalog")

        if any(r in NOT_READY_REASONS for r in reasons):
            result = NOT_READY
        elif reasons:
            result = READY_WITHOUT_DISTANCE
        else:
            result = READY
        self.stdout.write("")
        self.stdout.write(f"RESULT: {result}")
        self.stdout.write(f"reasons: {','.join(reasons)}")
        self.stdout.write(f"info: {','.join(info)}")

        if options["require_ready"] and result == NOT_READY:
            raise CommandError(f"RESULT: NOT_READY ({','.join(r for r in reasons if r in NOT_READY_REASONS)})")

    def _line(self, label: str, facts: str) -> None:
        self.stdout.write(f"  {label:<14}: {facts}")
