"""``manage.py purge_expired_mapping_reports [--apply]`` — отчёты разбора старше срока (DRF-2409).

Решение владельца 28.09 (п.5): «Храним отчёты 90 дней с последнего прогона по
конкретному салону». Срок — от ``generated_at`` ВНУТРИ отчёта салона, не от
даты файла и не глобально (``services.mapping.store``).

Без ``--apply`` ничего не удаляет: печатает охват и что было бы удалено.
Необратимое действие требует отдельного слова. По расписанию ту же работу
делает задача ``services.purge_expired_mapping_reports`` — и тоже удаляет
только при ``MAPPING_REPORT_PURGE_ENABLED``.

Нечитаемый отчёт (нет ``generated_at`` с часовым поясом, битый файл, чужой
салон внутри) не удаляется никогда — только называется: «неизвестно, когда
был прогон» не значит «старый».
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from services.mapping.store import (
    EXPIRED,
    UNREADABLE,
    report_dir,
    retention_days,
    review_reports,
)


class Command(BaseCommand):
    help = (
        "Отчёты разбора услуг старше срока (90 дней с последнего прогона салона). "
        "По умолчанию — только отчёт; удаление — с --apply."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--apply", action="store_true", help="удалить истёкшие отчёты")

    def handle(self, *args, **options) -> None:
        from services.mapping.store import purge_expired_reports

        w = self.stdout.write
        dir_exists, ages = review_reports()
        w(f"каталог: {report_dir()} ({'есть' if dir_exists else 'НЕТ — отчётов не было ни одного'})")
        w(f"срок:    {retention_days()} дней с последнего прогона салона (generated_at в отчёте)")
        w(f"режим:   {'УДАЛЕНИЕ (--apply)' if options['apply'] else 'только отчёт, ничего не удаляется'}")
        w(f"просмотрено отчётов: {len(ages)}")
        for age in ages:
            when = age.generated_at.isoformat(timespec="seconds") if age.generated_at else "—"
            mark = {EXPIRED: "ИСТЁК", UNREADABLE: "НЕЧИТАЕМ"}.get(age.state, "свежий")
            extra = f"  ({age.reason})" if age.reason else ""
            w(f"  {age.slug:<24} {mark:<9} последний прогон: {when}{extra}")

        result = purge_expired_reports(apply=options["apply"])
        w(f"истекло: {result['expired']}; удалено: {result['deleted']}; "
          f"отказ удаления: {result['refused']}; нечитаемых: {result['unreadable']} (не удаляются)")
