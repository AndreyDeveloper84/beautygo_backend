"""Удаление параметров тела, собранных без согласия (§120, срез N-a2).

Решение владельца §120 (10.09.2026): четыре записанных веса удаляются.
Условия названы там же и исполнены здесь.

### Почему команда, а не миграция

Слияние в ``dev`` — это выкладка. Миграция стёрла бы данные живых людей
**в момент слияния**, то есть как побочный эффект работы очереди PR, и
человек, нажавший «слить», не был бы тем, кто решил удалить. Команда
разводит эти два действия во времени: PR можно слить сегодня, а удаление
запустить тогда и тем, кем решено.

### Почему по умолчанию ничего не делает

Без ``--apply`` команда только ПЕЧАТАЕТ. Запуск без флага — это отчёт о
том, что будет стёрто; необратимое действие требует отдельного слова, а
не отдельного везения.

### Что печатается

**Значения, а не счётчик.** «Удалили четыре» — утверждение о числе строк;
«удалили 95.0, 90.0, 67.0, 65.0» — о том, что именно ушло. Второе можно
проверить, первое приходится принимать на слово.

### Что эта команда НЕ делает, и это важнее того, что делает

Объём §120 — три столбца: ``weight_kg``, ``height_cm``, ``age``.
``gender``, ``activity_coefficient`` и ``goal`` остаются по решению
владельца.

**Числа, посчитанные ОТ удаляемых параметров, остаются тоже.** ``bmr``,
``daily_kcal`` и прочие ориентиры вычислены из веса, роста и возраста, и
после удаления они переживут свои входы. §92 правило 5 говорит: «уже
рассчитанный ориентир нельзя продолжать показывать как актуальный без его
происхождения» — а происхождения после этого удаления не остаётся.

Это не гипотеза: **два профиля пилота уже находятся в этом состоянии** —
вес пуст, а ``bmr`` и ``daily_kcal`` заполнены (замер 10.09.2026,
реестр §29/§120). Команда печатает остающиеся числа по каждой строке,
чтобы запускающий видел, чего она НЕ сделала, а не узнавал об этом
потом.

Расширять объём молча нельзя — он назван решением. Вопрос вынесен
отдельно; до ответа команда исполняет ровно то, что решено.

### Снимок входов

``targets_input_snapshot`` хранит те же три параметра под теми же именами
и был бы вторым местом, где вес переживает удаление. На 10.09.2026 он
пуст у всех шести профилей (``targets_source=unknown_legacy`` — расчёты
старше введения провенанса), поэтому чистить сегодня нечего. Команда всё
равно его чистит: пусто сегодня не значит пусто в день запуска.

Usage:
    python manage.py purge_unconsented_body_parameters
    python manage.py purge_unconsented_body_parameters --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from nutrition.models import NutritionProfile

#: Объём удаления по §120. Расширять только решением владельца.
PURGED_FIELDS = ("weight_kg", "height_cm", "age")

#: Ключи снимка входов, несущие те же параметры под теми же именами.
PURGED_SNAPSHOT_KEYS = ("weight_kg", "height_cm", "age")

#: Числа, посчитанные от удаляемых параметров. НЕ удаляются — печатаются,
#: чтобы запускающий видел, что осталось.
DERIVED_FIELDS = ("bmr", "daily_kcal", "daily_protein_g", "daily_fat_g",
                  "daily_carbs_g")


class Command(BaseCommand):
    help = (
        "Стереть параметры тела, собранные без согласия (§120). "
        "Без --apply только печатает."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Выполнить удаление. Без флага команда ничего не меняет — "
                "необратимое действие требует отдельного слова."
            ),
        )

    def handle(self, *args, **options):
        apply = options["apply"]

        rows = list(
            NutritionProfile.objects.all().order_by("user_id")
        )
        affected = [
            p for p in rows
            if any(getattr(p, f) is not None for f in PURGED_FIELDS)
            or any((p.targets_input_snapshot or {}).get(k) is not None
                   for k in PURGED_SNAPSHOT_KEYS)
        ]

        self.stdout.write(
            f"Профилей всего: {len(rows)}. "
            f"Несут параметры тела: {len(affected)}."
        )
        if not affected:
            self.stdout.write(self.style.SUCCESS("Стирать нечего."))
            return

        # ЗНАЧЕНИЯ, а не счётчик — §120. Печатаются ДО удаления, иначе
        # после него их уже неоткуда взять, и отчёт стал бы утверждением
        # о числе строк вместо утверждения о том, что ушло.
        self.stdout.write("")
        self.stdout.write("Будет стёрто:" if not apply else "Стирается:")
        for p in affected:
            snap = p.targets_input_snapshot or {}
            values = ", ".join(
                f"{f}={getattr(p, f)!r}" for f in PURGED_FIELDS
            )
            snap_values = ", ".join(
                f"{k}={snap.get(k)!r}" for k in PURGED_SNAPSHOT_KEYS
            )
            self.stdout.write(f"  user={p.user_id}  {values}")
            self.stdout.write(f"      снимок входов: {snap_values}")
            remaining = ", ".join(
                f"{f}={getattr(p, f)!r}" for f in DERIVED_FIELDS
            )
            # То, чего команда НЕ делает, печатается рядом с тем, что
            # делает. Иначе «параметры тела удалены» прочитается как
            # «от тела ничего не осталось», а осталось.
            self.stdout.write(f"      ОСТАЁТСЯ (посчитано от них): {remaining}")

        if not apply:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "Ничего не изменено. Для удаления: --apply"
                )
            )
            return

        with transaction.atomic():
            for p in affected:
                for f in PURGED_FIELDS:
                    setattr(p, f, None)
                snap = dict(p.targets_input_snapshot or {})
                for k in PURGED_SNAPSHOT_KEYS:
                    snap.pop(k, None)
                p.targets_input_snapshot = snap
                p.save(
                    update_fields=[
                        *PURGED_FIELDS, "targets_input_snapshot", "updated_at",
                    ]
                )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"Стёрто профилей: {len(affected)}.")
        )
        self.stdout.write(
            "Ориентиры, посчитанные от стёртых параметров, НЕ тронуты — "
            "объём §120 их не называет. Показ ориентира без происхождения "
            "запрещён §92 правилом 5; это отдельное решение."
        )
