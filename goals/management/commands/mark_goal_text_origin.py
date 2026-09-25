"""Пометить прежние свободные тексты цели как слова человека (DRF-2283, CD §73).

Контракт Goal этапа C (G-INV-15) требует у дословного текста цели пометку
источника `user_stated`. Поле заведено этим же листом и ставится при записи;
строки, записанные раньше, пометки не имеют — и **не получают её молча**.

### Почему команда, а не миграция data-шагом

Миграция пометила бы людей в момент слияния, и нажавший «слить» не был бы
тем, кто решил. Раздел «Миграции» контракта бэкфилл ДОПУСКАЕТ, но не
доказывает, что каждая существующая строка — слова человека; доказательство
нужно нам, а не контракту. Поэтому: без ``--apply`` команда только считает и
печатает, ``--apply`` — по слову владельца, и запускает его он.

### Что метится — и что не метится

Метится: цель с непустым ``goal_text`` и пустой пометкой. Свободный текст
попадал в ``ClientGoal`` только двумя путями — прямой выбор (``goals/select``
с ``goal_text``) и шаг цели анкеты, — и оба это ввод человека.

Не метится: строки без текста (чип — там слов человека нет) и строки, у
которых пометка уже стоит. Значения ``goal_text``, ``goal_key``,
``source_channel`` и состояние цели не трогаются: это пометка, а не правка.

Дословный текст команда **не печатает** — только числа и ``client_id`` (pk,
не телефон и не имя), как ``mark_legacy_default_inputs``.

Usage:
    python manage.py mark_goal_text_origin
    python manage.py mark_goal_text_origin --apply
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from goals.models import ClientGoal

#: Поля, которые команда обязана оставить нетронутыми. Перечитываются внутри
#: транзакции «до» и «после»; расхождение — откат.
UNTOUCHED = ("goal_key", "goal_text", "source_channel", "state")


class Command(BaseCommand):
    help = (
        "Mark pre-existing free-text goals as the person's own words "
        "(text_origin=user_stated). Dry run unless --apply."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the mark. Without it the command only counts (the default).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        apply = bool(options["apply"])
        candidates = ClientGoal.objects.filter(text_origin="").exclude(goal_text__isnull=True)
        candidates = candidates.exclude(goal_text="")
        rows = list(candidates.values("pk", "client_id", "goal_key"))

        with_key = [r for r in rows if r["goal_key"]]
        text_only = [r for r in rows if not r["goal_key"]]
        already = ClientGoal.objects.filter(
            text_origin=ClientGoal.TextOrigin.USER_STATED
        ).count()
        no_text = ClientGoal.objects.filter(text_origin="").filter(
            goal_text__isnull=True
        ).count()

        self.stdout.write(f"{'APPLY' if apply else 'DRY RUN'} — text_origin=user_stated")
        self.stdout.write(f"  to_mark_total={len(rows)}")
        self.stdout.write(f"    text_with_key={len(with_key)}  text_only={len(text_only)}")
        self.stdout.write(f"  already_marked={already}  no_text_not_marked={no_text}")
        self.stdout.write(
            "  clients=" + ", ".join(str(r["client_id"]) for r in rows[:50]) + (" …" if len(rows) > 50 else "")
        )

        if not apply:
            self.stdout.write("nothing written; rerun with --apply")
            return

        pks = [r["pk"] for r in rows]
        with transaction.atomic():
            before = {
                row.pk: tuple(getattr(row, f) for f in UNTOUCHED)
                for row in ClientGoal.objects.filter(pk__in=pks)
            }
            updated = ClientGoal.objects.filter(pk__in=pks, text_origin="").update(
                text_origin=ClientGoal.TextOrigin.USER_STATED
            )
            after = {
                row.pk: tuple(getattr(row, f) for f in UNTOUCHED)
                for row in ClientGoal.objects.filter(pk__in=pks)
            }
            if before != after:
                raise RuntimeError(
                    "mark_goal_text_origin touched a field it must not: rolling back"
                )
        self.stdout.write(f"marked={updated}")
