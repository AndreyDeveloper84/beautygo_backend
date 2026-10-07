"""Dry-run включения флага fail-closed по активным предложениям (DRF-2866).

Решение владельца 07.10: «census=0 только по шаблонам недостаточно; нужен
dry-run по активным предложениям». Команду пустят на стенд перед включением
флага, и по её выводу владелец примет список закрываемых предложений.

Узлы держат:

* единица — активное предложение, включая предложение без канона;
  выключенное в перепись не попадает;
* допуск считается при обоих положениях флага, а текущее положение флага на
  числа не влияет и не меняется;
* причины не сливаются: предложение стоит под каждой своей причиной, а
  первая — та, что ушла бы на провод;
* неизвестная область считается по предложениям и раскладывается по
  разделам кода; предложение без канона — отдельной строкой;
* ничего не пишется — счётчиком запросов, не докстрокой;
* людей в выводе нет;
* ``--fail-on-unclassified`` отвечает кодом выхода.
"""

from __future__ import annotations

import itertools
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from services.body_care_admission import (
    CLASS_UNCONFIRMED,
    LINK_NOT_VERIFIED,
    NO_CANON,
    NO_CODE,
    NO_SELLABLE_MASTER,
    REASON_UNCLASSIFIED,
    admission_census,
)
from services.models import SalonService, ServiceCategory, ServiceTemplate, SpecialistService
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

Scope = ServiceTemplate.BodyCareScope
LC = ServiceTemplate.LegalServiceClass
_SEQ = itertools.count(1)


@pytest.fixture
def staff(db):
    return User.objects.create_user(username="dryrun-staff", password="x")


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="dryrun-salon", name="Салон прогона")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Прогон 2866", slug="dryrun-2866")


def _master(tenant, username="pavel-ivanov-dryrun"):
    """Продаваемый мастер салона: услугу салона оказывает только его мастер."""
    user = User.objects.create_user(username=username, password="x", first_name="Павел")
    return SpecialistProfile.objects.create(
        user=user, tenant=tenant, display_name="Павел Иванов",
        status=SpecialistProfile.ProfileStatus.ACTIVE,
    )


@pytest.fixture
def master(salon):
    return _master(salon)


def _offer(
    salon, category, staff, master, name, *, code=None, canon=True, scope=None, legal_class=None,
    verified=True, active=True, sold=True,
) -> SalonService:
    template = None
    if canon:
        template = ServiceTemplate.objects.create(
            category=category, name=f"Канон {name} {next(_SEQ)}", name_short=name[:40], canonical_code=code,
        )
        fields = {}
        if scope is not None:
            fields.update(
                body_care_scope=scope, scope_confirmed_by=staff,
                scope_confirmed_at=timezone.now(), scope_source_ref="решение владельца",
            )
        if legal_class is not None:
            fields.update(
                legal_service_class=legal_class, legal_class_confirmed_by=staff,
                legal_class_confirmed_at=timezone.now(), legal_class_source_ref="решение юриста",
            )
        if fields:
            ServiceTemplate.objects.filter(pk=template.pk).update(**fields)
    link = (
        {
            "mapping_status": "verified", "mapping_confirmed_rule": "test_fixture",
            "mapping_rule_version": "1.0.0", "mapping_confirmed_at": timezone.now(),
            "mapping_source_ref": "узел dry-run",
        }
        if verified and canon else {"mapping_status": "unmapped"}
    )
    offer = SalonService.objects.create(
        tenant=salon, category=category, template=template, name=name, is_active=active,
        duration_minutes=60, base_price=Decimal("3000"), **link,
    )
    if sold:
        SpecialistService.objects.create(
            salon_service=offer, specialist=master, duration_minutes=60, price=Decimal("3000"),
        )
    return offer


