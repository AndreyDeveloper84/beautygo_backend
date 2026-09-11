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
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from services.models import SalonService, ServiceCategory, SpecialistService
from tenants.models import Tenant
from users.models import SpecialistProfile, User

TENANT_SLUG = "golden-p7"
CATEGORY_SLUG = "golden-manicure"
MASTER_COUNT = 3


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
        parser.add_argument("--scenario", choices=["p7"], required=True)
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

            salon, _ = SalonService.objects.get_or_create(
                tenant=tenant,
                category=category,
                name=f"Golden manicure {i}",
                defaults={
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
