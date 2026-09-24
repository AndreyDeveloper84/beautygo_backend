"""Отчёт разбора услуг переживает выкладку — или отказывает понятно (DRF-2409).

## Замер (стенд 176.119.159.141, 24.09)

`dev-web-1` пересоздан 09:14:39 UTC; `ls /app/var` → `No such file or
directory`; `/app` принадлежит root, контейнер идёт под uid 1000,
`test -w /app` → not writable. В образе нет `mkdir var`, в compose нет тома
на `/app/var`, `MAPPING_REPORT_DIR` не задан нигде в дереве.

`store_report` каталог создаёт сам (`mkdir(parents=True)`), поэтому дефект
не «каталога нет», а **«создать его некому»**: `/app` пишется только root'ом,
и `mkdir` падает `PermissionError` с голой трассой — ни пути, ни имени
настройки, ни того, что делать.

## Почему узел на ПРАВО записи, а не на существование

Каталог, созданный root, существует и не пишется. Узел, который проверяет
`exists()`, был бы зелёным ровно в том состоянии, которое сломало стенд.
Поэтому здесь каталог создаётся и делается недоступным для записи — и
ожидается **названный** отказ.

## Что доказывает ноль

«Расхождений нет» и «нечего было разбирать» — разные факты. Поэтому
положительный узел смотрит не только на появившийся файл, но и на охват:
`summary.total` в самом отчёте должен быть непустым.
"""

from __future__ import annotations

import json
import os
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from services.mapping import store
from services.models import SalonService, ServiceCategory, ServiceTemplate
from tenants.models import Tenant

pytestmark = pytest.mark.django_db


def _cat(name):
    return ServiceCategory.objects.get_or_create(name=name)[0]


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="ft-report-2409", name="Формула тела (отчёт)")


@pytest.fixture
def canon(db):
    return ServiceTemplate.objects.create(
        category=_cat("Базовый ручной массаж"),
        name="Массаж спины",
        name_short="Массаж спины",
        canonical_code="1.1.4",
        lifecycle="approved",
        requires_health_check=False,
        approved_rule="test",
        approval_rule_version="1",
        approved_at=timezone.now(),
        approval_source_ref="fixture",
    )


def _svc(tenant, name):
    return SalonService.objects.create(
        tenant=tenant,
        category=_cat("Базовый ручной массаж"),
        name=name,
        duration_minutes=60,
        source=SalonService.Source.YCLIENTS,
    )


def _run(tenant, **options) -> str:
    out = StringIO()
    call_command("map_salon_services", tenant=tenant.slug, stdout=out, stderr=StringIO(), **options)
    return out.getvalue()


class TestAnUnwritableDirectoryRefusesByName:
    def test_mkdir_refused_names_the_path_and_the_setting(
        self, tenant, canon, settings, tmp_path, monkeypatch
    ) -> None:
        """Подмена прав: создать каталог нельзя. Отказ обязан быть названным.

        Именно это и случилось на стенде: `/app` пишется только root'ом.
        Голая `PermissionError` не говорит ни где писать, ни чем это
        настраивается.
        """
        settings.MAPPING_REPORT_DIR = str(tmp_path / "var" / "mapping_reports")
        _svc(tenant, "Массаж спины")

        def _refuse(self, *a, **kw):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr("pathlib.Path.mkdir", _refuse)

        with pytest.raises(CommandError) as exc:
            _run(tenant, store=True)

        message = str(exc.value)
        assert str(tmp_path / "var" / "mapping_reports") in message
        assert "MAPPING_REPORT_DIR" in message

    @pytest.mark.skipif(os.name == "nt", reason="права каталога на Windows не снимаются chmod")
    def test_a_directory_we_do_not_own_refuses_by_name(
        self, tenant, canon, settings, tmp_path
    ) -> None:
        """Каталог ЕСТЬ и не пишется — состояние стенда один в один.

        Узел на существование здесь был бы зелёным: каталог на месте.
        """
        reports = tmp_path / "mapping_reports"
        reports.mkdir()
        reports.chmod(0o555)
        settings.MAPPING_REPORT_DIR = str(reports)
        _svc(tenant, "Массаж спины")

        try:
            with pytest.raises(CommandError) as exc:
                _run(tenant, store=True)
        finally:
            reports.chmod(0o755)

        assert str(reports) in str(exc.value)
        assert "MAPPING_REPORT_DIR" in str(exc.value)


class TestWhenWeMayWriteTheReportAppears:
    def test_the_report_lands_on_the_path_the_command_itself_uses(
        self, tenant, canon, settings, tmp_path
    ) -> None:
        """Обратная сторона узла: умеем не только краснеть, но и работать.

        Путь берётся у самого хранилища (`store.report_path`), а не
        собирается в тесте — иначе доказали бы согласие теста с собой.
        """
        settings.MAPPING_REPORT_DIR = str(tmp_path / "var" / "mapping_reports")
        _svc(tenant, "Массаж спины")
        _svc(tenant, "Массаж стоп")

        _run(tenant, store=True)

        path = store.report_path(tenant.slug)
        assert path.exists(), path
        payload = json.loads(path.read_text(encoding="utf-8"))
        # Ноль расхождений доказывается охватом: сколько строк разобрано.
        assert payload["summary"]["total"] == 2

    def test_a_missing_parent_is_created_not_refused(
        self, tenant, canon, settings, tmp_path
    ) -> None:
        """Каталога нет, но родитель пишется — это рабочий случай, не отказ."""
        settings.MAPPING_REPORT_DIR = str(tmp_path / "deep" / "var" / "mapping_reports")
        _svc(tenant, "Массаж спины")

        _run(tenant, store=True)

        assert store.report_path(tenant.slug).exists()
