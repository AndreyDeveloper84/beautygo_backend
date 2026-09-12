"""Посев стенда для cross-boundary golden бота (DRF-1702, план способности 41).

Бот гоняет golden-сценарии против **этого** каталога как отдельного процесса
(`ai-bot-platform/tests/cross_boundary/`). Клиента сеять не нужно — каталог
создаёт proxy-пользователя на первом запросе. Сеять нужно **мастеров**: их
состояние и есть предмет сценария.

### Сценарий ``p7`` — мастера есть, VERIFIED нет

Состояние пилота на 09.09: 34 мастера, `verified = 0`. Полка рекомендаций
обязана ответить «каталог видно, рекомендовать нечего» —
``ELIG_EXCLUDED_NOT_RECOMMENDABLE`` на слое 2 — а не выдумать кандидата и не
промолчать. Пустой каталог даёт **другое** пусто (``QUALITY_NO_EVIDENCE``,
свидетельств нет вовсе), поэтому пустой базой P7 пилота не воспроизвести:
нужны мастера, у которых связь услуги с каноном есть, но не подтверждена.

```
p7:  tenant golden-p7 · категория · 3 мастера ACTIVE / available / booking
     · у каждого SpecialistService на SalonService с REVIEW_REQUIRED
```

``--verify-one`` подтверждает связь у ОДНОГО мастера. Это положительная
стража golden'а на стороне бота: после неё P7 обязан **покраснеть** — на
полке появляется кандидат. Тест, который не умеет краснеть, ничего не
доказывает; этим флагом его учат.

### Сценарий ``p1`` — прямая услуга → бронь

Один мастер с **подтверждённой** связью, одна услуга, расписание на все
семь дней — чтобы слот нашёлся в любую дату. Клиент не сеется (proxy).

Два числа длительности посеяны **намеренно разными**:

```
SalonService.duration_minutes      = 45   ← бронь берёт ЭТО (service_resolver, AMD-019)
SpecialistService.duration_minutes = 60   ← бот ПОКАЗЫВАЕТ это (specialist-services)
SpecialistService.price            = 1500 ← и бронь, и показ — из ребра
```

Матрица готовности зовёт P1 PARTIAL с формулировкой «цена/длительность
подменяются молча». Golden не чинит это — он делает видимым, **какое**
число и **из какого слоя** ушло в бронь. Одинаковые значения спрятали бы
расхождение источников за совпадением чисел; разные — называют источник
по самому числу. Tenant создаётся с фиксированным UUID: бот зовёт каталог
услуг с ``?tenant=<id>``, и его собственный tenant обязан носить тот же.

### Почему отказывается писать не в стендовую базу

Команда создаёт выдуманных мастеров. На пилоте это было бы загрязнением
каталога живыми на вид строками. Поэтому она пишет только в базу, имя
которой содержит ``golden``, — или с явным ``--allow-any-db``, и тогда
ответственность у того, кто его набрал.

### Идемпотентность

Повторный запуск не плодит мастеров: всё через ``get_or_create`` по
фиксированным именам. Печатает, что создано, а что уже было, — числами
рядом с именами.

Usage:
    python manage.py seed_golden --scenario p7
    python manage.py seed_golden --scenario p7 --verify-one
    python manage.py seed_golden --scenario p1
"""

from __future__ import annotations

import uuid
from datetime import time
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from appointments.models import SpecialistWorkingHours
from services.models import SalonService, ServiceCategory, ServiceTemplate, SpecialistService
from tenants.models import Tenant
from users.models import SpecialistProfile, User

TENANT_SLUG = "golden-p7"
CATEGORY_SLUG = "golden-manicure"
MASTER_COUNT = 3

#: P1. UUID фиксирован: бот сеет свой Tenant с тем же id, иначе его вызов
#: ``catalog/salon-services/?tenant=<id>`` увидит пустой каталог.
P1_TENANT_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
P1_TENANT_SLUG = "golden-p1"
P1_CATEGORY_SLUG = "golden-manicure-p1"
P1_SERVICE_NAME = "Golden manicure P1"
P1_SALON_DURATION_MIN = 45
P1_EDGE_DURATION_MIN = 60
P1_EDGE_PRICE = Decimal("1500.00")


