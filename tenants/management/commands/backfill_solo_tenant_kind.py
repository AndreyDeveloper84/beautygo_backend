"""Вид соло-мастерам, заведённым до G4 — по списку из бота (DRF-2325, решение владельца №31).

# Зачем

``Tenant.kind`` появился с G4 (DRF-1828) и пишется ТОЛЬКО provisioning
solo-workspace. Миграция 0007 поставила всем прежним строкам ``salon`` по
умолчанию — и сама G4 назвала это «умолчанием, а не доказательством салона»
(``propose_master_locations``). Последствие измерено в DRF-2254: соло-мастер,
заведённый раньше G4, открывает «Место работы» и получает от каталога отказ
«место ведёт владелец салона», хотя владелец — он сам.

# Почему список, а не префикс слага

Слаг соло-тенанта бота выглядит как ``solo-{канал}-{8 hex}``, но G4 прямо
сказал: имя производной происхождения не доказывает. Доказательство даёт БОТ:
он пересчитывает слаг по личности (``solo_onboarding._solo_tenant_slug`` —
sha256 от ``канал:id``) и отдаёт совпавшие. Эта команда переводит только то,
что названо в списке, и ничего не ищет сама.

Связь бота и каталога здесь — СЛАГ, не UUID: до G4 каталог заводил строку
через ``ensure_tenant(slug=…)`` со своим ключом; общий UUID есть только у
solo-workspace, заведённых самим provisioning.

# Порядок: сначала разбор, потом отчёт, запись — последней

Отчёт печатается ЦЕЛИКОМ до первой записи, а записи идут одной транзакцией.
Иначе падение на сороковом слаге из пятидесяти оставило бы тридцать девять
переведённых строк и НИ ОДНОЙ строки отчёта — владелец не узнал бы, что
именно записано. Порядок тот же, что у соседней команды переноса места
(``promote_tenant_location``: план печатается, запись — в конце).

# Что команда НЕ делает

* **Не пишет без ``--apply``.** Сухой прогон — умолчание: сначала отчёт
  владельцу, потом запуск на стенде.
* **Не переводит тенанта с двумя и более живыми мастерами** (решение главного
  окна): у ``kind=solo`` самообслуживание местом и услугами открыто каждому
  профилю этого тенанта, а не только владельцу (случай B замера DRF-2254).
* **Не переводит тенанта БЕЗ живых мастеров.** Ноль мастеров — признак
  служебного (например, маркетплейсного) тенанта: на этом свойстве
  ``service_location.tenant_has_no_masters`` держит сразу две защиты, а вид
  ``solo`` снимает их обе — ``deletion_executor._erase_own_place`` начинает
  стирать место такого тенанта, а ``personal_data_api._works_at`` начинает
  отдавать адрес организации как личные данные человека. И сам отказ
  DRF-2254 там некому получить: отказ получает мастер, а мастера нет.
* **Не переводит неактивного тенанта и тенанта неизвестного вида.** Оба —
  решение владельца, а не умолчание команды; оба названы в отчёте числом.
* **Не заводит тенантов и не трогает тех, кого нет в списке.**
* **Не печатает людей.** В отчёте — слаги тенантов и числа: ни имён, ни
  логинов, ни идентификаторов.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import NoReturn

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.measurement_subject import gather_pulse, subject_lines
from tenants.models import Tenant
from tenants.service_location import live_master_count

#: Столько живых мастеров уже делают тенант командой, а не соло-workspace.
TEAM_FROM = 2


@dataclass
class Plan:
    """Разбор списка ДО записи: числа отчёта и числа записи — из одного места."""

    lines: int = 0
    slugs: list[str] = field(default_factory=list)
    convert: list[tuple[str, int]] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    team: list[tuple[str, int]] = field(default_factory=list)
    no_masters: list[str] = field(default_factory=list)
    inactive: list[str] = field(default_factory=list)
    unknown_kind: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


class Command(BaseCommand):
    help = (
        "Проставить Tenant.kind=solo соло-мастерам, заведённым до G4, по списку слагов из бота. "
        "Сухой прогон по умолчанию; запись — только с --apply."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--from-file",
            default=None,
            help="Файл со слагами, по одному в строке. Пусто — список читается из stdin.",
        )
        parser.add_argument(
            "--apply", action="store_true", help="Записать. Без флага — сухой прогон."
        )

    def _fail(self, msg: str) -> NoReturn:
        self.stderr.write(self.style.ERROR(msg))
        raise SystemExit(2)

    def _read_list(self, options) -> str:
        path = options["from_file"]
        if path:
            try:
                # utf-8-sig: список, сохранённый блокнотом Windows, несёт BOM,
                # и первый слаг иначе тихо попал бы в «нет в каталоге».
                with open(path, encoding="utf-8-sig") as fh:
                    return fh.read()
            except OSError as exc:
                # Путь — не идентификатор человека; без него опечатка в имени
                # файла неотличима от запрета доступа.
                self._fail(f"не читается файл списка {path!r}: {type(exc).__name__}")
        if sys.stdin.isatty():
            self._fail(
                "список не передан: укажите --from-file или подайте слаги в канал "
                "(команда не спрашивает список у терминала — молчаливое ожидание "
                "ввода на стенде выглядит как зависание)."
            )
        return sys.stdin.read()

    def _slugs(self, raw: str) -> tuple[int, list[str]]:
        """Строки списка и уникальные слаги: отчёт называет оба числа.

        Владелец сверяет отчёт с тем, что отдал бот; одно число «в списке»
        после дедупликации сверить не с чем.
        """
        lines = 0
        seen: list[str] = []
        for line in raw.splitlines():
            slug = line.strip()
            if not slug or slug.startswith("#"):
                continue
            lines += 1
            if slug not in seen:
                seen.append(slug)
        return lines, seen

    def _classify(self, slugs: list[str], lines: int) -> Plan:
        plan = Plan(lines=lines, slugs=slugs)
        for slug in slugs:
            tenant = Tenant.all_objects.filter(slug=slug).first()
            if tenant is None:
                plan.missing.append(slug)
            elif tenant.kind == Tenant.Kind.SOLO:
                plan.already.append(slug)
            elif tenant.kind != Tenant.Kind.SALON:
                plan.unknown_kind.append(slug)
            elif not tenant.is_active:
                plan.inactive.append(slug)
            else:
                masters = live_master_count(tenant)
                if masters >= TEAM_FROM:
                    plan.team.append((slug, masters))
                elif masters == 0:
                    plan.no_masters.append(slug)
                else:
                    plan.convert.append((slug, masters))
        return plan

    def _report(self, plan: Plan, *, apply: bool) -> None:
        mode = "ЗАПИСЬ (--apply)" if apply else "сухой прогон (без --apply ничего не записано)"
        self.stdout.write(f"Режим: {mode}")
        self.stdout.write(f"содержательных строк от бота: {plan.lines}")
        self.stdout.write(f"в списке бота (без повторов): {len(plan.slugs)}")
        self.stdout.write(f"будет переведено: {len(plan.convert)}")
        for slug, masters in plan.convert:
            self.stdout.write(f"  salon → solo, живых мастеров {masters}: {slug}")
        self.stdout.write(f"уже solo: {len(plan.already)}")
        for slug in plan.already:
            self.stdout.write(f"  без изменений: {slug}")
        self.stdout.write(f"с командой ({TEAM_FROM}+ мастера), пропущено: {len(plan.team)}")
        for slug, masters in plan.team:
            self.stdout.write(f"  пропущен, живых мастеров {masters}: {slug}")
        self.stdout.write(f"без живых мастеров, пропущено: {len(plan.no_masters)}")
        for slug in plan.no_masters:
            # Причина названа предикатом, а не догадкой: у тенанта до G4
            # профиль мастера мог не получить tenant при заливке
            # (`backfill_tenants`) или принадлежать удалённому аккаунту —
            # «служебный» было бы неправдой.
            self.stdout.write(
                f"  пропущен, живых мастеров нет (служебный тенант, "
                f"непривязанный профиль или удалённый аккаунт): {slug}"
            )
        self.stdout.write(f"неактивен, пропущено: {len(plan.inactive)}")
        for slug in plan.inactive:
            self.stdout.write(f"  пропущен, is_active=False: {slug}")
        self.stdout.write(f"вид, которого код не знает, пропущено: {len(plan.unknown_kind)}")
        for slug in plan.unknown_kind:
            self.stdout.write(f"  пропущен, kind не salon и не solo: {slug}")
        self.stdout.write(f"нет в каталоге: {len(plan.missing)}")
        for slug in plan.missing:
            self.stdout.write(f"  не найден: {slug}")

    def _write(self, plan: Plan) -> int:
        """Записать разобранное — одной транзакцией, считая РЕАЛЬНЫЕ строки."""
        written = 0
        now = timezone.now()
        with transaction.atomic():
            for slug, _masters in plan.convert:
                # kind=SALON в условии: строку, которую между разбором и записью
                # сменил кто-то другой, мы не перетираем. update() минует
                # auto_now — updated_at пишем явно, иначе у переведённой строки
                # не остаётся следа, по которому её отличают и откатывают.
                written += Tenant.all_objects.filter(slug=slug, kind=Tenant.Kind.SALON).update(
                    kind=Tenant.Kind.SOLO, updated_at=now
                )
        return written

    def handle(self, *args, **options) -> None:
        for line in subject_lines(pulses=gather_pulse()):
            self.stdout.write(line)
        self.stdout.write("")

        lines, slugs = self._slugs(self._read_list(options))
        if not slugs:
            self._fail(
                "список слагов пуст: команда переводит только то, что названо ботом "
                "(--from-file или stdin). Поиска по префиксу слага здесь нет — "
                "имя производной происхождения не доказывает (G4)."
            )

        apply = bool(options["apply"])
        plan = self._classify(slugs, lines)
        self._report(plan, apply=apply)
        if not apply:
            return

        written = self._write(plan)
        self.stdout.write(f"переведено: {written}")
        if written != len(plan.convert):
            # Число записи ниже числа плана — строку сменили между разбором и
            # записью. Молчать здесь значило бы отчитаться о записи, которой нет.
            self.stdout.write(
                self.style.WARNING(
                    f"  не переведено: {len(plan.convert) - written} — "
                    "строка сменилась между разбором и записью, повторите прогон"
                )
            )
