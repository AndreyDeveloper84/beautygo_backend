"""Проставить признаки демонстрационности и тестовости существующим строкам.

Зачем отдельная команда
-----------------------
Поля ``Tenant.is_demo`` и ``User.is_test_persona`` появились пустыми (DRF-2420):
миграция не знает, какие салоны демонстрационные, а какие личности — для
показа. Знает это файл сида (``services/seeds/demo_salons_2026-08.json``) и
владелец. Поэтому пометка — отдельный шаг, и запускает его владелец.

Почему слаги из файла, а правило читает поле
-------------------------------------------
Слаги годятся ровно для РАЗОВОГО проставления: список устареет на шестом
салоне, и код, читающий список имён, соврёт в тот же день. Код читает признак
(``users.sellable.demo_visibility_q``), а список живёт здесь, в команде, где он
и является тем, чем является, — способом найти уже созданные строки.

Сухой прогон — поведение по умолчанию
-------------------------------------
Без ``--apply`` не пишется ничего. Печатается, что изменится: сколько тенантов
станут демонстрационными (и какие слаги при этом НЕ найдены в базе), сколько
личностей станут тестовыми, и сколько из них уже помечены. Тем же проходом
считаются обе стороны, чтобы отчёт и запись не разошлись.

Личность владельца
------------------
Пометить её обязательно (условие тикета): непомеченная личность идёт путём
клиента и видит пустой продукт — боевой салон пилота ведёт только тело и
массаж. То же для съёмки эталонов (DRF-2413): снимать надо личностью, которая
демо видит. Кого помечать, команда не угадывает: личности называются
``--persona`` по username или id, и слово владельца здесь единственный
источник.

Идемпотентность
---------------
Повторный запуск ничего не меняет: пишутся только строки, у которых признак
ещё не стоит, и в отчёте они видны отдельно от уже помеченных.

Использование::

    manage.py mark_demo_and_test_personas                       # сухой прогон
    manage.py mark_demo_and_test_personas --apply               # записать
    manage.py mark_demo_and_test_personas --persona owner --persona 41f0…
    manage.py mark_demo_and_test_personas --slug olhovyy-dvor   # сузить салоны
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from tenants.models import Tenant
from users.models import User

DEFAULT_SEED = (
    Path(__file__).resolve().parents[3] / "services" / "seeds" / "demo_salons_2026-08.json"
)

#: Боевой пилот. Тот же запрет, что у сида: настоящий салон с настоящими
#: записями настоящих людей не может быть помечен демонстрационным.
PROTECTED_SLUGS = frozenset({"formula-tela"})


@dataclass
class Plan:
    """Что произойдёт. Читается и сухим прогоном, и записью — одними полями."""

    salons_to_mark: list[str] = field(default_factory=list)
    salons_already: list[str] = field(default_factory=list)
    salons_absent: list[str] = field(default_factory=list)
    personas_to_mark: list[str] = field(default_factory=list)
    personas_already: list[str] = field(default_factory=list)
    personas_absent: list[str] = field(default_factory=list)


class Command(BaseCommand):
    help = (
        "Проставить Tenant.is_demo демонстрационным салонам и "
        "User.is_test_persona названным личностям. Без --apply — сухой прогон."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--apply", action="store_true",
            help="записать; без него команда ничего не меняет",
        )
        parser.add_argument(
            "--slug", action="append", default=[],
            help="слаг салона; можно повторять. По умолчанию — слаги файла сида",
        )
        parser.add_argument(
            "--persona", action="append", default=[],
            help="username или id личности, которая должна видеть демо; можно повторять",
        )
        parser.add_argument(
            "--file", default=str(DEFAULT_SEED),
            help="файл сида, из которого берутся слаги демо-салонов",
        )

    def handle(self, *args, **options) -> None:
        slugs = list(dict.fromkeys(options["slug"])) or self._seed_slugs(options["file"])
        protected = sorted(set(slugs) & PROTECTED_SLUGS)
        if protected:
            self._fail(
                f"среди слагов боевой салон: {', '.join(protected)}. "
                "Пометить его демонстрационным нельзя — там настоящие записи."
            )
        personas = list(dict.fromkeys(options["persona"]))

        plan = self._classify(slugs, personas)
        self._report(plan, apply=options["apply"])
        if not options["apply"]:
            self.stdout.write("сухой прогон: ничего не записано (--apply запишет)")
            return
        written = self._write(plan)
        self.stdout.write(
            f"записано: салонов {written['salons']}, личностей {written['personas']}"
        )

    # -- разбор -----------------------------------------------------------

    def _seed_slugs(self, path: str) -> list[str]:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            self._fail(f"файл сида не найден: {path}")
        except json.JSONDecodeError as exc:
            self._fail(f"файл сида не читается как JSON: {exc}")
        slugs = [
            salon.get("slug") for salon in payload.get("salons", [])
            if salon.get("slug")
        ]
        if not slugs:
            self._fail(f"в файле сида нет слагов салонов: {path}")
        return list(dict.fromkeys(slugs))

    def _classify(self, slugs: list[str], personas: list[str]) -> Plan:
        plan = Plan()

        # `all_objects`, а не `objects`: менеджер по умолчанию прячет
        # выключенные тенанты, а пометить надо и такие — демо могло остаться
        # за замками сида.
        rows = {
            row.slug: row.is_demo
            for row in Tenant.all_objects.filter(slug__in=slugs).only("slug", "is_demo")
        }
        for slug in slugs:
            if slug not in rows:
                plan.salons_absent.append(slug)
            elif rows[slug]:
                plan.salons_already.append(slug)
            else:
                plan.salons_to_mark.append(slug)

        for name in personas:
            found = list(
                User.objects.filter(self._persona_q(name)).only("pk", "is_test_persona")
            )
            if not found:
                plan.personas_absent.append(name)
                continue
            if len(found) > 1:
                self._fail(
                    f"личность «{name}» называет больше одной строки ({len(found)}) — "
                    "уточните id"
                )
            if found[0].is_test_persona:
                plan.personas_already.append(name)
            else:
                plan.personas_to_mark.append(name)
        return plan

    @staticmethod
    def _persona_q(name: str) -> Q:
        """Личность называется username или id. Никаких догадок по имени."""
        import uuid

        try:
            return Q(pk=uuid.UUID(str(name)))
        except (ValueError, AttributeError, TypeError):
            return Q(username=name)

    # -- отчёт и запись ---------------------------------------------------

    def _report(self, plan: Plan, *, apply: bool) -> None:
        head = "ЗАПИСЬ" if apply else "СУХОЙ ПРОГОН"
        self.stdout.write(f"=== {head} ===")
        self.stdout.write(
            f"салоны: пометить {len(plan.salons_to_mark)}, "
            f"уже помечены {len(plan.salons_already)}, "
            f"не найдены {len(plan.salons_absent)}"
        )
        for slug in plan.salons_to_mark:
            self.stdout.write(f"  + демонстрационным: {slug}")
        for slug in plan.salons_absent:
            self.stdout.write(f"  ? слага нет в базе: {slug}")
        self.stdout.write(
            f"личности: пометить {len(plan.personas_to_mark)}, "
            f"уже помечены {len(plan.personas_already)}, "
            f"не найдены {len(plan.personas_absent)}"
        )
        for name in plan.personas_to_mark:
            self.stdout.write(f"  + тестовой: {name}")
        for name in plan.personas_absent:
            self.stdout.write(f"  ? личности нет в базе: {name}")
        if not plan.personas_to_mark and not plan.personas_already:
            # Не отказ: салоны пометить можно и отдельно. Но сказать надо, иначе
            # владелец пойдёт путём клиента и увидит пустой продукт.
            self.stdout.write(
                "  ВНИМАНИЕ: тестовых личностей не названо — демо не увидит НИКТО, "
                "включая владельца и съёмку эталонов (--persona)"
            )

    def _write(self, plan: Plan) -> dict[str, int]:
        with transaction.atomic():
            salons = 0
            if plan.salons_to_mark:
                salons = (
                    Tenant.all_objects
                    .filter(slug__in=plan.salons_to_mark, is_demo=False)
                    .update(is_demo=True)
                )
            personas = 0
            for name in plan.personas_to_mark:
                personas += (
                    User.objects
                    .filter(self._persona_q(name), is_test_persona=False)
                    .update(is_test_persona=True)
                )
        return {"salons": salons, "personas": personas}

    def _fail(self, message: str) -> NoReturn:
        raise CommandError(message)
