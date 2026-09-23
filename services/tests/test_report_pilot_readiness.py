"""Читающий отчёт готовности пилота: считает, ничего не пишет (DRF-2361, DRF-2358).

Узлы держат три свойства, за которые команду и пустят на стенд:

* **числа верные** — по группам, с разделением «категории нет» и «категория
  есть, но целью не названа»: сливать их нельзя, это разные причины;
* **ничего не пишется** — проверяется не обещанием в докстроке, а счётчиком
  запросов: ни одного ``INSERT``/``UPDATE``/``DELETE`` за весь прогон;
* **людей в выводе нет** — ни имени, ни логина, ни ссылки на фото.
"""

from __future__ import annotations

import itertools
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from services.models import SalonService, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User


@pytest.fixture
def salon(db) -> Tenant:
    return Tenant.objects.create(slug="report-2361", name="Салон отчёта")


_SEQ = itertools.count(1)


def _category(tenant: Tenant, name: str | None = None) -> ServiceCategory:
    """Категория с уникальным именем: имя категории уникально на всю базу."""
    return ServiceCategory.objects.create(tenant=tenant, name=name or f"Категория {next(_SEQ)}")


def _service(tenant: Tenant, *, category=None, active=True, status="unmapped") -> SalonService:
    """Услуга салона; без категории — как СТАРАЯ строка, а не как новая.

    Схема требует категорию у услуги вне таксономии (``clean``), поэтому
    строку без категории сегодня создать нельзя — но в живой базе такие
    строки есть, они старше проверки. Здесь это воспроизводится прямым
    ``update``: иначе узел мерил бы состояние, которого в жизни не бывает,
    и группа «категории нет вовсе» осталась бы непроверенной.
    """
    service = SalonService.objects.create(
        tenant=tenant,
        name="услуга",
        category=category or _category(tenant),
        is_active=active,
        mapping_status=status,
        duration_minutes=30,
        base_price=1000,
    )
    if category is None:
        SalonService.objects.filter(pk=service.pk).update(category=None)
        service.refresh_from_db()
    return service


def _run(**kwargs) -> str:
    out = StringIO()
    call_command("report_pilot_readiness", stdout=out, **kwargs)
    return out.getvalue()


@pytest.mark.django_db
class TestItCounts:
    def test_services_and_canon_by_group(self, salon: Tenant) -> None:
        _service(salon)
        _service(salon, status="review_required")
        _service(salon, active=False)

        report = _run(tenant_slug=salon.slug)

        assert "услуг всего:              3" in report
        assert "из них активных:        2" in report
        # Положительная пара: раскладка по статусам печатается вся, включая нули.
        assert "unmapped" in report and "verified" in report
        assert "verified и активна:       0" in report

    def test_goal_label_separates_two_reasons(self, salon: Tenant) -> None:
        """«Категории нет» и «категория есть, цели нет» — разные причины."""
        from services.models import GoalOption, GoalOptionCategory

        named = _category(salon, "Уход за лицом")
        other = _category(salon, "Эпиляция")
        goal = GoalOption.objects.create(key="tone_up", label="Привести себя в порядок")
        GoalOptionCategory.objects.create(goal_option=goal, category=named)

        _service(salon, category=named)
        _service(salon, category=other)
        _service(salon, category=None)  # старая строка без категории

        report = _run(tenant_slug=salon.slug)

        assert "категория названа целью:   1" in report
        assert "категория есть, цели нет:  1" in report
        assert "категории нет вовсе:       1" in report

    def test_masters_photo_counted_by_source_field(self, salon: Tenant) -> None:
        user = User.objects.create(username="m-1", tenant=salon)
        twin = User.objects.create(username="m-2", tenant=salon)
        SpecialistProfile.objects.create(user=user, tenant=salon, status="active", avatar="a.jpg")
        SpecialistProfile.objects.create(user=twin, tenant=salon, status="active", avatar="")

        report = _run(tenant_slug=salon.slug)

        assert "активных мастеров:        2" in report
        assert "фото есть:                 1" in report
        assert "фото нет:                  1" in report

    def test_zero_of_zero_is_not_zero_percent(self, salon: Tenant) -> None:
        """Ноль из нуля — «мерить нечего», а не «0 %»: разница видна владельцу."""
        report = _run(tenant_slug=salon.slug)

        assert "(—)" in report
        assert "(0 %)" not in report


@pytest.mark.django_db
class TestItNeverWrites:
    def test_not_a_single_write_query(self, salon: Tenant) -> None:
        """Обещание «только чтение» доказывается счётчиком, а не докстрокой."""
        _service(salon)

        with CaptureQueriesContext(connection) as queries:
            _run(tenant_slug=salon.slug)

        wrote = [
            q["sql"]
            for q in queries.captured_queries
            if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        assert queries.captured_queries, "положительная пара: команда вообще ходила в базу"
        assert wrote == [], wrote

    def test_there_is_no_apply_flag(self) -> None:
        """У читающего отчёта не должно быть даже возможности записать."""
        from services.management.commands.report_pilot_readiness import Command

        parser = Command().create_parser("manage.py", "report_pilot_readiness")
        flags = {action.dest for action in parser._actions}

        assert "tenant_slug" in flags  # положительная пара: разбор аргументов живой
        assert "apply" not in flags


@pytest.mark.django_db
class TestItPrintsScopeAndNoPeople:
    def test_scope_and_time_come_first(self, salon: Tenant) -> None:
        report = _run(tenant_slug=salon.slug)
        head = report.split("== 1.")[0]

        assert "снято:" in head and "база:" in head
        assert salon.slug in head
        assert "только чтение" in head

    def test_no_person_reaches_the_output(self, salon: Tenant) -> None:
        user = User.objects.create(username="pavel-ivanov", tenant=salon)
        SpecialistProfile.objects.create(
            user=user, tenant=salon, status="active", avatar="specialists/avatars/pavel.jpg"
        )

        report = _run(tenant_slug=salon.slug)

        assert "активных мастеров:        1" in report  # положительная пара: мастер посчитан
        assert "pavel" not in report.lower()
        assert ".jpg" not in report
