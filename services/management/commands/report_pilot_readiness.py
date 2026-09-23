"""``manage.py report_pilot_readiness [--tenant <slug>]`` — три числа, три решения.

**Только чтение.** У команды нет ``--apply`` и нет ни одной записи: она
считает и печатает. Запускает владелец на стенде.

Зачем именно ЭТИ три числа
--------------------------

Они не набор счётчиков «на всякий случай»: каждое разблокирует **своё**
решение владельца, и без числа каждое из трёх принимается вслепую.

1. **Связи услуг с каноном** (DRF-2361). ``verified`` запрещён схемой без
   связи с каноном (``salonservice_verified_requires_template``). Если
   verified-связей нет, «проверенные услуги» на экранах опираются на пустое
   множество, и владельцу решать: доводить разметку до запуска, показывать
   услуги без признака проверенности или убрать обещание с экранов.
   В коде сегодня живёт **недатированный пересказанный замер** («265 услуг,
   связей нет») — на него опираться нельзя, поэтому здесь число снимается
   с названной областью и временем.
2. **Доля услуг с меткой цели** (DRF-2358). Шаг плана «записаться под цель»
   ведёт в плоский прайс. Критерий из листа: если меткой покрыто близко к
   100 % услуг, отбор по цели ничего не даст и разговор уходит к разметке;
   если заметно меньше — собираем «Под цель / Все услуги».
3. **Доля мастеров с фото** (решение по карточке ближайшей записи). На
   макете нарисовано фото процедуры, которого в контракте нет вовсе; портрет
   мастера есть. Если фото почти ни у кого нет, «поставить портрет» даст
   всем пустой кружок — пункт будет сделан и не виден.

Что команда НЕ делает
---------------------

* **Не пишет.** Ни ``--apply``, ни записи, ни миграций: у неё нет ни одного
  вызова ``save``/``update``/``create``.
* **Не печатает людей.** Ни имён, ни логинов, ни телефонов, ни адресов, ни
  ссылок на сами фото — только числа по группам и слаг тенанта, когда область
  сужена. Мастер попадает в отчёт числом, а не строкой.
* **Не толкует.** Печатается измеренное; какой порог считать «близко к 100 %»
  и что из этого следует — решение владельца, оно в листах.

Область печатается ПЕРВОЙ
-------------------------

База, тенант и время снятия идут до чисел. Без этого «0 verified» с
эталонного сида и «0 verified» с живого стенда выглядят одинаково, а
означают разное — ровно та ошибка, из-за которой прежний замер нельзя было
взять в решение.

Почему фото меряется по ``avatar``
----------------------------------

В контракте наружу поле зовётся ``photo_url``, но в каталоге хранится
``SpecialistProfile.avatar`` (``ImageField``), и ``photo_url`` собирается из
него. Меряется источник, а не витрина: если витрина однажды начнёт отдавать
заглушку, число по витрине станет врать, число по источнику — нет.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db.models import Count, Q
from django.utils import timezone


class Command(BaseCommand):
    help = "Читающий отчёт готовности пилота: канон, метки целей, фото мастеров. Ничего не пишет."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--tenant",
            dest="tenant_slug",
            default=None,
            help="Слаг тенанта: сузить область. Без него — вся база, и это сказано в шапке.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        from django.conf import settings

        from services.models import GoalOptionCategory, SalonService
        from tenants.models import Tenant
        from users.models import SpecialistProfile

        tenant_slug: str | None = options.get("tenant_slug")
        tenant = None
        if tenant_slug:
            tenant = Tenant.objects.filter(slug=tenant_slug).first()
            if tenant is None:
                self.stderr.write(f"Тенанта со слагом {tenant_slug!r} нет — область не сужена.")
                return

        # ── область и время: печатаются ДО чисел ────────────────────────
        db = settings.DATABASES["default"]
        self.stdout.write("== Область замера ==")
        self.stdout.write(f"снято:   {timezone.now().isoformat()}")
        self.stdout.write(f"база:    {db.get('NAME')} на {db.get('HOST') or 'localhost'}")
        self.stdout.write(
            f"тенант:  {tenant.slug if tenant else 'ВСЕ (область не сужена)'}"
        )
        self.stdout.write("режим:   только чтение, записей не делается")
        self.stdout.write("")

        services = SalonService.objects.all()
        if tenant is not None:
            services = services.filter(tenant=tenant)

        # ── 1. связи с каноном (DRF-2361) ───────────────────────────────
        by_status = dict(
            services.values_list("mapping_status").annotate(n=Count("id")).values_list(
                "mapping_status", "n"
            )
        )
        total_services = sum(by_status.values())
        active_services = services.filter(is_active=True).count()
        with_template = services.filter(template__isnull=False).count()
        verified_active = services.filter(is_active=True, mapping_status="verified").count()

        self.stdout.write("== 1. Услуги и канон (DRF-2361) ==")
        self.stdout.write(f"услуг всего:              {total_services}")
        self.stdout.write(f"  из них активных:        {active_services}")
        self.stdout.write(f"со связью с каноном:      {with_template}")
        for status in ("unmapped", "review_required", "verified", "not_recommendable"):
            self.stdout.write(f"  {status:<18} {by_status.get(status, 0)}")
        self.stdout.write(f"verified и активна:       {verified_active}")
        self.stdout.write("")

        # ── 2. метка цели (DRF-2358) ────────────────────────────────────
        #
        # Метка цели живёт не на услуге, а на её КАТЕГОРИИ: курируемая
        # таблица §51 связывает цель с категориями (``GoalOptionCategory``).
        # Поэтому «услуга под цель» = «категория услуги названа хоть одной
        # целью»; услуга без категории меткой не покрыта по построению, и
        # она считается отдельно, а не растворяется в «нет метки».
        #
        # Услуг без категории схема сегодня не принимает (``clean``: категория
        # обязательна у услуги вне таксономии), но в живой базе такие строки
        # есть — они старше проверки. Группа печатается ради них.
        goal_categories = set(
            GoalOptionCategory.objects.values_list("category_id", flat=True).distinct()
        )
        active = services.filter(is_active=True)
        without_category = active.filter(category__isnull=True).count()
        with_goal = active.filter(category_id__in=goal_categories).count()
        with_category_no_goal = active.filter(category__isnull=False).exclude(
            category_id__in=goal_categories
        ).count()

        self.stdout.write("== 2. Метка цели у активных услуг (DRF-2358) ==")
        self.stdout.write(f"активных услуг:           {active_services}")
        self.stdout.write(f"  категория названа целью:{with_goal:>4}  {_share(with_goal, active_services)}")
        self.stdout.write(f"  категория есть, цели нет:{with_category_no_goal:>3}")
        self.stdout.write(f"  категории нет вовсе:    {without_category:>4}")
        goals_in_table = GoalOptionCategory.objects.values("goal_option_id").distinct().count()
        self.stdout.write(f"целей в таблице §51:      {goals_in_table}")
        self.stdout.write("")

        # ── 3. фото мастеров ────────────────────────────────────────────
        masters = SpecialistProfile.objects.all()
        if tenant is not None:
            masters = masters.filter(tenant=tenant)
        active_masters = masters.filter(status="active")
        total_masters = active_masters.count()
        with_photo = active_masters.exclude(Q(avatar="") | Q(avatar__isnull=True)).count()

        self.stdout.write("== 3. Фото у активных мастеров ==")
        self.stdout.write(f"активных мастеров:        {total_masters}")
        self.stdout.write(f"  фото есть:              {with_photo:>4}  {_share(with_photo, total_masters)}")
        self.stdout.write(f"  фото нет:               {total_masters - with_photo:>4}")
        self.stdout.write("")
        self.stdout.write(
            "Ссылок на сами фото, имён и контактов в отчёте нет — только числа по группам."
        )


def _share(part: int, whole: int) -> str:
    """Доля в процентах, или «—», когда делить не на что.

    Ноль из нуля — не «0 %»: это «мерить нечего», и разница видна владельцу.
    """
    if whole <= 0:
        return "(—)"
    return f"({part * 100 // whole} %)"