@pytest.fixture
def pool(salon, category, staff, master):
    make = lambda name, **kw: _offer(salon, category, staff, master, name, **kw)  # noqa: E731
    return {
        # Открыта и сегодня, и после: область и класс подтверждены.
        "massage": make("Массаж спины", code="1.1.4", scope=Scope.NOT_BODY_CARE,
                        legal_class=LC.NON_MEDICAL_COSMETIC),
        # Область правдива, класса нет: закроет юридическая проверка.
        "laser": make("Лазерная эпиляция", code="7.1.1", scope=Scope.NOT_BODY_CARE),
        # Спорный раздел: область неизвестна.
        "lymph": make("Лимфодренаж", code="1.4.1"),
        # Канон без кода, область неизвестна.
        "codeless": make("Авторский уход"),
        # Канона нет вовсе — связь не подтверждена уже сегодня.
        "no_canon": make("Услуга без канона", canon=False),
        # Продавать некому.
        "unsold": make("Маска", code="4.4.1", scope=Scope.NOT_BODY_CARE,
                       legal_class=LC.NON_MEDICAL_COSMETIC, sold=False),
        # Выключена — в перепись не попадает.
        "off": make("Выключенная", code="1.1.1", active=False),
    }


def _by_name(census):
    return {row.name: row for row in census.rows}


def _run(*args, **kwargs) -> str:
    out = StringIO()
    call_command("body_care_admission_dry_run", *args, stdout=out, **kwargs)
    return out.getvalue()


# ─── перепись ────────────────────────────────────────────────────────────────


def test_the_unit_is_an_active_offer_including_one_without_a_canon(pool, salon) -> None:
    census = admission_census([salon.slug])

    assert census.total == 6
    rows = _by_name(census)
    assert "Выключенная" not in rows
    assert rows["Услуга без канона"].canon == NO_CANON
    assert rows["Авторский уход"].canon == NO_CODE
    assert rows["Лимфодренаж"].canon == "1.4.1"


def test_today_the_bypass_lets_everything_with_a_verified_link_through(pool, salon) -> None:
    rows = _by_name(admission_census([salon.slug]))

    assert {name for name, row in rows.items() if row.today.available} == {
        "Массаж спины", "Лазерная эпиляция", "Лимфодренаж", "Авторский уход",
    }
    assert rows["Услуга без канона"].today.reasons == (LINK_NOT_VERIFIED,)
    assert rows["Маска"].today.reasons == (NO_SELLABLE_MASTER,)


def test_after_the_flag_each_offer_closes_for_its_own_reasons(pool, salon) -> None:
    census = admission_census([salon.slug])
    rows = _by_name(census)

    assert [r.name for r in census.available("after")] == ["Массаж спины"]
    assert rows["Лазерная эпиляция"].after.reasons == (CLASS_UNCONFIRMED,)
    # Причины не сливаются: неизвестная область и неподтверждённый класс — обе.
    assert rows["Лимфодренаж"].after.reasons == (REASON_UNCLASSIFIED, CLASS_UNCONFIRMED)
    assert rows["Авторский уход"].after.reasons == (REASON_UNCLASSIFIED, CLASS_UNCONFIRMED)
    assert rows["Услуга без канона"].after.reasons == (
        LINK_NOT_VERIFIED, REASON_UNCLASSIFIED, CLASS_UNCONFIRMED,
    )
    assert rows["Маска"].after.reasons == (NO_SELLABLE_MASTER,)
    assert {r.name for r in census.closing} == {"Лазерная эпиляция", "Лимфодренаж", "Авторский уход"}
    assert census.opening == []


def test_reasons_are_counted_under_each_and_the_first_is_the_wire_one(pool, salon) -> None:
    census = admission_census([salon.slug])

    assert census.reasons("after") == {
        LINK_NOT_VERIFIED: 1, NO_SELLABLE_MASTER: 1, REASON_UNCLASSIFIED: 3, CLASS_UNCONFIRMED: 4,
    }
    assert census.first_reasons("after") == {
        LINK_NOT_VERIFIED: 1, NO_SELLABLE_MASTER: 1, REASON_UNCLASSIFIED: 2, CLASS_UNCONFIRMED: 1,
    }


def test_the_unknown_scope_is_counted_by_offers_and_grouped_by_section(pool, salon) -> None:
    census = admission_census([salon.slug])

    assert sorted((r.section, r.name) for r in census.unclassified) == [
        ("1.4", "Лимфодренаж"), (NO_CODE, "Авторский уход"), (NO_CANON, "Услуга без канона"),
    ]


