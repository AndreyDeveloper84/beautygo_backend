"""Состояние поверхности — одним прогоном, с названным предметом (DRF-1661).

Зачем команда, а не документ
----------------------------

Владелец: «ты опять меряешь, и так было много раз; почему текущее
состояние поверхности не хранить в документе». Он прав: за сутки одни
и те же числа снимались по ssh по три-четыре раза.

Но рукописный документ протухает **молча**. 11.09.2026 это стоило целого
замера на чужой машине: запись «пилот — 194.87.99.126» была верна в
августе, а в сентябре по тому же пути, с той же учёткой и тем же compose
отвечала брошенная копия. Документ не знает, что устарел; команда
каждый раз спрашивает машину заново.

Поэтому: команда печатает числа, флаг ``--write`` кладёт их в файл, и
файл — это вывод команды, а не текст, который кто-то правит.

Поэтому же файл **не отслеживается**: путь по умолчанию —
``docs/generated/SURFACE_STATE.md`` (``docs/generated/`` в ``.gitignore``).
Отслеживаемый файл, перезаписанный шагом выкладки в рабочем дереве,
уронил бы следующий ``git checkout`` — «local changes would be
overwritten» — ещё до миграций (находка по бот-стороне, 11.09.2026).

Три правила вывода
------------------

1. **Шапка предмета первой.** Число без названного хоста, контейнера и
   пульса не существует: его нельзя отличить от числа с замороженной
   копии. Шапку даёт ``core/measurement_subject.py`` (взят с ветки
   PR #330, коммит 849e72d, без правок).

2. **Рядом с числом — таблица и поле, из которых оно снято.** 11.09
   главное окно доложило «координатных колонок нет», потому что шаблон
   поиска не увидел ``location_lat``, — а их четыре. Подпись «с
   координатами» читатель проверить не может, ``users.SpecialistProfile
   .location_lat IS NOT NULL`` — может.

3. **Названный предел.** Команда показывает **данные, а не поведение**:
   «обед показывается свободным» отсюда не видно, видно только
   ``break_start IS NULL``. Без этой строки файл прочтут как отчёт о
   готовности.

Счёт идёт через ``_base_manager``: менеджер по умолчанию бывает
отфильтрован (``Tenant.objects`` прячет ``is_active=False``), и тогда
«всего» означало бы «видимых», а не «в таблице». Где фильтр важен, он
назван в подписи отдельной строкой.
"""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.exceptions import FieldDoesNotExist
from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from core.measurement_subject import gather_pulse, subject_lines

#: Строка, которой открывается записанный файл. Не «сгенерировано
#: автоматически» — это читается как техническая мелочь. Здесь названо
#: последствие правки руками.
FILE_HEADER = (
    "<!-- файл перезаписывается командой `manage.py surface_state --write`; "
    "правка руками превращает замер в мнение -->"
)

LIMIT_LINES = (
    "== ПРЕДЕЛ: что эта команда НЕ показывает ==",
    "Команда показывает ДАННЫЕ, а не ПОВЕДЕНИЕ. «Обед показывается",
    "свободным» отсюда не видно — видно только break_start IS NULL. Ноль в",
    "любой строке ниже — факт о таблице, а не вывод о готовности контура.",
)

#: Рубильники каталога, чьё значение решает «ЗАПЕРТО или нет» по §138
#: (построено, но false — ЗАПЕРТО). Имя setting == имя переменной
#: окружения для каждого (base.py читает os.environ.get(<то же имя>)).
#: GOAL_RESOLUTION_ENABLED здесь, а не в боте: бот-команда печатает его
#: строкой «не setting бота — снимать каталогом».
FLAGS = (
    "GOAL_RESOLUTION_ENABLED",
    "GOAL_ANKETA_ENABLED",
    "CROSS_DOMAIN_ENABLED",
    "EXTERNAL_BUSY_ENABLED",
    "BOOKING_AUTO_COMPLETE_ENABLED",
    "SMS_ENABLED",
)
_FLAG_W = max(len(n) for n in FLAGS) + 2

#: Ширина колонки с подписью числа. Одна на всю таблицу, чтобы источник
#: (таблица.поле) начинался в одной позиции и читался столбцом.
_LABEL_W = 24
_VALUE_W = 8


def _row(label: str, value, source: str, *, label_w: int = _LABEL_W) -> str:
    """Одна строка вывода: подпись · число · откуда снято."""
    return f"  {label:<{label_w}}: {str(value):>{_VALUE_W}}   {source}"


def _field_exists(model, name: str) -> bool:
    try:
        model._meta.get_field(name)
    except FieldDoesNotExist:
        return False
    return True


def _by_value(qs, field: str, choices) -> list[tuple[str, int]]:
    """Распределение по значениям поля — ВСЕ значения, что есть в базе.

    Сначала объявленные варианты (в порядке объявления, включая нули: ноль
    — тоже факт), затем всё, чего в объявлении нет. Данные переживают
    код: значение, снятое из choices год назад, в таблице остаётся, и
    печать «только известного» спрятала бы его в разницу между суммой
    и «всего».
    """
    found = dict(
        qs.values_list(field).annotate(n=Count("pk")).values_list(field, "n")
    )
    declared = [c[0] for c in choices]
    rows = [(str(v), found.pop(v, 0)) for v in declared]
    rows += [(f"{v!r} (вне choices)", n) for v, n in sorted(found.items(), key=str)]
    return rows


