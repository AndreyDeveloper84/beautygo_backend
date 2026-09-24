"""DRF-2407 — срез сухого прогона: числа по нужным строкам, а не по всему салону.

Лист просит числа по **44 услугам из YClients без привязки**: сколько
привязывается, сколько нет и **почему** — по группам причин. Инструмент для
этого уже есть (`map_salon_services`, dry-run, правила по умолчанию выключены,
`--apply` отказывает), не хватало двух вещей: **среза** и **времени снятия**.

Узлы держат:

* срез сужает строки до нужных, и печатаются **оба** числа — в срезе и у
  салона: «44 не привязались» и «44 из 232 не привязались» читаются по-разному;
* **опечатка в срезе — отказ, а не пустой результат.** `--status unmaped` дал
  бы ноль строк, и ноль прочитался бы как «нечего привязывать». Ноль от
  написания и ноль от предмета постфактум по выводу не отличить;
* решение резолвера от среза **не зависит**: сужение идёт после прогона, и
  строка получает то же решение, что получила бы без среза;
* область и время печатаются **до** чисел;
* прогон со срезом по-прежнему **ничего не пишет**.

Чего узлы не держат: сами правила сопоставления. Резолвер под желаемое число не
подкручивается — если он привязывает мало, это факт о каноне и синонимах.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from services.models import SalonService, ServiceCategory, ServiceTemplate
from tenants.models import Tenant

pytestmark = pytest.mark.django_db


def _cat(name):
    return ServiceCategory.objects.get_or_create(name=name)[0]


def _tpl(name, code, cat_name):
    return ServiceTemplate.objects.create(
        category=_cat(cat_name),
        name=name,
        name_short=name[:40],
        canonical_code=code,
        lifecycle="approved",
        requires_health_check=False,
        approved_rule="test",
        approval_rule_version="1",
        approved_at=timezone.now(),
        approval_source_ref="fixture",
    )


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="ft-slice-2407", name="Формула тела (срез)")


@pytest.fixture
def canon(db):
    return _tpl("Массаж спины", "1.1.4", "Базовый ручной массаж")


def _svc(tenant, name, **kw):
    return SalonService.objects.create(
        tenant=tenant,
        category=_cat("Базовый ручной массаж"),
        name=name,
        duration_minutes=60,
        **kw,
    )


def _run(tenant, **options) -> str:
    out = StringIO()
    call_command("map_salon_services", tenant=tenant.slug, stdout=out, stderr=StringIO(), **options)
    return out.getvalue()


class TestTheSliceNarrowsAndSaysSo:
    def test_it_reports_both_numbers(self, tenant, canon) -> None:
        """«44 не привязались» и «44 из 232» читаются по-разному."""
        _svc(tenant, "Массаж спины", source=SalonService.Source.YCLIENTS)
        _svc(tenant, "Массаж стоп", source=SalonService.Source.SEED)
        _svc(tenant, "Массаж головы", source=SalonService.Source.MANUAL)

        report = _run(tenant, source="yclients")

        assert "строк в срезе: 1 из 3 у салона" in report
        assert "срез: источник yclients" in report

    def test_without_a_slice_the_whole_salon_is_counted(self, tenant, canon) -> None:
        """Положительная пара: без среза считается весь салон."""
        _svc(tenant, "Массаж спины", source=SalonService.Source.YCLIENTS)
        _svc(tenant, "Массаж стоп", source=SalonService.Source.SEED)

        report = _run(tenant)

        assert "срез: весь салон" in report
        assert "строк в срезе: 2 из 2 у салона" in report

    def test_status_and_source_narrow_together(self, tenant, canon) -> None:
        _svc(
            tenant,
            "Массаж спины",
            source=SalonService.Source.YCLIENTS,
            mapping_status=SalonService.MappingStatus.UNMAPPED,
        )
        _svc(
            tenant,
            "Массаж стоп",
            source=SalonService.Source.YCLIENTS,
            mapping_status=SalonService.MappingStatus.REVIEW_REQUIRED,
        )

        report = _run(tenant, source="yclients", status="unmapped")

        assert "строк в срезе: 1 из 2 у салона" in report


class TestATypoIsRefusedNotCountedAsZero:
    def test_unknown_status_names_the_known_ones(self, tenant, canon) -> None:
        """Ноль от написания нельзя отличить от нуля по предмету — поэтому отказ.

        Живой случай этого класса: запрос со стенда вернул ноль, потому что в
        нём стояло `'ACTIVE'`, а в базе `'active'`. Ноль прочитали как факт.
        """
        _svc(tenant, "Массаж спины", source=SalonService.Source.YCLIENTS)

        with pytest.raises(CommandError) as exc:
            _run(tenant, status="unmaped")

        assert "unmaped" in str(exc.value)
        assert "unmapped" in str(exc.value)  # известные перечислены

    def test_unknown_source_names_the_known_ones(self, tenant, canon) -> None:
        _svc(tenant, "Массаж спины", source=SalonService.Source.YCLIENTS)

        with pytest.raises(CommandError) as exc:
            _run(tenant, source="yclient")

        assert "yclient" in str(exc.value)
        assert "yclients" in str(exc.value)


class TestTheSliceDoesNotChangeTheVerdict:
    def test_a_row_keeps_the_decision_it_had_without_the_slice(self, tenant, canon) -> None:
        """Резолвер видит салон целиком; срез — только про то, что печатать.

        Иначе отчёт отвечал бы на вопрос «что было бы, будь в салоне только эти
        строки», а спрашивают другое.
        """
        _svc(tenant, "Массаж спины", source=SalonService.Source.YCLIENTS)
        _svc(tenant, "Массаж стоп", source=SalonService.Source.SEED)

        whole = _run(tenant)
        sliced = _run(tenant, source="yclients")

        def verdict_of(report: str, name: str) -> str:
            line = next(ln for ln in report.splitlines() if ln.rstrip().endswith(name))
            return line.split()[0]

        assert verdict_of(sliced, "Массаж спины") == verdict_of(whole, "Массаж спины")


class TestScopeFirstAndNothingWritten:
    def test_time_and_scope_precede_the_numbers(self, tenant, canon) -> None:
        _svc(tenant, "Массаж спины", source=SalonService.Source.YCLIENTS)

        report = _run(tenant, source="yclients")
        head = report.split("\n\n")[0]

        assert "снято:" in head
        assert "предмет: база" in head
        assert "режим: dry-run, записи нет" in head
        assert "срез:" in head

    def test_the_sliced_run_writes_nothing(self, tenant, canon) -> None:
        """Обещание «только чтение» доказывается счётчиком, а не докстрокой."""
        _svc(tenant, "Массаж спины", source=SalonService.Source.YCLIENTS)

        with CaptureQueriesContext(connection) as queries:
            _run(tenant, source="yclients")

        wrote = [
            q["sql"]
            for q in queries.captured_queries
            if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        assert queries.captured_queries, "положительная пара: прогон вообще ходил в базу"
        assert wrote == [], wrote


class TestAnEmptySliceIsRefusedToo:
    """Зеркальный случай опечатки, и он опаснее: отбор не применён, а заголовок
    говорит «срез». Тогда 232 записывают в ответ на вопрос «сколько из 44».

    Живой путь к нему — не выдумка: `--status "$ST"` с незаданной переменной
    или запятая, оставшаяся от склейки списка в скрипте.
    """

    @pytest.mark.parametrize("spec", [",", " , ", ",,"])
    def test_a_spec_naming_nothing_is_refused(self, tenant, canon, spec) -> None:
        _svc(tenant, "Массаж спины", source=SalonService.Source.YCLIENTS)

        with pytest.raises(CommandError) as exc:
            _run(tenant, status=spec)

        assert "пустой срез" in str(exc.value)

    def test_an_absent_flag_still_means_the_whole_salon(self, tenant, canon) -> None:
        """Положительная пара: пустая строка по умолчанию — не срез, и так и сказано."""
        _svc(tenant, "Массаж спины", source=SalonService.Source.YCLIENTS)

        report = _run(tenant, status="", source="")

        assert "срез: весь салон" in report

    def test_the_header_repeats_only_what_was_applied(self, tenant, canon) -> None:
        """Заголовок собирается из разобранных значений, а не из сырой строки.

        Иначе он повторял бы дубли и пробелы пользователя и мог бы описать
        отбор, которого не было.
        """
        _svc(tenant, "Массаж спины", source=SalonService.Source.YCLIENTS)

        report = _run(tenant, source=" yclients , yclients ")

        assert "срез: источник yclients ·" in report


class TestTheSliceDoesNotLeakIntoSinks:
    def test_store_refuses_to_save_a_partial_report(self, tenant, canon) -> None:
        """Отчёт админки — один файл на салон, и админка читает его как полный.

        Строка, которой в нём нет, показывается как «нет в отчёте», действие
        «подтвердить кандидата» на неё отказывает, а «отметить пробел канона»
        пишет провенанс с пометкой «без отчёта». Срез, записанный сюда, увёл бы
        туда весь салон молча.
        """
        _svc(tenant, "Массаж спины", source=SalonService.Source.YCLIENTS)

        with pytest.raises(CommandError) as exc:
            _run(tenant, source="yclients", store=True)

        assert "--store" in str(exc.value)

    def test_store_without_a_slice_still_works(self, tenant, canon, settings, tmp_path) -> None:
        """Положительная пара: запрет узкий — он про срез, а не про --store."""
        settings.MAPPING_REPORT_DIR = str(tmp_path)
        _svc(tenant, "Массаж спины", source=SalonService.Source.YCLIENTS)

        report = _run(tenant, store=True)

        assert "отчёт последнего прогона" in report

    def test_the_json_carries_its_own_scope(self, tenant, canon, tmp_path) -> None:
        """Файл уходит дальше терминала, где два числа напечатаны рядом."""
        import json

        _svc(tenant, "Массаж спины", source=SalonService.Source.YCLIENTS)
        _svc(tenant, "Массаж стоп", source=SalonService.Source.SEED)
        out = tmp_path / "r.json"

        _run(tenant, source="yclients", out=str(out))

        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["scope"]["slice"] == "источник yclients"
        assert payload["scope"]["rows_in_slice"] == 1
        assert payload["scope"]["rows_in_tenant"] == 2
        assert payload["scope"]["tenant"] == tenant.slug
        # Положительная пара: сводка по-прежнему про срез, а не про салон.
        assert payload["summary"]["total"] == 1


class TestTheSliceIsRefusedOnTheWritePath:
    def test_apply_with_a_slice_is_refused_before_the_gate(self, tenant, canon) -> None:
        """Сегодня ворота закрыты, но в день, когда откроются, срез обманул бы.

        `--apply --status unmapped` записал бы ВЕСЬ салон, пока оператор
        считает, что сузил до 44 строк. Сужение для записи уже есть — `--only`.
        """
        _svc(tenant, "Массаж спины", source=SalonService.Source.YCLIENTS)

        with pytest.raises(CommandError) as exc:
            _run(tenant, source="yclients", apply=True)

        assert "--only" in str(exc.value)

    def test_a_typo_on_the_write_path_is_seen_at_all(self, tenant, canon) -> None:
        """Прежде опечатка на этом пути не рассматривалась вовсе.

        Разбор среза шёл после ветки записи, и `--apply --status unmaped`
        уходил в отказ ворот, ни разу не взглянув на опечатку.
        """
        _svc(tenant, "Массаж спины", source=SalonService.Source.YCLIENTS)

        with pytest.raises(CommandError) as exc:
            _run(tenant, status="unmaped", apply=True)

        assert "unmaped" in str(exc.value)