@pytest.mark.parametrize("flag", [False, True], ids=["флаг-выключен", "флаг-включён"])
def test_the_current_flag_changes_neither_column_and_is_left_as_it_was(settings, pool, salon, flag) -> None:
    settings.BODY_CARE_UNCLASSIFIED_FAIL_CLOSED = flag

    census = admission_census([salon.slug])

    assert census.flag_now is flag
    assert (len(census.available("today")), len(census.available("after"))) == (4, 1)
    assert settings.BODY_CARE_UNCLASSIFIED_FAIL_CLOSED is flag


def test_the_scope_narrows_to_the_named_salons(pool, salon, category, staff) -> None:
    other = Tenant.objects.create(slug="dryrun-other", name="Другой салон")
    _offer(other, category, staff, _master(other, "dryrun-other-master"), "Чужая услуга", code="1.1.5")

    assert admission_census([salon.slug]).total == 6
    assert admission_census(None).total == 7
    assert [r.name for r in admission_census([other.slug]).rows] == ["Чужая услуга"]


# ─── команда ─────────────────────────────────────────────────────────────────


def test_the_command_prints_the_scope_first_and_the_numbers(pool, salon) -> None:
    report = _run(tenant_slugs=[salon.slug])
    head = report.split("== 1.")[0]

    assert "снято:" in head and "база:" in head and salon.slug in head
    assert "выключен — обход открыт" in head and "только чтение" in head
    assert "активных предложений:        6" in report
    assert "допущено сегодня:          4" in report
    assert "допущено после включения:  1" in report
    assert "закроется при включении:   3" in report
    assert "активных предложений с неизвестной областью: 3" in report
    assert "из них допущены сегодня (их закроет именно это): 2" in report


def test_the_details_name_every_offer_with_all_its_reasons(pool, salon) -> None:
    report = _run("--details", tenant_slugs=[salon.slug])

    line = next(row for row in report.splitlines() if "Лимфодренаж" in row)
    assert "1.4.1" in line and "сегодня: допущено" in line
    assert "после: закрыто: unclassified, class_unconfirmed" in line
    assert f"{NO_CANON} | Услуга без канона" in report


def test_not_a_single_write_query(pool, salon) -> None:
    """Обещание «только чтение» доказывается счётчиком, а не докстрокой."""
    with CaptureQueriesContext(connection) as queries:
        _run("--details", tenant_slugs=[salon.slug])

    wrote = [
        q["sql"]
        for q in queries.captured_queries
        if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    assert queries.captured_queries, "положительная пара: команда вообще ходила в базу"
    assert wrote == [], wrote


def test_there_is_no_apply_flag() -> None:
    from services.management.commands.body_care_admission_dry_run import Command

    flags = {action.dest for action in Command().create_parser("manage.py", "x")._actions}

    assert "tenant_slugs" in flags  # положительная пара: разбор аргументов живой
    assert "apply" not in flags


def test_no_person_reaches_the_output(pool, salon) -> None:
    report = _run("--details", tenant_slugs=[salon.slug])

    assert "мастеров 1" in report  # положительная пара: мастер посчитан
    for leak in ("Павел", "Иванов", "pavel-ivanov-dryrun", "dryrun-staff"):
        assert leak not in report


def test_fail_on_unclassified_answers_with_the_exit_code(pool, salon, category, staff) -> None:
    with pytest.raises(SystemExit) as stop:
        _run("--fail-on-unclassified", tenant_slugs=[salon.slug])
    assert stop.value.code == 2

    clean = Tenant.objects.create(slug="dryrun-clean", name="Чистый салон")
    _offer(clean, category, staff, _master(clean, "dryrun-clean-master"), "Массаж шеи", code="1.1.5",
           scope=Scope.NOT_BODY_CARE, legal_class=LC.NON_MEDICAL_COSMETIC)

    assert "с неизвестной областью: 0" in _run("--fail-on-unclassified", tenant_slugs=[clean.slug])


def test_the_readiness_report_carries_the_same_numbers(pool, salon) -> None:
    out = StringIO()
    call_command("report_pilot_readiness", tenant_slug=salon.slug, stdout=out)
    report = out.getvalue()

    assert "== 4. Допуск к рекомендации и классификация (DRF-2866) ==" in report
    assert "выключен — обход открыт" in report
    assert "допущено сегодня:                 4" in report
    assert "допущено после включения флага:   1" in report
    assert "с неизвестной областью:           3" in report
    assert "класс не подтверждён:             4" in report
