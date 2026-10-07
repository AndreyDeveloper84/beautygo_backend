"""``manage.py body_care_admission_dry_run [--tenant <slug> ...] [--details] [--fail-on-unclassified]``

Что закроет включение ``BODY_CARE_UNCLASSIFIED_FAIL_CLOSED`` — по активным
предложениям, с причинами (DRF-2866).

**Только чтение.** У команды нет ``--apply`` и нет ни одной записи; флаг на
стенде она не трогает. Запускают её ДО включения флага: владелец принимает
по её выводу список закрываемых предложений.

Зачем
-----
Условие включения флага — ноль активных предложений с неизвестной областью
**либо** список, принятый владельцем. Число по шаблонам этого не показывает:
канон с неизвестной областью может не продаваться никем, а предложение без
канона в переписи шаблонов не видно вовсе. Единица здесь — активное
предложение салона.

Что печатается
--------------
* область замера — первой: время, база, салоны, текущее положение флага;
* сколько предложений допущено к персональной рекомендации сегодня и после
  включения; сколько закроется и сколько откроется (ожидается ноль);
* причины после включения: под каждой причиной — все предложения, что её
  несут, и отдельно — «первая причина» в порядке резолвера (она уйдёт на
  провод);
* предложения с неизвестной областью — по разделам кода справочника,
  отдельной строкой «нет канона»;
* по салонам;
* с ``--details`` — построчно: салон, код канона, название услуги, вердикт
  сегодня и после, все причины.

Чего команда не делает
----------------------
* **Не пишет** и флаг не включает.
* **Не печатает людей**: мастер попадает числом. Название услуги
  печатается — без него владелец не узнает предложение без канона.
* **Не толкует**: какой список принять — решение владельца.
* Не знает про гейт новой записи и про то, что ответит бот: проверки
  закрывают персональную рекомендацию.

``--fail-on-unclassified`` — код выхода 2, если есть хотя бы одно активное
предложение с неизвестной областью: для автоматической проверки условия.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone

from services.body_care_admission import FLAG, REASONS, admission_census

TITLES = {
    "link_not_verified": "связь с каноном не подтверждена",
    "canon_retired": "канон выведен из оборота",
    "no_sellable_master": "нет продаваемого мастера",
    "unclassified": "область неизвестна / нет канона",
    "config_not_ready": "конфигурация Body Care не готова",
    "class_unconfirmed": "юридический класс не подтверждён",
    "license_not_verified": "лицензия салона не проверена",
    "license_scope_mismatch": "канон вне объёма лицензии",
    "no_master_cleared": "ни один мастер не прошёл адрес и квалификацию",
}


class Command(BaseCommand):
    help = "Dry-run включения флага fail-closed по активным предложениям. Ничего не пишет."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--tenant", dest="tenant_slugs", action="append", default=None,
            help="Слаг салона; можно несколько раз. Без него — вся база, и это сказано в шапке.",
        )
        parser.add_argument(
            "--details", action="store_true",
            help="Построчно: каждое активное предложение с вердиктом и причинами.",
        )
        parser.add_argument(
            "--fail-on-unclassified", action="store_true",
            help="Код выхода 2, если есть активное предложение с неизвестной областью.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        from django.conf import settings

        slugs: list[str] | None = options.get("tenant_slugs")
        census = admission_census(slugs)
        write = self.stdout.write

        db = settings.DATABASES["default"]
        write("== Область замера ==")
        write(f"снято:   {timezone.now().isoformat()}")
        write(f"база:    {db.get('NAME')} на {db.get('HOST') or 'localhost'}")
        write(f"салоны:  {', '.join(slugs) if slugs else 'ВСЕ (область не сужена)'}")
        write(f"флаг:    {FLAG} сейчас {'ВКЛЮЧЁН' if census.flag_now else 'выключен — обход открыт'}")
        write("режим:   только чтение, записей не делается, флаг не меняется")
        write("единица: активное предложение салона, включая предложения без канона")
        write("")

        today, after = census.available("today"), census.available("after")
        write("== 1. Допуск к персональной рекомендации ==")
        write(f"активных предложений:        {census.total}")
        write(f"  допущено сегодня:          {len(today)}")
        write(f"  допущено после включения:  {len(after)}")
        write(f"  закроется при включении:   {len(census.closing)}")
        write(f"  откроется при включении:   {len(census.opening)}  (ожидается 0)")
        write("")

        write("== 2. Причины после включения ==")
        write("предложение стоит под КАЖДОЙ своей причиной; в скобках — сколько из них")
        write("несут её первой в порядке резолвера (она уйдёт на провод)")
        carrying, first = census.reasons("after"), census.first_reasons("after")
        today_carrying = census.reasons("today")
        for reason in REASONS:
            write(
                f"  {TITLES[reason]:<46} {carrying.get(reason, 0):>4}"
                f"  (первой {first.get(reason, 0)}; сегодня {today_carrying.get(reason, 0)})"
            )
        write("")

        unknown = census.unclassified
        write("== 3. Неизвестная область — условие включения ==")
        write(f"активных предложений с неизвестной областью: {len(unknown)}")
        write(f"  из них допущены сегодня (их закроет именно это): "
              f"{sum(1 for r in unknown if r.today.available)}")
        for section, count in sorted(Counter(r.section for r in unknown).items()):
            write(f"  {section:<12} {count}")
        write("")

        write("== 4. По салонам ==")
        write(f"  {'салон':<28} {'активных':>8} {'сегодня':>8} {'после':>6} {'закроется':>10}")
        for slug in sorted({r.tenant_slug for r in census.rows}):
            rows = [r for r in census.rows if r.tenant_slug == slug]
            write(
                f"  {slug:<28} {len(rows):>8} {sum(r.today.available for r in rows):>8}"
                f" {sum(r.after.available for r in rows):>6}"
                f" {sum(r.today.available and not r.after.available for r in rows):>10}"
            )
        write("")

        if options.get("details"):
            write("== 5. Построчно ==")
            for r in census.rows:
                write(
                    f"  {r.tenant_slug} | {r.canon} | {r.name} | мастеров {r.sellable_masters}"
                    f" | сегодня: {_verdict(r.today)} | после: {_verdict(r.after)}"
                )
            write("")

        write("Обход считается закрытым только после фактического включения флага.")
        if options.get("fail_on_unclassified") and unknown:
            raise SystemExit(2)


def _verdict(verdict) -> str:
    return "допущено" if verdict.available else "закрыто: " + ", ".join(verdict.reasons)