class Command(BaseCommand):
    help = (
        "Печатает состояние поверхности каталога: тенанты, мастера, часы, "
        "услуги, записи, питание, цели — с шапкой предмета и источником "
        "каждого числа."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--write",
            metavar="PATH",
            default=None,
            help=(
                "Перезаписать файл выводом команды (обычно "
                "docs/generated/SURFACE_STATE.md — путь игнорируется git: "
                "это замер, а не исходник). Файл целиком заменяется; "
                "заголовок предупреждает о правке руками."
            ),
        )
        parser.add_argument(
            "--recent-days",
            type=int,
            default=30,
            help="Окно для строки «записей за N дней» (по умолчанию 30).",
        )

    def handle(self, *args, **options) -> None:
        now = timezone.now()
        lines: list[str] = []

        # Предмет — первым и до всякого счёта. Пульс собирается один раз:
        # напечатанный возраст и есть тот, по которому судят.
        pulses = gather_pulse()
        lines.extend(subject_lines(pulses=pulses, now=now))
        lines.append(f"время снятия               : {now.isoformat(timespec='seconds')}")
        lines.append("")
        lines.extend(LIMIT_LINES)
        lines.append("")
        lines.extend(self._flags())
        lines.append("")
        lines.append("== СОСТОЯНИЕ ПОВЕРХНОСТИ ==")
        lines.append(
            f"  {'':<{_LABEL_W}}  {'число':>{_VALUE_W}}   откуда снято "
            "(таблица.поле; счёт через _base_manager, без фильтров менеджера)"
        )
        lines.extend(self._tenants())
        lines.extend(self._specialists())
        lines.extend(self._working_hours())
        lines.extend(self._services())
        lines.extend(self._appointments(now, options["recent_days"]))
        lines.extend(self._nutrition())
        lines.extend(self._goals())

        text = "\n".join(lines)
        self.stdout.write(text)

        if options["write"]:
            path = Path(options["write"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                FILE_HEADER + "\n\n# Состояние поверхности\n\n```\n" + text + "\n```\n",
                encoding="utf-8",
            )
            self.stdout.write("")
            self.stdout.write(f"записано: {path}")

    # -- рубильники ---------------------------------------------------------

    def _flags(self) -> list[str]:
        """Живое значение каждого флага и откуда оно: env или умолчание кода.

        Умолчание в коде и явное false в .env — разные новости, и §138
        различает ИСПОЛНЕНО от ЗАПЕРТО ровно значением; поэтому печатается
        и значение, и то, задан ли флаг в окружении.
        """
        out = ["== РУБИЛЬНИКИ (§138: построено, но false — ЗАПЕРТО) =="]
        for name in FLAGS:
            if not hasattr(settings, name):
                # Флаг исчез из настроек — это новость, а не «выключен».
                out.append(_row(name, "нет setting", f"settings.{name} отсутствует", label_w=_FLAG_W))
                continue
            value = bool(getattr(settings, name))
            mark = "открыт" if value else "ЗАПЕРТО"
            origin = (
                f"env {name}={os.environ[name]!r}" if name in os.environ
                else "env не задан → умолчание кода"
            )
            out.append(_row(name, mark, f"settings.{name} = {value}; {origin}", label_w=_FLAG_W))
        return out

    # -- разделы --------------------------------------------------------------
    #
    # Каждый раздел — список строк, и каждая строка несёт источник. Если
    # поле ещё не существует (DRF-1662 добавляет координаты тенанта),
    # печатается не ноль, а строка про отсутствие поля: ноль читается как
    # «никого не геокодировали», отсутствие поля — как «геокодировать
    # ещё некуда».

    def _tenants(self) -> list[str]:
        Tenant = apps.get_model("tenants.Tenant")
        qs = Tenant._base_manager.all()
        out = ["тенанты"]
        out.append(_row("всего", qs.count(), "tenants.Tenant (все строки, включая is_active=false)"))
        out.append(_row("активных", qs.filter(is_active=True).count(), "tenants.Tenant.is_active = true"))
        out.append(_row("с адресом", qs.exclude(address="").count(), "tenants.Tenant.address <> ''"))
        out.append(_row("с городом", qs.exclude(city="").count(), "tenants.Tenant.city <> ''"))
        # §139 / #333: координаты тенанта считаются ЕДИНСТВЕННЫМ правилом
        # `Tenant.is_geocoded` (статус ok/confirmed И обе координаты И не
        # 0/0), а не запросом «lat IS NOT NULL»: тот вернул бы в счёт
        # `pending` и `ambiguous`, что §139 запрещает дословно. Тенантов
        # мало — обход строк дешевле, чем своя копия условий в SQL.
        if hasattr(Tenant, "is_geocoded") and _field_exists(Tenant, "geocode_status"):
            n = sum(1 for t in qs.only("latitude", "longitude", "geocode_status") if t.is_geocoded)
            out.append(_row(
                "с координатами", n,
                "tenants.Tenant.is_geocoded (geocode_status ∈ ok/confirmed И latitude, longitude И не 0/0)",
            ))
            for value, k in _by_value(qs, "geocode_status", Tenant._meta.get_field("geocode_status").choices):
                out.append(_row(f"  {value}", k, f"tenants.Tenant.geocode_status = {value.split(' ')[0]}"))
        else:
            # Поле исчезло — это новость, а не ноль: ноль читался бы как
            # «никого не геокодировали», отсутствие поля — «геокодировать
            # некуда».
            out.append(_row(
                "с координатами", "нет поля",
                "tenants.Tenant.geocode_status / is_geocoded отсутствуют (§139, #333)",
            ))
        return out

    def _specialists(self) -> list[str]:
        Specialist = apps.get_model("users.SpecialistProfile")
        qs = Specialist._base_manager.all()
        out = ["специалисты"]
        out.append(_row("всего", qs.count(), "users.SpecialistProfile (все статусы)"))
        out.append(_row("с адресом", qs.exclude(address="").count(), "users.SpecialistProfile.address <> ''"))
        out.append(_row(
            "с координатами",
            qs.filter(location_lat__isnull=False, location_lng__isnull=False).count(),
            "users.SpecialistProfile.location_lat, location_lng IS NOT NULL",
        ))
        return out

    def _working_hours(self) -> list[str]:
        Hours = apps.get_model("appointments.SpecialistWorkingHours")
        qs = Hours._base_manager.all()
        out = ["рабочие часы"]
        out.append(_row("всего", qs.count(), "appointments.SpecialistWorkingHours (строка = мастер × день недели)"))
        out.append(_row(
            "рабочих дней", qs.filter(is_working_day=True).count(),
            "appointments.SpecialistWorkingHours.is_working_day = true",
        ))
        out.append(_row(
            "с перерывом",
            qs.filter(break_start__isnull=False, break_end__isnull=False).count(),
            "appointments.SpecialistWorkingHours.break_start, break_end IS NOT NULL",
        ))
        out.append(_row(
            "мастеров", qs.values("specialist_id").distinct().count(),
            "appointments.SpecialistWorkingHours.specialist_id (distinct)",
        ))
        return out

    def _services(self) -> list[str]:
        SalonService = apps.get_model("services.SalonService")
        qs = SalonService._base_manager.all()
        out = ["услуги"]
        total = qs.count()
        out.append(_row("SalonService всего", total, "services.SalonService (включая is_active=false)"))
        # Все значения статуса, включая те, которых в choices уже нет:
        # сумма строк обязана сходиться с «всего», и расхождение видно.
        for value, n in _by_value(qs, "mapping_status", SalonService.MappingStatus.choices):
            out.append(_row(f"  {value}", n, f"services.SalonService.mapping_status = {value.split(' ')[0]}"))
        return out

    def _appointments(self, now, recent_days: int) -> list[str]:
        Appointment = apps.get_model("appointments.Appointment")
        qs = Appointment._base_manager.all()
        out = ["записи"]
        out.append(_row("всего", qs.count(), "appointments.Appointment (все статусы)"))
        since = now - timedelta(days=recent_days)
        out.append(_row(
            f"за {recent_days} дней",
            qs.filter(created_at__gte=since).count(),
            f"appointments.Appointment.created_at >= {since.date().isoformat()}",
        ))
        return out

    def _nutrition(self) -> list[str]:
        Profile = apps.get_model("nutrition.NutritionProfile")
        FoodLog = apps.get_model("nutrition.FoodLog")
        profiles = Profile._base_manager.all()
        logs = FoodLog._base_manager.all()
        out = ["питание"]
        out.append(_row("профилей", profiles.count(), "nutrition.NutritionProfile"))
        for value, n in _by_value(profiles, "targets_source", Profile.TargetsSource.choices):
            out.append(_row(
                f"  {value}", n,
                f"nutrition.NutritionProfile.targets_source = {value.split(' ')[0]}",
            ))
        out.append(_row("записей еды", logs.count(), "nutrition.FoodLog"))
        out.append(_row(
            "людей с записями еды", logs.values("user_id").distinct().count(),
            "nutrition.FoodLog.user_id (distinct)",
        ))
        return out

    def _goals(self) -> list[str]:
        Goal = apps.get_model("goals.ClientGoal")
        qs = Goal._base_manager.all()
        out = ["цели"]
        out.append(_row("активных", qs.filter(is_active=True).count(), "goals.ClientGoal.is_active = true"))
        out.append(_row("закрытых", qs.filter(is_active=False).count(), "goals.ClientGoal.is_active = false"))
        out.append(_row(
            "людей с целью", qs.values("client_id").distinct().count(),
            "goals.ClientGoal.client_id (distinct, активные и закрытые)",
        ))
        return out
