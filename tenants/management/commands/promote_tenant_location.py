"""Перенос адреса ОДНОГО салона в `ServiceLocation` (§9, DRF-1687, срез L3).

Почему по одному, и почему это в команде, а не в миграции
--------------------------------------------------------

§9: «автоматический массовый перенос запрещён». Команда без ``--slug`` не
работает вовсе — не «переносит всё по умолчанию», а отказывает с кодом 2.
Десять тенантов с адресом — десять запусков, и у каждого своя строка в
логе с предметом. Это не неудобство, это форма решения.

Что становится статусом, а что нет
----------------------------------

``CONFIRMED`` — только когда подтверждение **названо**: ``--confirm`` вместе
с ``--by`` (кто) и ``--source-ref`` (на каком основании). Без них — только
``REVIEW_REQUIRED``: «неизвестное происхождение → REVIEW_REQUIRED» (§9).
Команда не может знать, что адрес в ``Tenant.address`` подтверждён, — это
знает человек, и он говорит это флагом. Адрес «Формулы тела» (§10) —
``--confirm --by <оператор> --source-ref "ayla-owner-decisions-2026-09-11 §10"``.

``Tenant.address`` остаётся **входом** и не трогается: место — снимок
адреса на момент переноса, дальше у него своя жизнь (геокодирование — L4).

Совпавшие точки — без дублей (§9): если у салона уже есть место с тем же
адресом, второе не создаётся, команда печатает существующее и выходит с
нулём. Сравнение — по нормализованной строке адреса; после геокодирования
дубли ловит уже схема (``servicelocation_point_is_unique``).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.measurement_subject import gather_pulse, subject_lines
from tenants.models import LocationStatus, ServiceLocation, Tenant
from users.models import User


def _same_address(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


class Command(BaseCommand):
    help = "Перенести адрес ОДНОГО салона в ServiceLocation (§9). Без --slug не работает. Сухой прогон по умолчанию."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--slug", default=None, help="Slug салона. Обязателен: массовый перенос запрещён (§9).")
        parser.add_argument("--label", default="", help="Как место называют люди. Пусто — берётся адрес.")
        parser.add_argument("--confirm", action="store_true",
                            help="Создать место сразу CONFIRMED. Требует --by и --source-ref.")
        parser.add_argument("--by", default=None, help="username подтверждающего (для --confirm).")
        parser.add_argument("--source-ref", default="", help="Основание подтверждения (для --confirm).")
        parser.add_argument("--apply", action="store_true", help="Записать. Без флага — сухой прогон.")

    def _fail(self, msg: str) -> None:
        self.stderr.write(self.style.ERROR(msg))
        raise SystemExit(2)

    def handle(self, *args, **options) -> None:
        for line in subject_lines(pulses=gather_pulse()):
            self.stdout.write(line)
        self.stdout.write("")

        slug = options["slug"]
        if not slug:
            self._fail("--slug обязателен: §9 запрещает автоматический массовый перенос. Один салон — один запуск.")

        tenant = Tenant.all_objects.filter(slug=slug).first()
        if tenant is None:
            self._fail(f"салона {slug!r} нет")
        address = (tenant.address or "").strip()
        if not address:
            self._fail(
                f"у салона {slug!r} пустой адрес — переносить нечего "
                "(Tenant.address — вход, заполняется оператором)"
            )

        confirmed_by = None
        if options["confirm"]:
            if not options["by"] or not options["source_ref"]:
                self._fail(
                    "--confirm требует --by <username> и --source-ref <основание>: "
                    "подтверждение без автора и основания — не подтверждение (§9, §1 п. 7)"
                )
            confirmed_by = User.objects.filter(username=options["by"]).first()
            if confirmed_by is None:
                self._fail(f"пользователя {options['by']!r} нет")
        status = LocationStatus.CONFIRMED if options["confirm"] else LocationStatus.REVIEW_REQUIRED

        existing = next(
            (loc for loc in ServiceLocation.objects.filter(tenant=tenant) if _same_address(loc.address, address)),
            None,
        )
        mode = "ЗАПИСЬ (--apply)" if options["apply"] else "СУХОЙ ПРОГОН"
        self.stdout.write(f"== ПЕРЕНОС: салон={slug} · режим={mode} ==")
        self.stdout.write(f"  адрес (Tenant.address) : {address}")
        self.stdout.write(f"  город                  : {tenant.city or '—'}")
        self.stdout.write(f"  статус места           : {status.value}"
                          + (f"  (подтверждает {confirmed_by.username}, основание: {options['source_ref']})"
                             if confirmed_by else "  (происхождение не названо → требует проверки)"))

        if existing is not None:
            self.stdout.write(self.style.WARNING(
                f"  уже есть место с этим адресом: {existing.id} [{existing.status}] — дубль не создаётся (§9)"
            ))
            return

        loc = ServiceLocation(
            tenant=tenant, label=options["label"], address=address, city=tenant.city or "",
            status=status, confirmed_by=confirmed_by,
            confirmed_at=timezone.now() if confirmed_by else None,
            confirmed_source_ref=options["source_ref"] if confirmed_by else "",
        )
        loc.full_clean()
        if options["apply"]:
            loc.save()
            self.stdout.write(self.style.SUCCESS(f"  создано: {loc.id} [{loc.status}]"))
        else:
            self.stdout.write(self.style.WARNING("  сухой прогон — не записано; повторить с --apply"))
