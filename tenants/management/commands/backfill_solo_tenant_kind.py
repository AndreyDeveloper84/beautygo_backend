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

# Что команда НЕ делает

* **Не пишет без ``--apply``.** Сухой прогон — умолчание: сначала отчёт
  владельцу, потом запуск на стенде.
* **Не переводит тенанта с двумя и более живыми мастерами** (решение главного
  окна): у ``kind=solo`` самообслуживание местом и услугами открыто каждому
  профилю этого тенанта, а не только владельцу (случай B замера DRF-2254).
  Такие называются отдельной строкой и числом — что с ними делать, решает
  владелец.
* **Не заводит тенантов и не трогает тех, кого нет в списке.**
* **Не печатает людей.** В отчёте — слаги тенантов и числа: ни имён, ни
  логинов, ни идентификаторов.

Живой мастер — профиль, чей пользователь не удалён: тот же предикат, что у
исполнителя удаления аккаунта (``deletion_executor._other_live_masters``).
"""
from __future__ import annotations

import sys

from django.core.management.base import BaseCommand
from django.db import transaction

from core.measurement_subject import gather_pulse, subject_lines
from tenants.models import Tenant

#: Столько живых мастеров уже делают тенант командой, а не соло-workspace.
TEAM_FROM = 2


def _live_masters(tenant: Tenant) -> int:
    """Мастера тенанта, чей аккаунт не удалён — предикат D3, одним счётом."""
    from users.models import SpecialistProfile

    return SpecialistProfile.objects.filter(
        tenant=tenant, user__deleted_at__isnull=True
    ).count()


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

    def _fail(self, msg: str) -> None:
        self.stderr.write(self.style.ERROR(msg))
        raise SystemExit(2)

    def _slugs(self, options) -> list[str]:
        path = options["from_file"]
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    raw = fh.read()
            except OSError as exc:
                self._fail(f"не читается файл списка: {type(exc).__name__}")
        else:
            raw = sys.stdin.read()
        seen: list[str] = []
        for line in raw.splitlines():
            slug = line.strip()
            if not slug or slug.startswith("#") or slug in seen:
                continue
            seen.append(slug)
        return seen

    def handle(self, *args, **options) -> None:
        for line in subject_lines(pulses=gather_pulse()):
            self.stdout.write(line)
        self.stdout.write("")

        slugs = self._slugs(options)
        if not slugs:
            self._fail(
                "список слагов пуст: команда переводит только то, что названо ботом "
                "(--from-file или stdin). Поиска по префиксу слага здесь нет — "
                "имя производной происхождения не доказывает (G4)."
            )

        apply = bool(options["apply"])
        missing: list[str] = []
        already: list[str] = []
        team: list[tuple[str, int]] = []
        changed: list[str] = []

        for slug in slugs:
            tenant = Tenant.all_objects.filter(slug=slug).first()
            if tenant is None:
                missing.append(slug)
                continue
            if tenant.kind == Tenant.Kind.SOLO:
                already.append(slug)
                continue
            masters = _live_masters(tenant)
            if masters >= TEAM_FROM:
                team.append((slug, masters))
                continue
            changed.append(slug)
            if apply:
                with transaction.atomic():
                    Tenant.all_objects.filter(pk=tenant.pk).update(kind=Tenant.Kind.SOLO)

        mode = "ЗАПИСЬ (--apply)" if apply else "сухой прогон (без --apply ничего не записано)"
        self.stdout.write(f"Режим: {mode}")
        self.stdout.write(f"в списке бота: {len(slugs)}")
        self.stdout.write(f"переведено: {len(changed)}" if apply else f"будет переведено: {len(changed)}")
        for slug in changed:
            self.stdout.write(f"  salon → solo: {slug}")
        self.stdout.write(f"уже solo: {len(already)}")
        for slug in already:
            self.stdout.write(f"  без изменений: {slug}")
        self.stdout.write(f"с командой ({TEAM_FROM}+ мастера), пропущено: {len(team)}")
        for slug, masters in team:
            self.stdout.write(f"  пропущен, живых мастеров {masters}: {slug}")
        self.stdout.write(f"нет в каталоге: {len(missing)}")
        for slug in missing:
            self.stdout.write(f"  не найден: {slug}")
