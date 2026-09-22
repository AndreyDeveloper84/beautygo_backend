"""Пометка прежних умолчаний темпа и активности — ``legacy_default`` (DRF-2279).

Решение владельца (CD §76, №32): «старые значения пометить как
legacy_default и запросить подтверждение, а не считать ответами человека».
#543 (вопрос 59) убрал подстановку для новых расчётов; строки, записанные
раньше, хранят подставленное рядом с названным.

### Что метится — только доказуемое, по группам

* ``activity_1_4`` — активность ровно 1.4: умолчание схемы (``default=1.4``
  до #543) и ``get_or_create(defaults=…)``. Такого ответа в боте нет и не
  было (1.2 / 1.375 / 1.55 / 1.725); подводка к 1.375 в столбец не писалась.
* ``activity_skipped`` — «Не знаю»: бот слал 1.375 за человека.
* ``pace_moderate_no_pace_goal`` / ``pace_moderate_pace_goal`` — темп
  «moderate». До #543 расчёт писал свой темп обратно в столбец
  (``profile.pace = norms.pace``, ``norms.pace = pace or "moderate"``), и
  каждый пересчёт без темпа оставлял «moderate»; шага темпа в боте до #1972
  (22.09) не было. Названный «moderate» от подставленного по строке НЕ
  отличим — метится весь (решение главного окна 22.09), двумя счётчиками.
  Неизвестно, был ли выбор темпа в приложении Ayla: если был, часть
  помеченных назвали сами — они подтвердят одним нажатием.

Не метится: темп «gentle» (расчёт его не подставлял), активность из набора
без ``activity_skipped`` (названа), пустые значения (они и так «не названы»).

### Почему команда, а не миграция, и почему по умолчанию ничего не пишет

Миграция пометила бы людей в момент слияния, и нажавший «слить» не был бы
тем, кто решил. Без ``--apply`` команда только печатает группы и ``user_id``
(pk — не телефон и не имя; параметры тела не печатаются). Сухой прогон на
стенде — главное окно после выкладки; ``--apply`` — по слову владельца.

### Что трогается — стражей, а не обещанием

Только ``legacy_default_inputs``. Сами значения (``activity_coefficient``,
``pace``) и ``health_flags`` НЕ меняются — пометка, не стирание, и не флаг
здоровья (DT-1: любой флаг здоровья выключает внешнюю модель). Внутри
транзакции кортеж нетронутых полей перечитывается «до» и «после»;
расхождение — откат.

Usage:
    python manage.py mark_legacy_default_inputs
    python manage.py mark_legacy_default_inputs --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from nutrition.models import NutritionProfile
from nutrition.services.nutrition_profile_service import PACE_GOALS

#: Прежнее умолчание схемы активности (до #543).
LEGACY_ACTIVITY = 1.4

#: Группы — в порядке печати.
GROUPS: tuple[str, ...] = (
    "activity_1_4",
    "activity_skipped",
    "pace_moderate_no_pace_goal",
    "pace_moderate_pace_goal",
)

#: Поля, которые команда обязана оставить как были.
UNTOUCHED: tuple[str, ...] = ("activity_coefficient", "pace", "goal", "health_flags")


def _groups_for(profile: NutritionProfile) -> dict[str, str]:
    """Группа → помечаемый вход, для одной строки; пусто — метить нечего."""
    out: dict[str, str] = {}
    flags = profile.health_flags or {}
    if profile.activity_coefficient == LEGACY_ACTIVITY:
        out["activity_1_4"] = "activity_coefficient"
    elif flags.get("activity_skipped") and profile.activity_coefficient is not None:
        out["activity_skipped"] = "activity_coefficient"
    if profile.pace == NutritionProfile.Pace.MODERATE:
        key = (
            "pace_moderate_pace_goal"
            if profile.goal in PACE_GOALS
            else "pace_moderate_no_pace_goal"
        )
        out[key] = "pace"
    already = set(profile.legacy_default_inputs or [])
    return {group: name for group, name in out.items() if name not in already}


class Command(BaseCommand):
    help = "Пометить прежние умолчания темпа и активности как legacy_default (DRF-2279)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Записать пометки. Без флага — только печать.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        plan: dict[int, dict[str, str]] = {}
        by_group: dict[str, list[int]] = {g: [] for g in GROUPS}
        for profile in NutritionProfile.objects.all().order_by("pk"):
            groups = _groups_for(profile)
            if groups:
                plan[profile.pk] = groups
                for group in groups:
                    by_group[group].append(profile.pk)

        for group in GROUPS:
            ids = by_group[group]
            self.stdout.write(f"{group}: {len(ids)}" + (f" — user_id {ids}" if ids else ""))

        if not plan:
            self.stdout.write("Метить нечего.")
            return
        if not apply:
            self.stdout.write(
                f"Сухой прогон: пометок {len(plan)} профилей не записано. "
                "Записать — с --apply."
            )
            return

        with transaction.atomic():
            rows = list(NutritionProfile.objects.select_for_update().filter(pk__in=list(plan)))
            before = {r.pk: tuple(getattr(r, f) for f in UNTOUCHED) for r in rows}
            for row in rows:
                marks = set(row.legacy_default_inputs or []) | set(plan[row.pk].values())
                row.legacy_default_inputs = sorted(marks)
                row.save(update_fields=["legacy_default_inputs"])
            after = {
                r.pk: (tuple(getattr(r, f) for f in UNTOUCHED), set(r.legacy_default_inputs or []))
                for r in NutritionProfile.objects.filter(pk__in=list(plan))
            }
            for pk, (untouched, marks) in after.items():
                if untouched != before[pk]:
                    raise CommandError(f"user_id {pk}: нетронутые поля изменились — откат")
                if not set(plan[pk].values()) <= marks:
                    raise CommandError(f"user_id {pk}: пометка не записалась — откат")

        self.stdout.write(f"Помечено профилей: {len(plan)}.")
