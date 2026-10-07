"""``manage.py health_check_census [--tenant <slug> ...] [--details]``

Список последствий правила записи о проверке здоровья — по активным
предложениям (DRF-2877, пакет S2).

**Только чтение.** У команды нет ``--apply`` и нет ни одной записи; правило
записи она не включает и подтверждений не подставляет. Запускают её ДО
включения правила: владелец смотрит, что перестанет записываться.

Что печатается
--------------
* область замера — первой: время, база, салоны;
* у скольких предложений салон ответил и сколько ответов подтверждено; у
  скольких флаг канона выведен правилом, а не просмотрен человеком; сколько
  мастеров подняли требование сами;
* исходы гейта записи сегодня и после правила — по продаваемым рёбрам
  мастер × услуга;
* **сколько предложений сегодня проходят запись и перестанут** — это и
  есть список последствий; то, что «неизвестно» уже сегодня, изменением не
  является;
* по салонам;
* с ``--details`` — построчно: салон, код канона, название услуги, ответ
  салона и его происхождение, флаг канона и его происхождение, исходы.

После правила исходов три, и два нельзя путать: «нужна проверка клиента»
адресована клиенту, «условия услуги не определены» — ответственному за
данные услуги.

Чего команда не делает
----------------------
* **Не пишет**, правило не включает, флаги не подтверждает.
* **Не печатает людей**: мастер попадает числом.
* **Не толкует**: что включать и когда — решение владельца.
* Не видит, что делает с вердиктом гейт записи в боте, и не трогает уже
  созданные записи.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone

from services.health_check_census import (
    CLIENT_CHECK,
    PASSES,
    UNDEFINED,
    UNKNOWN,
    health_check_census,
)

OUTCOMES = {
    PASSES: "проходит (проверка не нужна)",
    CLIENT_CHECK: "нужна проверка клиента",
    UNKNOWN: "неизвестно",
    UNDEFINED: "условия услуги не определены",
}
REASONS = {
    "canon_missing": "у услуги нет канона",
    "canon_flag_inferred": "флаг канона выведен правилом, человеком не просмотрен",
    "salon_raise_unconfirmed": "салон поднял требование, подтверждения нет",
    "master_raise_unconfirmed": "мастер поднял требование, подтверждения нет",
}
ANSWER = {None: "не отвечал", True: "нужна", False: "не нужна"}


def _origin(confirmed: bool) -> str:
    return "подтверждён" if confirmed else "не подтверждён"


class Command(BaseCommand):
    help = "Перепись проверки здоровья по активным предложениям. Ничего не пишет."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--tenant", dest="tenant_slugs", action="append", default=None,
            help="Слаг салона; можно несколько раз. Без него — вся база, и это сказано в шапке.",
        )
        parser.add_argument(
            "--details", action="store_true",
            help="Построчно: каждое активное предложение с ответами и исходами.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        from django.conf import settings

        slugs: list[str] | None = options.get("tenant_slugs")
        census = health_check_census(slugs)
        write = self.stdout.write

        db = settings.DATABASES["default"]
        write("== Область замера ==")
        write(f"снято:   {timezone.now().isoformat()}")
        write(f"база:    {db.get('NAME')} на {db.get('HOST') or 'localhost'}")
        write(f"салоны:  {', '.join(slugs) if slugs else 'ВСЕ (область не сужена)'}")
        write("режим:   только чтение, записей не делается, правило записи не включается")
        write("единица: активное предложение салона; исходы — по продаваемым рёбрам мастер × услуга")
        write("")

        write("== 1. Чьим словом сегодня решается проверка ==")
        write(f"активных предложений:          {len(census.rows)}")
        write(f"  из них с продаваемым мастером: {len(census.sellable)}")
        write("ответ салона (значение, происхождение):")
        for (answer, confirmed), count in sorted(
            census.salon_answers().items(), key=lambda kv: (str(kv[0][0]), kv[0][1])
        ):
            origin = "" if answer is None else f", {_origin(confirmed)}"
            write(f"  {ANSWER[answer]}{origin}: {count}")
        write("флаг канона у предложения (значение, происхождение):")
        for (flag, confirmed), count in sorted(
            census.canon_flags().items(), key=lambda kv: (str(kv[0][0]), kv[0][1])
        ):
            label = "нет канона" if flag is None else f"{ANSWER[flag]}, {_origin(confirmed)}"
            write(f"  {label}: {count}")
        write(f"мастеров подняли требование сами: {census.reasons().get('master_raise_unconfirmed', 0)}")
        write("")

        today, after = census.outcomes("today"), census.outcomes("after")
        write("== 2. Гейт записи: сегодня и после правила (по рёбрам мастер × услуга) ==")
        write("сегодня:")
        for outcome in (PASSES, CLIENT_CHECK, UNKNOWN):
            write(f"  {OUTCOMES[outcome]:<34} {today.get(outcome, 0):>5}")
        write("  основания сегодняшнего вердикта:")
        for basis, count in sorted(census.bases().items()):
            write(f"    {basis:<20} {count:>5}")
        write("после правила:")
        for outcome in (PASSES, CLIENT_CHECK, UNDEFINED):
            write(f"  {OUTCOMES[outcome]:<34} {after.get(outcome, 0):>5}")
        write("  почему условия не определены (ребро стоит под каждой своей причиной):")
        for reason, count in sorted(census.reasons().items()):
            write(f"    {REASONS[reason]:<56} {count:>5}")
        write("")

        write("== 3. Список последствий ==")
        write(f"предложений, которые сегодня проходят запись и перестанут: {len(census.stopping)}")
        write("")

        write("== 4. По салонам ==")
        write(f"  {'салон':<28} {'активных':>8} {'с мастером':>10} {'проходит':>9} {'после':>6} {'перестанет':>11}")
        for slug in sorted({r.tenant_slug for r in census.rows}):
            rows = [r for r in census.rows if r.tenant_slug == slug]
            write(
                f"  {slug:<28} {len(rows):>8} {sum(r.sellable for r in rows):>10}"
                f" {sum(r.passes('today') for r in rows):>9} {sum(r.passes('after') for r in rows):>6}"
                f" {sum(r.stops for r in rows):>11}"
            )
        write("")

        if options.get("details"):
            write("== 5. Построчно ==")
            for r in census.rows:
                canon_flag = (
                    "нет канона" if r.canon_flag is None
                    else f"{ANSWER[r.canon_flag]} ({_origin(r.canon_confirmed)})"
                )
                salon = ANSWER[r.salon_answer] + (
                    "" if r.salon_answer is None else f" ({_origin(r.salon_confirmed)})"
                )
                if r.edges:
                    today_set = ", ".join(sorted({f"{OUTCOMES[e.today]} [{e.basis}]" for e in r.edges}))
                    after_set = ", ".join(sorted({OUTCOMES[e.after] for e in r.edges}))
                    verdicts = f"сегодня: {today_set} | после: {after_set}"
                else:
                    verdicts = "нет продаваемого мастера — записи нет"
                mark = " | ПЕРЕСТАНЕТ" if r.stops else ""
                write(
                    f"  {r.tenant_slug} | {r.canon} | {r.name} | салон: {salon} | канон: {canon_flag}"
                    f" | мастеров {len(r.edges)}, подняли {r.master_raises} | {verdicts}{mark}"
                )
            write("")

        write("Правило записи не включено. Подтверждений команда не подставляет.")
