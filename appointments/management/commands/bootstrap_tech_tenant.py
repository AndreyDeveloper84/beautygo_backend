"""Выделенный ТЕХНИЧЕСКИЙ тенант для разрушающих проверок (§13, OD-PILOT-6).

Зачем
-----
Проверки идемпотентности, устаревшего слота/колбэка, смены цены и
длительности, недоступности провайдера, fail-closed по здоровью и
отката — это **записи**. Делать их на боевом ``formula-tela`` нельзя:
там настоящие мастера, настоящие слоты и напоминания живым людям.
§13 промпта требует отдельный тенант с собственной фикстурой.

Состав по §13
-------------
====================================  ====================================
мастер                                один, ``tech-probe-master``
рабочие часы                          пн–сб 10:00–19:00 (перерыв 14–15),
                                      вс — выходной с ПУСТЫМИ временами
безопасная услуга (прямая бронь)      ``safe-direct``   → health = False
услуга с ОБЯЗАТЕЛЬНЫМ скринингом      ``health-required``→ health = True
услуга с НЕИЗВЕСТНЫМ здоровьем        ``health-unknown`` → health = None
подтверждённая связь (VERIFIED)       у первых двух
неразмеченное предложение (UNMAPPED)  ``unmapped-offer``
слоты                                 занятый / свободный, в манифесте
цена и длительность                   на каждой связке мастер↔услуга
отмена и перенос                      готовые цели + уже отменённая
====================================  ====================================

Почему «неизвестно» и «не размечено» — это две РАЗНЫЕ строки, но одна
из них совмещает оба признака не по лени. ``resolved_requires_health_check``
отдаёт ``None`` ровно в одном случае: шаблона нет и флаг никто не
поднимал. Шаблон есть — ответ всегда настоящий ``bool``. Значит
«здоровье неизвестно» **влечёт** отсутствие шаблона, а отсутствие
шаблона влечёт ``UNMAPPED``: развести эти два признака по разным
строкам модель не позволяет, и делать вид, что позволяет, — врать.
Поэтому ``unmapped-offer`` заведён отдельно и отвечает про здоровье
явное ``False``: проверка про разметку не должна спотыкаться о
здоровье, а проверка про здоровье — о разметку.

Настоящих персональных данных нет
---------------------------------
Имена вымышлены и помечены как фикстура. ``User.phone`` остаётся
``NULL`` — не «правдоподобный номер», а вообще никакого: любой
правдоподобный номер является чьим-то настоящим. Адрес и город тенанта
пустые; пустой город к тому же не даёт салону попасть ни в один
городской ответ поиска.

Тенант отличим от боевого
-------------------------
Слаг ``tech-probe``, имя ``ТЕХСТЕНД — разрушающие проверки (НЕ БОЕВОЙ
САЛОН)``, у мастера и у каждой услуги в названии стоит «Техстенд».
Открывший админку видит это, не читая код.

Три замка
---------
Как у ``seed_demo_salons``: ``Tenant.is_active=False``,
``SpecialistProfile.status=PENDING``, ``is_booking_enabled=False``.
``--activate`` снимает их и ничего не создаёт — включение стенда
остаётся отдельным осознанным действием.

Сухой прогон — поведение по умолчанию
-------------------------------------
Без ``--apply`` команда не пишет ничего: строки создаются в
откатываемой транзакции, чтобы счётчики отчёта были ЗАМЕРОМ, а не
оценкой автора. Это же и защита от случайного запуска не на том стенде.

Идемпотентность: что делает второй запуск
-----------------------------------------
Второй запуск — **сверка, а не пересоздание**, и у двух половин
фикстуры поведение РАЗНОЕ, потому что разное назначение.

* Каркас (тенант, мастер, часы, шаблоны, услуги, связки) ключуется
  прибитыми UUID. Совпало с объявленным — ``unchanged``; разошлось —
  возвращается к объявленному и печатается как ``restored`` с именами
  полей. Это затирание, но объявленное и названное построчно.
* Записи (``Appointment``) — то, что проверка ЛОМАЕТ. Их второй запуск
  **не трогает**: нашёл — ``kept``, не нашёл — ``created``. Молча
  сбросить отменённую проверкой запись значило бы уничтожить её
  доказательство. Вернуть их к объявленному состоянию можно только
  явным ``--reset``, и тогда они печатаются как ``reset``.

Использование::

    manage.py bootstrap_tech_tenant                    # сухой прогон
    manage.py bootstrap_tech_tenant --apply            # записать (выключенным)
    manage.py bootstrap_tech_tenant --apply --reset    # + вернуть записи
    manage.py bootstrap_tech_tenant --activate --apply # снять три замка
    manage.py bootstrap_tech_tenant --apply --output manifest.json

На боевом контуре команда не запускается: это отдельное решение
владельца, а не побочный эффект чьей-то проверки.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from appointments.models import Appointment, SpecialistWorkingHours
from services.models import SalonService, ServiceCategory, ServiceTemplate, SpecialistService
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User

# Боевой пилот. Захардкожен как ЗАПРЕТ: если кто-то перепишет
# TENANT_SLUG на живой салон, команда откажется работать, а не заведёт
# фикстуру поверх настоящих записей.
PROTECTED_SLUGS = frozenset({"formula-tela"})

TENANT_SLUG = "tech-probe"
TENANT_NAME = "ТЕХСТЕНД — разрушающие проверки (НЕ БОЕВОЙ САЛОН)"

MOSCOW = ZoneInfo("Europe/Moscow")

# Прибитые идентификаторы: проверка обязана уметь сослаться на строку,
# не разбирая манифест. Профиль мастера в списке не значится — его
# создаёт post_save-сигнал, и удалять созданную сигналом строку ради
# красивого UUID значит ломать чужой инвариант; id профиля уезжает в
# манифест.
IDS = {
    "tenant": UUID("7ec00000-0000-4000-8000-000000000001"),
    "category": UUID("7ec00000-0000-4000-8000-000000000002"),
    "template_safe": UUID("7ec00000-0000-4000-8000-000000000003"),
    "template_gated": UUID("7ec00000-0000-4000-8000-000000000004"),
    "client": UUID("7ec00000-0000-4000-8000-000000000005"),
    "master_user": UUID("7ec00000-0000-4000-8000-000000000006"),
    "offer_safe": UUID("7ec00000-0000-4000-8000-000000000011"),
    "offer_health_required": UUID("7ec00000-0000-4000-8000-000000000012"),
    "offer_health_unknown": UUID("7ec00000-0000-4000-8000-000000000013"),
    "offer_unmapped": UUID("7ec00000-0000-4000-8000-000000000014"),
    "edge_safe": UUID("7ec00000-0000-4000-8000-000000000021"),
    "edge_health_required": UUID("7ec00000-0000-4000-8000-000000000022"),
    "edge_health_unknown": UUID("7ec00000-0000-4000-8000-000000000023"),
    "edge_unmapped": UUID("7ec00000-0000-4000-8000-000000000024"),
    "appt_cancel_target": UUID("7ec00000-0000-4000-8000-000000000031"),
    "appt_reschedule_target": UUID("7ec00000-0000-4000-8000-000000000032"),
    "appt_occupied": UUID("7ec00000-0000-4000-8000-000000000033"),
    "appt_already_cancelled": UUID("7ec00000-0000-4000-8000-000000000034"),
}

# Момент подтверждения связи прибит, а не берётся из ``now()``: иначе
# каждый повторный прогон объявлял бы строку «изменившейся» и сверка
# перестала бы отличать дрейф от собственного тиканья часов.
MAPPING_CONFIRMED_AT = datetime(2026, 9, 10, 0, 0, tzinfo=dt_timezone.utc)
MAPPING_RULE = "tech_tenant_fixture"
MAPPING_RULE_VERSION = "1"
MAPPING_SOURCE_REF = "Launch Pack 2026-09-10 §13 dedicated technical tenant"

WORKDAY_START = time(10, 0)
WORKDAY_END = time(19, 0)
BREAK_START = time(14, 0)
BREAK_END = time(15, 0)
DAY_OFF = SpecialistWorkingHours.DayOfWeek.SUNDAY


class _Rollback(Exception):
    """Внутренний сигнал отката сухого прогона."""


class _Counts:
    """Счётчики по ИМЕНИ исхода, а не по типу строки.

    Имена — часть контракта команды: отчёт обязан отвечать, что второй
    запуск сделал с каждой строкой, а не только сколько их стало.
    """

    __slots__ = ("created", "unchanged", "restored", "kept", "reset", "changes")

    def __init__(self) -> None:
        self.created = 0
        self.unchanged = 0
        self.restored = 0
        self.kept = 0
        self.reset = 0
        self.changes: list[str] = []

    def bump(self, outcome: str) -> None:
        setattr(self, outcome, getattr(self, outcome) + 1)

    def as_dict(self) -> dict[str, int]:
        return {
            "created": self.created,
            "unchanged": self.unchanged,
            "restored": self.restored,
            "kept": self.kept,
            "reset": self.reset,
        }


def _next_monday_11(now: datetime) -> datetime:
    """Первый понедельник 11:00 MSK строго после ``now``.

    11:00 — внутри объявленных рабочих часов и до перерыва: якорь,
    попадающий в выходной или в перерыв, сделал бы фикстуру
    незаписываемой по причине, к предмету проверки не относящейся.
    """
    local = now.astimezone(MOSCOW)
    candidate = local.replace(hour=11, minute=0, second=0, microsecond=0)
    days_ahead = (0 - candidate.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= local:
        candidate += timedelta(days=7)
    return candidate


class Command(BaseCommand):
    help = "Завести выделенный технический тенант для разрушающих проверок (§13). По умолчанию — сухой прогон."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Записать. Без флага команда ничего не пишет.",
        )
        parser.add_argument(
            "--activate", action="store_true",
            help="Снять три замка (tenant.is_active, profile.status, is_booking_enabled). Ничего не создаёт.",
        )
        parser.add_argument(
            "--reset", action="store_true",
            help="Вернуть записи (Appointment) к объявленному состоянию. Без флага они не трогаются.",
        )
        parser.add_argument(
            "--anchor",
            help="Якорь расписания, ISO-8601 со смещением. По умолчанию — ближайший понедельник 11:00 MSK.",
        )
        parser.add_argument("--output", help="Путь для JSON-манифеста фикстуры.")

    # ------------------------------------------------------------------
    # entrypoint
    # ------------------------------------------------------------------
    def handle(self, *args, **options):
        if TENANT_SLUG in PROTECTED_SLUGS:
            raise CommandError(
                f"Слаг {TENANT_SLUG!r} — боевой салон. Технический тенант не заводится поверх живых записей."
            )
        anchor = self._resolve_anchor(options.get("anchor"))
        self._guard_foreign_tenant()

        if options["activate"]:
            self._run_activate(apply_changes=options["apply"])
            return

        if options["apply"]:
            with transaction.atomic():
                counts, manifest = self._build(anchor, reset=options["reset"])
            self._report(counts, manifest, applied=True)
            self._emit(manifest, options.get("output"))
            return

        box: dict = {}
        try:
            with transaction.atomic():
                box["counts"], box["manifest"] = self._build(anchor, reset=options["reset"])
                raise _Rollback
        except _Rollback:
            pass
        self._report(box["counts"], box["manifest"], applied=False)

    # ------------------------------------------------------------------
    # guards
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_anchor(raw: str | None) -> datetime:
        if not raw:
            from django.utils import timezone as django_timezone
            return _next_monday_11(django_timezone.now())
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise CommandError("--anchor должен быть ISO-8601, например 2026-09-14T11:00:00+03:00") from exc
        if parsed.tzinfo is None:
            raise CommandError("--anchor обязан нести смещение UTC")
        return parsed.astimezone(MOSCOW).replace(second=0, microsecond=0)

    @staticmethod
    def _guard_foreign_tenant() -> None:
        """Слаг занят чужой строкой — отказ, а не захват.

        Совпадение слага при другом id означает, что тенант завёл
        кто-то ещё. Писать в него фикстуру — то же самое, что писать в
        боевой салон: чужие строки, о которых мы ничего не знаем.
        """
        existing = Tenant.all_objects.filter(slug=TENANT_SLUG).first()
        if existing is not None and existing.id != IDS["tenant"]:
            raise CommandError(
                f"Слаг {TENANT_SLUG!r} уже занят тенантом {existing.id} — это не фикстура. Отказ."
            )

    # ------------------------------------------------------------------
    # upsert helper
    # ------------------------------------------------------------------
    @staticmethod
    def _upsert(model, pk: UUID, defaults: dict, counts: _Counts, label: str, *, manager=None):
        """Создать / сверить с объявленным. Возвращает (объект, исход)."""
        mgr = manager or model._default_manager
        obj = mgr.filter(pk=pk).first()
        if obj is None:
            obj = model(pk=pk, **defaults)
            obj.save()
            counts.bump("created")
            return obj, "created"
        drifted = [name for name, value in defaults.items() if getattr(obj, name) != value]
        if drifted:
            for name in drifted:
                setattr(obj, name, defaults[name])
            obj.save()
            counts.bump("restored")
            counts.changes.append(f"{label}: {', '.join(sorted(drifted))}")
            return obj, "restored"
        counts.bump("unchanged")
        return obj, "unchanged"

    # ------------------------------------------------------------------
    # build
    # ------------------------------------------------------------------
    def _build(self, anchor: datetime, *, reset: bool) -> tuple[_Counts, dict]:
        counts = _Counts()

        tenant, _ = self._upsert(
            Tenant, IDS["tenant"],
            {
                "slug": TENANT_SLUG,
                "name": TENANT_NAME,
                # Замок 1. Адрес и город пусты сознательно: выдумывать
                # их незачем, а пустой город к тому же не даёт стенду
                # попасть ни в один городской ответ поиска.
                "address": "",
                "city": "",
                "is_active": False,
            },
            counts, "tenant", manager=Tenant.all_objects,
        )

        category, _ = self._upsert(
            ServiceCategory, IDS["category"],
            {
                "tenant": tenant,
                "name": "Техстенд — разрушающие проверки",
                "slug": "tech-probe",
                "is_active": True,
            },
            counts, "category",
        )

        template_safe, _ = self._upsert(
            ServiceTemplate, IDS["template_safe"],
            {
                "category": category,
                "name": "Техстенд: безопасная услуга",
                "name_short": "Техстенд: безопасная",
                "duration_default": 45,
                "requires_health_check": False,
            },
            counts, "template_safe",
        )
        template_gated, _ = self._upsert(
            ServiceTemplate, IDS["template_gated"],
            {
                "category": category,
                "name": "Техстенд: услуга со скринингом",
                "name_short": "Техстенд: скрининг",
                "duration_default": 60,
                "requires_health_check": True,
            },
            counts, "template_gated",
        )

        client, _ = self._upsert_user(
            IDS["client"], "tech-probe-client", "client", tenant, counts, "client",
        )
        master_user, master_user_outcome = self._upsert_user(
            IDS["master_user"], "tech-probe-master", "specialist", tenant, counts, "master_user",
        )
        master = self._upsert_master_profile(
            master_user, tenant, counts, born_with_the_user=master_user_outcome == "created",
        )

        for user, role in (
            (client, TenantUserRelationship.Role.CUSTOMER),
            (master_user, TenantUserRelationship.Role.STAFF),
        ):
            TenantUserRelationship.objects.update_or_create(
                user=user, tenant=tenant, is_active=True,
                defaults={
                    "role": role,
                    "granted_by": TenantUserRelationship.GrantedBy.SYSTEM,
                    "revoked_at": None,
                },
            )

        offers = self._upsert_offers(tenant, category, template_safe, template_gated, counts)
        edges = self._upsert_edges(tenant, master, offers, counts)
        self._upsert_hours(master, counts)
        appointments = self._upsert_appointments(
            tenant, client, master, offers["safe"], edges["safe"], anchor, counts, reset=reset,
        )

        manifest = self._manifest(anchor, tenant, master, offers, edges, appointments)
        return counts, manifest

    def _upsert_user(self, pk: UUID, username: str, role: str, tenant, counts: _Counts, label: str):
        return self._upsert(
            User, pk,
            {
                "username": username,
                "role": role,
                # НИКАКОГО телефона. Правдоподобный номер — это чей-то
                # настоящий номер; NULL честнее выдумки.
                "phone": None,
                "tenant": tenant,
                "is_active": True,
                "is_verified": False,
                "deleted_at": None,
            },
            counts, label,
        )

    @staticmethod
    def _upsert_master_profile(
        master_user, tenant, counts: _Counts, *, born_with_the_user: bool,
    ) -> SpecialistProfile:
        """Профиль создаёт post_save-сигнал; здесь он только дополняется.

        ``born_with_the_user`` отличает первый прогон от последующих.
        Пустая строка, только что созданная сигналом внутри НАШЕГО
        создания пользователя, — это ``created``; назвать её
        ``restored`` значило бы отчитаться о дрейфе, которого не было.
        """
        profile = SpecialistProfile.objects.get(user=master_user)
        declared = {
            "tenant": tenant,
            "display_name": "Техстенд Мастер (фикстура)",
            "timezone": "Europe/Moscow",
            "is_available": True,
            "booking_source": SpecialistProfile.BookingSource.AYLA_LOCAL,
        }
        drifted = [name for name, value in declared.items() if getattr(profile, name) != value]
        for name in drifted:
            setattr(profile, name, declared[name])
        # Замки 2 и 3. Уже включённого владельцем мастера повторный
        # прогон НЕ выключает — включение стенда было отдельным
        # решением, и отменять его сидом нельзя.
        if profile.status != SpecialistProfile.ProfileStatus.ACTIVE:
            if profile.status != SpecialistProfile.ProfileStatus.PENDING:
                drifted.append("status")
            profile.status = SpecialistProfile.ProfileStatus.PENDING
            if profile.is_booking_enabled:
                drifted.append("is_booking_enabled")
            profile.is_booking_enabled = False
        profile.save()
        if born_with_the_user:
            counts.bump("created")
        elif drifted:
            counts.bump("restored")
            counts.changes.append(f"master_profile: {', '.join(sorted(set(drifted)))}")
        else:
            counts.bump("unchanged")
        return profile

    def _upsert_offers(self, tenant, category, template_safe, template_gated, counts: _Counts) -> dict:
        verified = {
            "mapping_status": SalonService.MappingStatus.VERIFIED,
            "mapping_confirmed_by": None,
            # Подтверждает ПРАВИЛО, а не человек, и это не уловка ради
            # прохождения CHECK-констрейнта. Никакой человек эту связь
            # не смотрел; вписать сюда живого пользователя значило бы
            # изготовить ровно ту фикцию, против которой констрейнт и
            # поставлен.
            "mapping_confirmed_rule": MAPPING_RULE,
            "mapping_rule_version": MAPPING_RULE_VERSION,
            "mapping_confirmed_at": MAPPING_CONFIRMED_AT,
            "mapping_source_ref": MAPPING_SOURCE_REF,
        }
        unmapped = {
            "mapping_status": SalonService.MappingStatus.UNMAPPED,
            "mapping_confirmed_by": None,
            "mapping_confirmed_rule": "",
            "mapping_rule_version": "",
            "mapping_confirmed_at": None,
            "mapping_source_ref": "",
        }
        specs = {
            "safe": (
                IDS["offer_safe"], "Техстенд: безопасная услуга (прямая бронь)", template_safe, 45,
                Decimal("1000.00"),
                # Салон ОТВЕТИЛ «нет». Это сказанное «нет», а не молчание.
                False, verified,
            ),
            "health_required": (
                IDS["offer_health_required"], "Техстенд: услуга с обязательным скринингом", template_gated, 60,
                Decimal("2000.00"),
                # Салон молчит; пол шаблона (True) — канон и не снимается.
                None, verified,
            ),
            "health_unknown": (
                IDS["offer_health_unknown"], "Техстенд: услуга с неизвестным здоровьем", None, 30,
                Decimal("1500.00"),
                # NULL — «салон на вопрос НЕ ОТВЕЧАЛ», а не «ответил нет».
                # Вместе с отсутствием шаблона это единственный путь к
                # resolved_requires_health_check() is None.
                None, unmapped,
            ),
            "unmapped": (
                IDS["offer_unmapped"], "Техстенд: неразмеченное предложение", None, 30,
                Decimal("1200.00"),
                # Здоровье ЗНАЕМ (салон сказал «нет») — чтобы проверка
                # про разметку не спотыкалась о здоровье.
                False, unmapped,
            ),
        }
        offers = {}
        for key, (pk, name, template, duration, price, health, mapping) in specs.items():
            defaults = {
                "tenant": tenant,
                "template": template,
                "category": category,
                "name": name,
                "duration_minutes": duration,
                "base_price": price,
                "requires_health_check": health,
                "is_active": True,
                "source": SalonService.Source.SEED,
            }
            defaults.update(mapping)
            offers[key], _ = self._upsert(SalonService, pk, defaults, counts, f"offer_{key}")
        return offers

    def _upsert_edges(self, tenant, master, offers: dict, counts: _Counts) -> dict:
        specs = {
            "safe": (IDS["edge_safe"], offers["safe"], 45, Decimal("1000.00")),
            "health_required": (IDS["edge_health_required"], offers["health_required"], 60, Decimal("2000.00")),
            "health_unknown": (IDS["edge_health_unknown"], offers["health_unknown"], 30, Decimal("1500.00")),
            "unmapped": (IDS["edge_unmapped"], offers["unmapped"], 30, Decimal("1200.00")),
        }
        edges = {}
        for key, (pk, offer, duration, price) in specs.items():
            edges[key], _ = self._upsert(
                SpecialistService, pk,
                {
                    "tenant": tenant,
                    "salon_service": offer,
                    "specialist": master,
                    "duration_minutes": duration,
                    "price": price,
                    # Мастер НЕ эскалирует: поле двузначное, False здесь —
                    # полный ответ «не поднимаю», а не молчание.
                    "requires_health_check": False,
                    "is_active": True,
                },
                counts, f"edge_{key}",
            )
        return edges

    def _upsert_hours(self, master, counts: _Counts) -> None:
        """Пн–сб рабочие, вс выходной — и у выходного времена ПУСТЫЕ.

        На боевом контуре лежат пять строк «выходной с 09:00 до 21:00»:
        флаг говорит одно, времена другое, и читатель не знает, чему
        верить. Здесь выходной несёт ``None`` во всех четырёх временах.
        """
        for weekday in range(7):
            day_off = weekday == DAY_OFF
            defaults = {
                "specialist": master,
                "day_of_week": weekday,
                "is_working_day": not day_off,
                "start_time": None if day_off else WORKDAY_START,
                "end_time": None if day_off else WORKDAY_END,
                "break_start": None if day_off else BREAK_START,
                "break_end": None if day_off else BREAK_END,
            }
            row = SpecialistWorkingHours.objects.filter(specialist=master, day_of_week=weekday).first()
            if row is None:
                SpecialistWorkingHours.objects.create(**defaults)
                counts.bump("created")
                continue
            drifted = [name for name, value in defaults.items() if getattr(row, name) != value]
            if drifted:
                for name in drifted:
                    setattr(row, name, defaults[name])
                row.save()
                counts.bump("restored")
                counts.changes.append(f"hours[{weekday}]: {', '.join(sorted(drifted))}")
            else:
                counts.bump("unchanged")

    def _upsert_appointments(
        self, tenant, client, master, offer, edge, anchor: datetime, counts: _Counts, *, reset: bool,
    ) -> dict:
        duration = timedelta(minutes=edge.duration_minutes)
        specs = {
            "cancel_target": (IDS["appt_cancel_target"], anchor, Appointment.Status.CONFIRMED, ""),
            "reschedule_target": (
                IDS["appt_reschedule_target"], anchor + timedelta(hours=2), Appointment.Status.CONFIRMED, "",
            ),
            "occupied": (
                IDS["appt_occupied"], anchor + timedelta(days=1), Appointment.Status.CONFIRMED, "",
            ),
            "already_cancelled": (
                IDS["appt_already_cancelled"], anchor - timedelta(days=7), Appointment.Status.CANCELLED,
                "Техстенд: терминальная фикстура",
            ),
        }
        out = {}
        for key, (pk, starts_at, status, reason) in specs.items():
            defaults = {
                "client": client,
                "specialist": master,
                "tenant": tenant,
                "service": None,
                "salon_service": offer,
                "start_datetime": starts_at,
                "end_datetime": starts_at + duration,
                "status": status,
                "version": 1,
                "price": edge.price,
                "snapshot_service_name": offer.name,
                "snapshot_duration_minutes": edge.duration_minutes,
                "snapshot_price": edge.price,
                "snapshot_timezone": "Europe/Moscow",
                "idempotency_key": f"tech-probe-{key}",
                "cancellation_reason": reason,
            }
            existing = Appointment.objects.filter(pk=pk).first()
            if existing is None:
                obj = Appointment(pk=pk, **defaults)
                obj.save()
                counts.bump("created")
                outcome = "created"
            elif reset:
                for name, value in defaults.items():
                    setattr(existing, name, value)
                existing.save()
                obj, outcome = existing, "reset"
                counts.bump("reset")
            else:
                # Проверка ЛОМАЕТ эти строки — в этом их назначение.
                # Молчаливый сброс уничтожил бы её доказательство.
                obj, outcome = existing, "kept"
                counts.bump("kept")
            out[key] = {
                "appointment_id": str(obj.id),
                "status": obj.status,
                "version": obj.version,
                "starts_at": obj.start_datetime.isoformat(),
                "ends_at": obj.end_datetime.isoformat(),
                "outcome": outcome,
            }
        return out

    # ------------------------------------------------------------------
    # activate
    # ------------------------------------------------------------------
    def _run_activate(self, *, apply_changes: bool) -> None:
        tenant = Tenant.all_objects.filter(pk=IDS["tenant"]).first()
        if tenant is None:
            raise CommandError("Технический тенант не заведён. Сначала `bootstrap_tech_tenant --apply`.")
        masters = SpecialistProfile.objects.filter(tenant=tenant)
        plan = {
            "tenant_activated": not tenant.is_active,
            "masters": masters.count(),
        }
        if apply_changes:
            Tenant.all_objects.filter(pk=IDS["tenant"]).update(is_active=True)
            masters.update(
                status=SpecialistProfile.ProfileStatus.ACTIVE,
                is_booking_enabled=True,
                is_available=True,
            )
        head = "ВКЛЮЧЕНО" if apply_changes else "СУХОЙ ПРОГОН включения (ничего не записано)"
        self.stdout.write(f"{head}: замки сняты у тенанта {TENANT_SLUG} и {plan['masters']} мастера(ов).")

    # ------------------------------------------------------------------
    # manifest / report
    # ------------------------------------------------------------------
    @staticmethod
    def _manifest(anchor, tenant, master, offers, edges, appointments) -> dict:
        health = {key: edges[key].resolved_requires_health_check() for key in edges}
        return {
            "schema_version": 1,
            "fixture": "tech-probe",
            "purpose": "destructive probes — §13 / OD-PILOT-6",
            "anchor": anchor.isoformat(),
            "timezone": "Europe/Moscow",
            "tenant_id": str(tenant.id),
            "tenant_slug": tenant.slug,
            "tenant_is_active": tenant.is_active,
            "specialist_id": str(master.id),
            "offers": {key: str(offers[key].id) for key in offers},
            "bookable_edges": {key: str(edges[key].id) for key in edges},
            "resolved_requires_health_check": health,
            "mapping_status": {key: offers[key].mapping_status for key in offers},
            "working_hours": {
                "working_days": "mon-sat",
                "start": WORKDAY_START.isoformat(),
                "end": WORKDAY_END.isoformat(),
                "break": [BREAK_START.isoformat(), BREAK_END.isoformat()],
                "day_off": int(DAY_OFF),
                "day_off_times_are_null": True,
            },
            "appointments": appointments,
            "candidate_slots": {
                "free": (anchor + timedelta(days=2)).isoformat(),
                "occupied": appointments["occupied"]["starts_at"],
            },
        }

    def _report(self, counts: _Counts, manifest: dict, *, applied: bool) -> None:
        head = "ЗАПИСАНО" if applied else "СУХОЙ ПРОГОН (ничего не записано)"
        self.stdout.write(f"{head} — технический тенант {TENANT_SLUG} ({TENANT_NAME})")
        c = counts.as_dict()
        self.stdout.write(
            "  строки: created={created} unchanged={unchanged} restored={restored} "
            "kept={kept} reset={reset}".format(**c)
        )
        for line in counts.changes:
            self.stdout.write(f"  restored ← {line}")
        health = manifest["resolved_requires_health_check"]
        self.stdout.write(
            "  здоровье по связкам: safe={safe} health_required={health_required} "
            "health_unknown={health_unknown} unmapped={unmapped}".format(**health)
        )
        if not applied:
            self.stdout.write("  запустите с --apply, чтобы записать.")

    def _emit(self, manifest: dict, output: str | None) -> None:
        payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
        if output:
            Path(output).write_text(payload + "\n", encoding="utf-8")
        self.stdout.write(payload)
