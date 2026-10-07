"""Перепись проверки здоровья по активным предложениям (DRF-2877, пакет S2).

Решение владельца 07.10: неподтверждённое «проверка не нужна» — «неизвестно»;
черновое «нужна» — «требование ещё не подтверждено»; правило записи
включается только после его просмотра списка затронутых услуг. Команда
строит этот список и ничего не включает.

Узлы держат:

* единица — активное предложение; выключенное в перепись не попадает,
  предложение без продаваемого мастера стоит отдельно и в «после» не идёт;
* «сегодня» — вердикт самого гейта записи, а не свой расчёт;
* «после»: подтверждённое «нужна» у канона — проверка клиента;
  подтверждённое «не нужна» без поднятых требований — проходит; всё
  остальное — условия услуги не определены, с причинами;
* ответ салона без происхождения подтверждённым не считается — ни «нужна»,
  ни «не нужна»;
* список последствий — предложения, которые сегодня проходят запись и
  перестанут; то, что неизвестно уже сегодня, в него не входит;
* ничего не пишется — счётчиком запросов; людей в выводе нет.
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

from services.health_check_census import (
    CANON_FLAG_INFERRED,
    CANON_MISSING,
    CLIENT_CHECK,
    MASTER_RAISE_UNCONFIRMED,
    PASSES,
    SALON_RAISE_UNCONFIRMED,
    UNDEFINED,
    UNKNOWN,
    health_check_census,
    salon_answer_confirmed,
)
from services.models import SalonService, ServiceCategory, ServiceTemplate, SpecialistService
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

Origin = ServiceTemplate.HealthCheckOrigin
_SEQ = itertools.count(1)


@pytest.fixture
def staff(db):
    return User.objects.create_user(username="hc-census-staff", password="x")


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="hc-census-salon", name="Салон переписи")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Перепись 2877", slug="hc-census-2877")


def _master(tenant, username):
    user = User.objects.create_user(username=username, password="x", first_name="Павел")
    return SpecialistProfile.objects.create(
        user=user, tenant=tenant, display_name="Павел Иванов",
        status=SpecialistProfile.ProfileStatus.ACTIVE,
    )


@pytest.fixture
def master(salon):
    return _master(salon, "pavel-ivanov-hc")


def _canon(category, staff, *, flag, confirmed) -> ServiceTemplate:
    n = next(_SEQ)
    canon = ServiceTemplate.objects.create(
        category=category, name=f"Канон {n}", name_short=f"Канон {n}", requires_health_check=flag,
    )
    if confirmed:
        ServiceTemplate.objects.filter(pk=canon.pk).update(
            health_check_origin=Origin.CONFIRMED, health_check_confirmed_by=staff,
            health_check_confirmed_at=timezone.now(), health_check_source_ref="разбор владельца",
        )
    return canon


def _offer(salon, category, master, name, *, canon=None, answer=None, raised=False, active=True, sold=True):
    offer = SalonService.objects.create(
        tenant=salon, category=category, template=canon, name=name, is_active=active,
        requires_health_check=answer, duration_minutes=60, base_price=Decimal("3000"),
    )
    if sold:
        SpecialistService.objects.create(
            salon_service=offer, specialist=master, duration_minutes=60, price=Decimal("3000"),
            requires_health_check=raised,
        )
    return offer


@pytest.fixture
def pool(salon, category, staff, master):
    make = lambda name, **kw: _offer(salon, category, master, name, **kw)  # noqa: E731
    canon = lambda **kw: _canon(category, staff, **kw)  # noqa: E731
    return {
        # Канон подтверждённо «не нужна», никто не возражал: проходит и после.
        "clear": make("Маникюр", canon=canon(flag=False, confirmed=True)),
        # Канон подтверждённо «нужна»: проверку проходит клиент — и сегодня, и после.
        "gated": make("Лазер", canon=canon(flag=True, confirmed=True)),
        # Флаг канона выведен, «не нужна»: сегодня проходит, после — не определено.
        "draft_clear": make("Массаж", canon=canon(flag=False, confirmed=False)),
        # Флаг канона выведен, «нужна»: сегодня проверка, после — не определено.
        "draft_gated": make("Пилинг", canon=canon(flag=True, confirmed=False)),
        # Салон сказал «не нужна» без автора; канона нет.
        "salon_says_no": make("Услуга без канона", answer=False),
        # Салон поднял требование без автора при подтверждённом «не нужна» у канона.
        "salon_raised": make("Обёртывание", canon=canon(flag=False, confirmed=True), answer=True),
        # Мастер поднял требование сам.
        "master_raised": make("Чистка", canon=canon(flag=False, confirmed=True), raised=True),
        # Никто не отвечал, канона нет.
        "silent": make("Немая услуга"),
        # Продавать некому.
        "unsold": make("Маска", canon=canon(flag=False, confirmed=False), sold=False),
        # Выключена.
        "off": make("Выключенная", canon=canon(flag=False, confirmed=False), active=False),
    }


def _by_name(census):
    return {row.name: row for row in census.rows}


def _one(row):
    assert len(row.edges) == 1
    return row.edges[0]


def _run(*args, **kwargs) -> str:
    out = StringIO()
    call_command("health_check_census", *args, stdout=out, **kwargs)
    return out.getvalue()


# ─── перепись ────────────────────────────────────────────────────────────────


def test_the_unit_is_an_active_offer_and_the_unsold_stand_apart(pool, salon) -> None:
    census = health_check_census([salon.slug])
    rows = _by_name(census)

    assert len(census.rows) == 9 and "Выключенная" not in rows
    assert len(census.sellable) == 8
    assert rows["Маска"].edges == () and not rows["Маска"].stops
    assert sum(census.outcomes("after").values()) == 8


def test_today_is_the_verdict_of_the_booking_gate_itself(pool, salon) -> None:
    rows = _by_name(health_check_census([salon.slug]))

    for row in rows.values():
        for edge in SpecialistService.objects.filter(salon_service_id=row.pk):
            verdict, basis = edge.resolved_health_check()
            assert (_one(row).today, _one(row).basis) == (
                {True: CLIENT_CHECK, False: PASSES, None: UNKNOWN}[verdict], basis,
            )
    assert {name: _one(rows[name]).today for name in ("Маникюр", "Лазер", "Немая услуга")} == {
        "Маникюр": PASSES, "Лазер": CLIENT_CHECK, "Немая услуга": UNKNOWN,
    }


def test_after_the_rule_only_a_confirmed_canon_decides(pool, salon) -> None:
    rows = _by_name(health_check_census([salon.slug]))
    after = {name: (_one(row).after, _one(row).reasons) for name, row in rows.items() if row.edges}

    assert after == {
        "Маникюр": (PASSES, ()),
        "Лазер": (CLIENT_CHECK, ()),
        "Массаж": (UNDEFINED, (CANON_FLAG_INFERRED,)),
        "Пилинг": (UNDEFINED, (CANON_FLAG_INFERRED,)),
        "Услуга без канона": (UNDEFINED, (CANON_MISSING,)),
        "Обёртывание": (UNDEFINED, (SALON_RAISE_UNCONFIRMED,)),
        "Чистка": (UNDEFINED, (MASTER_RAISE_UNCONFIRMED,)),
        "Немая услуга": (UNDEFINED, (CANON_MISSING,)),
    }


def test_a_salon_answer_without_an_author_is_not_a_confirmed_answer(pool, salon) -> None:
    """Ни «не нужна», ни «нужна» салона без происхождения ответом человека не являются."""
    rows = _by_name(health_check_census([salon.slug]))

    assert not any(salon_answer_confirmed(offer) for offer in SalonService.objects.all())
    assert (rows["Услуга без канона"].salon_answer, rows["Услуга без канона"].salon_confirmed) == (False, False)
    # Сегодня «не нужна» салона открывает запись; после правила — нет.
    assert (_one(rows["Услуга без канона"]).today, _one(rows["Услуга без канона"]).after) == (PASSES, UNDEFINED)
    # Сегодня «нужна» салона отправляет клиента на проверку; после — требование не подтверждено.
    assert (_one(rows["Обёртывание"]).today, _one(rows["Обёртывание"]).after) == (CLIENT_CHECK, UNDEFINED)


def test_the_consequences_are_the_offers_that_pass_today_and_will_stop(pool, salon) -> None:
    census = health_check_census([salon.slug])

    assert sorted(r.name for r in census.stopping) == ["Массаж", "Услуга без канона"]
    rows = _by_name(census)
    # Неизвестно уже сегодня — не изменение; проходит и после — не последствие.
    assert not rows["Немая услуга"].stops and not rows["Маникюр"].stops
    # Чистка: запись сегодня не проходит (мастер поднял требование) — не последствие.
    assert not rows["Чистка"].stops


def test_an_offer_passes_if_any_of_its_masters_does(salon, category, staff, master) -> None:
    canon = _canon(category, staff, flag=False, confirmed=True)
    offer = _offer(salon, category, master, "Маникюр", canon=canon, raised=True)
    SpecialistService.objects.create(
        salon_service=offer, specialist=_master(salon, "hc-second-master"),
        duration_minutes=60, price=Decimal("3000"),
    )

    row = _by_name(health_check_census([salon.slug]))["Маникюр"]

    assert sorted(e.after for e in row.edges) == [PASSES, UNDEFINED]
    assert row.passes("today") and row.passes("after") and not row.stops
    assert row.master_raises == 1


def test_the_counts_name_who_decides_today(pool, salon) -> None:
    census = health_check_census([salon.slug])

    assert census.salon_answers() == {(None, False): 7, (False, False): 1, (True, False): 1}
    assert census.canon_flags() == {
        (None, False): 2, (False, True): 3, (True, True): 1, (False, False): 2, (True, False): 1,
    }
    assert census.outcomes("today") == {PASSES: 3, CLIENT_CHECK: 4, UNKNOWN: 1}
    assert census.outcomes("after") == {PASSES: 1, CLIENT_CHECK: 1, UNDEFINED: 6}


def test_the_scope_narrows_to_the_named_salons(pool, salon, category) -> None:
    other = Tenant.objects.create(slug="hc-census-other", name="Другой салон")
    _offer(other, category, _master(other, "hc-other-master"), "Чужая услуга")

    assert len(health_check_census([salon.slug]).rows) == 9
    assert len(health_check_census(None).rows) == 10


# ─── команда ─────────────────────────────────────────────────────────────────


def test_the_command_prints_the_scope_first_and_the_consequences(pool, salon) -> None:
    report = _run(tenant_slugs=[salon.slug])
    head = report.split("== 1.")[0]

    assert "снято:" in head and "база:" in head and salon.slug in head and "только чтение" in head
    assert "активных предложений:          9" in report
    assert "предложений, которые сегодня проходят запись и перестанут: 2" in report
    assert "условия услуги не определены" in report and "нужна проверка клиента" in report
    assert "Правило записи не включено" in report


def test_the_details_name_every_offer_and_mark_the_ones_that_stop(pool, salon) -> None:
    report = _run("--details", tenant_slugs=[salon.slug])

    massage = next(row for row in report.splitlines() if "| Массаж |" in row)
    assert "канон: не нужна (не подтверждён)" in massage and massage.endswith("ПЕРЕСТАНЕТ")
    manicure = next(row for row in report.splitlines() if "| Маникюр |" in row)
    assert "канон: не нужна (подтверждён)" in manicure and "ПЕРЕСТАНЕТ" not in manicure
    no_canon = next(row for row in report.splitlines() if "Услуга без канона" in row)
    assert "салон: не нужна (не подтверждён)" in no_canon and "нет канона" in no_canon
    assert "нет продаваемого мастера — записи нет" in next(
        row for row in report.splitlines() if "| Маска |" in row
    )


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
    from services.management.commands.health_check_census import Command

    flags = {action.dest for action in Command().create_parser("manage.py", "x")._actions}

    assert "tenant_slugs" in flags  # положительная пара: разбор аргументов живой
    assert "apply" not in flags and "confirm" not in flags


def test_no_person_reaches_the_output(pool, salon) -> None:
    report = _run("--details", tenant_slugs=[salon.slug])

    assert "мастеров 1" in report  # положительная пара: мастер посчитан
    for leak in ("Павел", "Иванов", "pavel-ivanov-hc", "hc-census-staff"):
        assert leak not in report