def _db_is_a_stand() -> bool:
    """Стенд — база с «golden» в имени либо тестовая база pytest (``test_*``).

    Второе — не лазейка: ``test_*`` создаёт и уничтожает pytest-django, на
    пилоте такой базы не бывает. Без этого команду нельзя было бы проверить
    тестом, не выключая её собственный сторож флагом.
    """
    name = str(settings.DATABASES["default"].get("NAME", "")).lower()
    return "golden" in name or name.startswith("test_")


class Command(BaseCommand):
    help = "Посеять каталог под cross-boundary golden бота. Пишет только в базу с «golden» в имени."

    def add_arguments(self, parser):
        parser.add_argument("--scenario", choices=["p7", "p1"], required=True)
        parser.add_argument(
            "--verify-one",
            action="store_true",
            help="Подтвердить связь у одного мастера — golden P7 бота обязан покраснеть.",
        )
        parser.add_argument(
            "--allow-any-db",
            action="store_true",
            help="Разрешить запись в базу без «golden» в имени. На пилоте — НЕТ.",
        )

    def handle(self, *args, **options):
        if not _db_is_a_stand() and not options["allow_any_db"]:
            raise CommandError(
                "seed_golden пишет выдуманных мастеров и отказывается делать это в базе "
                f"{settings.DATABASES['default'].get('NAME')!r}: в имени нет «golden». "
                "Это стенд? Назовите базу так. Это не стенд? Тогда не сейте."
            )

        if options["scenario"] == "p7":
            self._seed_p7(verify_one=options["verify_one"])
        elif options["scenario"] == "p1":
            self._seed_p1()

    @transaction.atomic
    def _seed_p7(self, *, verify_one: bool) -> None:
        tenant, t_new = Tenant.objects.get_or_create(
            slug=TENANT_SLUG, defaults={"name": "Golden P7 Salon"}
        )
        category, c_new = ServiceCategory.objects.get_or_create(
            slug=CATEGORY_SLUG, defaults={"name": "Маникюр (golden)"}
        )
        created = 0
        for i in range(1, MASTER_COUNT + 1):
            username = f"golden_p7_master_{i}"
            user = User.objects.filter(username=username).first()
            if user is None:
                user = User.objects.create_user(
                    username=username, password=None, role="specialist", phone=f"+7999000010{i}"
                )
                created += 1
            profile = SpecialistProfile.objects.get(user=user)
            profile.display_name = f"Golden Master {i}"
            profile.tenant = tenant
            profile.status = SpecialistProfile.ProfileStatus.ACTIVE
            profile.is_available = True
            profile.is_booking_enabled = True
            profile.timezone = "Europe/Moscow"
            profile.save()

            # «Связь ЕСТЬ, но не подтверждена» — значит у строки есть
            # шаблон (DRF-1668: VERIFIED ⇒ template, и --verify-one ниже
            # переводит одну из этих строк в VERIFIED). Без шаблона это
            # была бы не REVIEW_REQUIRED, а UNMAPPED с чужим именем.
            p7_template, _ = ServiceTemplate.objects.get_or_create(
                category=category,
                name="Golden manicure template P7",
                defaults={
                    "name_short": "Golden P7",
                    "duration_default": 60,
                    "requires_health_check": False,
                },
            )
            salon, _ = SalonService.objects.get_or_create(
                tenant=tenant,
                category=category,
                name=f"Golden manicure {i}",
                defaults={
                    "template": p7_template,
                    "duration_minutes": 60,
                    # Связь ЕСТЬ, но не подтверждена — это и есть P7 пилота.
                    "mapping_status": SalonService.MappingStatus.REVIEW_REQUIRED,
                },
            )
            SpecialistService.objects.get_or_create(
                salon_service=salon,
                specialist=profile,
                defaults={"price": Decimal("1500.00"), "duration_minutes": 60},
            )

        verified = 0
        if verify_one:
            salon = SalonService.objects.filter(tenant=tenant, category=category).order_by("name").first()
            if salon is not None and salon.mapping_status != SalonService.MappingStatus.VERIFIED:
                salon.mapping_status = SalonService.MappingStatus.VERIFIED
                # VERIFIED без provenance схема не сохранит — и правильно.
                # Стенд называет себя правилом честно, как тестовая фикстура.
                salon.mapping_confirmed_rule = "golden_stand"
                salon.mapping_rule_version = "1.0.0"
                salon.mapping_confirmed_at = timezone.now()
                salon.mapping_source_ref = "seed_golden:--verify-one"
                salon.save()
                verified = 1

        total = SpecialistProfile.objects.filter(tenant=tenant).count()
        statuses: dict[str, int] = {}
        for s in SalonService.objects.filter(tenant=tenant).values_list("mapping_status", flat=True):
            statuses[s] = statuses.get(s, 0) + 1

        self.stdout.write(
            f"seed_golden p7: tenant={'создан' if t_new else 'был'} "
            f"category={'создана' if c_new else 'была'} "
            f"мастеров создано={created} всего={total} "
            f"связей по статусам={statuses} подтверждено сейчас={verified}"
        )
        self.stdout.write(
            self.style.SUCCESS(
                "ожидание бота: layer_2.reason_codes ∋ "
                + ("ELIG_EXCLUDED_NOT_RECOMMENDABLE (P7-b)" if not verified and statuses.get("verified", 0) == 0
                   else "кандидат на полке — golden P7 ОБЯЗАН покраснеть")
            )
        )

    @transaction.atomic
    def _seed_p1(self) -> None:
        tenant, t_new = Tenant.objects.get_or_create(
            id=P1_TENANT_ID, defaults={"slug": P1_TENANT_SLUG, "name": "Golden P1 Salon"}
        )
        category, _ = ServiceCategory.objects.get_or_create(
            slug=P1_CATEGORY_SLUG, defaults={"name": "Маникюр (golden P1)"}
        )
        username = "golden_p1_master"
        user = User.objects.filter(username=username).first()
        created = 0
        if user is None:
            user = User.objects.create_user(
                username=username, password=None, role="specialist", phone="+79990000201"
            )
            created = 1
        profile = SpecialistProfile.objects.get(user=user)
        profile.display_name = "Golden Master P1"
        profile.tenant = tenant
        profile.status = SpecialistProfile.ProfileStatus.ACTIVE
        profile.is_available = True
        profile.is_booking_enabled = True
        profile.timezone = "Europe/Moscow"
        profile.save()

        # Канонический шаблон с requires_health_check=False. Без шаблона
        # каскад отвечает None («не знаем»), и путь записи уводит бронь в
        # handoff HEALTH_CHECK_UNKNOWN (§98) — первый прогон стенда именно
        # так и кончился. P1 — прямая услуга БЕЗ медицинского вопроса,
        # значит ответ на вопрос обязан быть дан, а не отсутствовать.
        template, _ = ServiceTemplate.objects.get_or_create(
            category=category,
            name="Golden manicure template P1",
            defaults={
                "name_short": "Golden P1",
                "duration_default": P1_SALON_DURATION_MIN,
                "requires_health_check": False,
            },
        )
        salon, _ = SalonService.objects.get_or_create(
            tenant=tenant,
            category=category,
            name=P1_SERVICE_NAME,
            defaults={
                "template": template,
                "duration_minutes": P1_SALON_DURATION_MIN,
                "requires_health_check": False,
                "mapping_status": SalonService.MappingStatus.VERIFIED,
                "mapping_confirmed_rule": "golden_stand",
                "mapping_rule_version": "1.0.0",
                "mapping_confirmed_at": timezone.now(),
                "mapping_source_ref": "seed_golden:p1",
            },
        )
        if salon.template_id != template.id or salon.requires_health_check is not False:
            salon.template = template
            salon.requires_health_check = False
            salon.save(update_fields=["template", "requires_health_check"])
        edge, _ = SpecialistService.objects.get_or_create(
            salon_service=salon,
            specialist=profile,
            defaults={
                "price": P1_EDGE_PRICE,
                "duration_minutes": P1_EDGE_DURATION_MIN,
                "is_active": True,
            },
        )
        # Расписание на все семь дней: слот найдётся в любую дату, и golden
        # не зависит от того, в какой день недели его запустили.
        for day in SpecialistWorkingHours.DayOfWeek.values:
            SpecialistWorkingHours.objects.get_or_create(
                specialist=profile,
                day_of_week=day,
                defaults={
                    "is_working_day": True,
                    "start_time": time(9, 0),
                    "end_time": time(18, 0),
                },
            )

        self.stdout.write(
            f"seed_golden p1: tenant={'создан' if t_new else 'был'} id={tenant.id} "
            f"мастер={'создан' if created else 'был'} specialist_id={profile.id} "
            f"salon_service_id={salon.id} edge_id={edge.id}"
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"два числа длительности НАМЕРЕННО разные: "
                f"SalonService={salon.duration_minutes} (бронь), "
                f"SpecialistService={edge.duration_minutes} (показ); "
                f"цена ребра={edge.price} (и бронь, и показ)"
            )
        )
