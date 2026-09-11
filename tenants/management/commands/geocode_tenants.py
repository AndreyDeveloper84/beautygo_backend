"""Разовое заполнение координат мест (§139) — сухой прогон по умолчанию.

Зачем команда, если §139 говорит «при сохранении адреса»
--------------------------------------------------------

§139 предусматривает оба пути: «при сохранении или изменении адреса» и
«для разового заполнения координат уже существующих адресов». Эта команда —
второй путь. Адреса у одиннадцати мест на пилоте **ввёл человек** 11.09.2026
по слову владельца; их геокодирование по одному — тот же паттерн, что при
сохранении, только отложенный.

Предел, продиктованный лицензией, — и он про ЧИСЛО, а не про способ
--------------------------------------------------------------------

Оферта DaData, п. 4.2.1: «запрещается использовать сервис „Подсказки" для
автоматической обработки адресов». Одиннадцать адресов, введённых людьми, —
не автоматическая обработка. Импорт тысячи строк из чужой базы — она.
Команда **не различает** эти случаи по существу, поэтому различает по числу:
выше ``BATCH_CEILING`` строк она отказывается идти к провайдеру и печатает,
почему. Число названо, а не спрятано в условии.

Что печатается и в каком порядке
--------------------------------

1. **Предмет** — кто отвечает на замер (``core.measurement_subject``):
   хост, база, время старта БД, пульс. Числа без предмета не читаются.
2. **Предмет прогона** — провайдер, режим, порог пачки, число строк.
3. Построчно: slug, адрес, исход, статус, точность, город из ответа.
4. **Счётчики по шести статусам** и по причинам пропуска. Отдельной строкой
   — сколько строк ждут человека (``AMBIGUOUS``): поверхности подтверждения
   пока нет ни у кого, и без этой строки очередь на подтверждение невидима.

Сухой прогон печатает всё то же самое и не пишет ничего; ``--apply`` пишет.
Коды выхода: 2 — провайдер не готов или пачка выше потолка, ничего не
сделано; 1 — прогон прошёл, но есть строки, требующие человека.
"""
from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.geocoding.apply import Applied, apply_result
from core.geocoding.contract import Outcome
from core.geocoding.providers import PROVIDERS, get_provider
from core.measurement_subject import gather_pulse, subject_lines
from tenants.models import GeocodeStatus, Tenant

#: Выше — не «разовое заполнение введённых людьми адресов», а обработка
#: базы. Одиннадцать мест на пилоте — внутри с запасом; импорт — нет.
BATCH_CEILING = 50


class Command(BaseCommand):
    help = "Геокодировать адреса мест (Tenant.address) с происхождением по §139. Сухой прогон по умолчанию."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--provider", default=None,
            help=f"Имя провайдера из реестра: {', '.join(sorted(PROVIDERS))}. Обязателен.",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="Записать результаты. Без флага — сухой прогон: всё считается, ничего не пишется.",
        )
        parser.add_argument(
            "--overwrite-ok", action="store_true",
            help="Перегеокодировать строки со статусом ok. confirmed не трогается никогда.",
        )
        parser.add_argument(
            "--slug", action="append", default=None,
            help="Ограничить прогон этими slug'ами (можно несколько раз).",
        )

    def handle(self, *args, **options) -> None:
        # 1. Предмет — до всякого счёта.
        pulses = gather_pulse()
        for line in subject_lines(pulses=pulses):
            self.stdout.write(line)
        self.stdout.write("")

        # 2. Провайдер — и отказ ДО первого адреса, если он не готов.
        name = options["provider"]
        if not name:
            self.stderr.write(self.style.ERROR(
                f"--provider обязателен; известны: {', '.join(sorted(PROVIDERS))}"
            ))
            raise SystemExit(2)
        try:
            provider = get_provider(name)
        except ValueError as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            raise SystemExit(2)
        refusal = provider.check()
        if refusal is not None:
            self.stderr.write(self.style.ERROR(
                f"провайдер {name!r} не готов, ни одного запроса не сделано: {refusal.reason}"
            ))
            raise SystemExit(2)

        # 3. Вход. Пустой адрес — не кандидат: геокодеру нечего дать, и
        # запись pending на пустой строке выглядела бы как лежащий сервис.
        qs = Tenant.objects.order_by("slug")
        if options["slug"]:
            qs = qs.filter(slug__in=options["slug"])
        candidates = [t for t in qs if t.address.strip()]
        without_address = qs.count() - len(candidates)

        mode = "ЗАПИСЬ (--apply)" if options["apply"] else "СУХОЙ ПРОГОН"
        self.stdout.write(
            f"== ПРОГОН: провайдер={name} · режим={mode} · "
            f"потолок пачки={BATCH_CEILING} · строк с адресом={len(candidates)} · "
            f"без адреса={without_address} =="
        )

        if len(candidates) > BATCH_CEILING:
            self.stderr.write(self.style.ERROR(
                f"{len(candidates)} адресов — это обработка базы, а не разовое заполнение "
                f"введённых людьми адресов (потолок {BATCH_CEILING}). Оферта DaData п. 4.2.1 "
                f"запрещает автоматическую обработку через «Подсказки». Сузьте --slug."
            ))
            raise SystemExit(2)

        # 4. Построчно.
        now = timezone.now()
        applied: list[Applied] = []
        for tenant in candidates:
            result = provider.geocode(tenant.address)
            a = apply_result(
                tenant, result,
                source_address=tenant.address, now=now,
                overwrite_ok=options["overwrite_ok"], dry_run=not options["apply"],
            )
            applied.append(a)
            verdict = a.status.value if a.written else f"пропуск: {a.skipped_because}"
            self.stdout.write(
                f"  {tenant.slug:<24} {tenant.address[:40]:<40} "
                f"{result.outcome.value:<13} → {verdict}"
                + (f"  [{result.precision.value}; {result.locality or '—'}]"
                   if result.outcome in (Outcome.FOUND, Outcome.MULTIPLE) else "")
            )

        # 5. Счётчики — по шести статусам, а не «успех/ошибка».
        by_status = Counter(a.status.value for a in applied if a.written)
        by_skip = Counter(a.skipped_because for a in applied if not a.written)
        self.stdout.write("")
        self.stdout.write(f"== ИТОГ ({mode}) ==")
        for status in GeocodeStatus:
            self.stdout.write(f"  {status.value:<14}: {by_status.get(status.value, 0)}")
        for reason, n in sorted(by_skip.items()):
            self.stdout.write(f"  пропуск — {reason}: {n}")
        self.stdout.write(f"  без адреса     : {without_address}")

        waiting = by_status.get(GeocodeStatus.AMBIGUOUS.value, 0)
        if waiting:
            self.stdout.write(self.style.WARNING(
                f"ЖДУТ ЧЕЛОВЕКА: {waiting}. Поверхности подтверждения пока нет — "
                f"эта строка единственное, что делает очередь видимой."
            ))
        if not options["apply"]:
            self.stdout.write(self.style.WARNING(
                "сухой прогон — в базу не записано ничего; повторить с --apply"
            ))
        if waiting:
            raise SystemExit(1)
