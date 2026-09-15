"""`propose_master_locations` — предложение по старым адресам мастеров (§9, H4; DRF-1924).

Команда обязана НЕ писать (ни строк, ни SQL записи), предлагать привязку только
к месту своего тенанта и не печатать адреса без флага. Каждое обещание —
отдельной проверкой; классы A–D — на одной фикстуре, где их нельзя спутать.
"""
from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from ai.tests.factories import make_specialist
from tenants.models import LocationStatus, ServiceLocation, Tenant
from users.models import SpecialistProfile

pytestmark = pytest.mark.django_db

PLACE_ADDRESS = "г. Пенза, ул. Пушкина, д. 45"
SALON_ADDRESS = "г. Пенза, ул. Мира, д. 1"
OTHER_PLACE_ADDRESS = "г. Пенза, ул. Кирова, д. 7"
HOME_ADDRESS = "г. Пенза, ул. Садовая, д. 5, кв. 12"
NOWHERE_ADDRESS = "г. Пенза, ул. Лесная, д. 3"


def _run(**kw) -> str:
    out = StringIO()
    call_command("propose_master_locations", stdout=out, **kw)
    return out.getvalue()


def _tenant(slug: str, address: str = "") -> Tenant:
    return Tenant.objects.create(slug=slug, name=slug, city="Пенза", address=address)


def _master(tenant: Tenant | None, address: str) -> SpecialistProfile:
    master = make_specialist(display_name=f"Мастер {address[-6:]}", address=address)
    SpecialistProfile.objects.filter(pk=master.pk).update(tenant=tenant)
    master.refresh_from_db()
    return master


def _place(tenant: Tenant, address: str, status: str = LocationStatus.REVIEW_REQUIRED) -> ServiceLocation:
    return ServiceLocation.objects.create(tenant=tenant, address=address, city="Пенза", status=status)


@pytest.fixture
def four_classes():
    """По мастеру на класс; адреса разные, чтобы класс не мог «перетечь» в соседний."""
    with_place = _tenant("t1924-place")
    place = _place(with_place, PLACE_ADDRESS)
    with_address = _tenant("t1924-address", address=SALON_ADDRESS)
    with_other_place = _tenant("t1924-other", address=OTHER_PLACE_ADDRESS)
    _place(with_other_place, OTHER_PLACE_ADDRESS)
    empty = _tenant("t1924-empty")
    return {
        "place": place,
        "A": _master(with_place, "  Г. ПЕНЗА,  ул. Пушкина, д. 45 "),  # те же слова, другой регистр и пробелы
        "B": _master(with_address, SALON_ADDRESS),
        "C": _master(with_other_place, HOME_ADDRESS),
        "D": _master(empty, NOWHERE_ADDRESS),
    }


def _row(out: str, profile: SpecialistProfile) -> str:
    rows = [line for line in out.splitlines() if f"профиль={profile.pk}" in line]
    assert len(rows) == 1, rows
    return rows[0]


def test_dry_run_writes_nothing_rows_or_sql(four_classes):
    """H4: только предложение. Счётчики до и после равны, и в SQL прогона нет ни одной записи."""
    before = (
        ServiceLocation.objects.count(),
        SpecialistProfile.objects.count(),
        SpecialistProfile.objects.filter(works_at__isnull=False).count(),
        Tenant.all_objects.count(),
    )
    with CaptureQueriesContext(connection) as ctx:
        _run(with_address=True)
    after = (
        ServiceLocation.objects.count(),
        SpecialistProfile.objects.count(),
        SpecialistProfile.objects.filter(works_at__isnull=False).count(),
        Tenant.all_objects.count(),
    )
    assert before == after
    statements = [q["sql"].lstrip().split(None, 1)[0].upper() for q in ctx.captured_queries]
    assert "SELECT" in statements  # скан не пуст: команда читала базу этим соединением
    writes = [s for s in statements if s in {"INSERT", "UPDATE", "DELETE", "TRUNCATE"}]
    assert writes == []


def test_each_row_gets_its_own_class(four_classes):
    out = _run()
    a = _row(out, four_classes["A"])
    assert a.lstrip().startswith("[A]") and f"привязать к месту {four_classes['place'].pk} [review_required]" in a
    b = _row(out, four_classes["B"])
    assert b.lstrip().startswith("[B]") and "promote_tenant_location --slug t1924-address" in b
    c = _row(out, four_classes["C"])
    assert c.lstrip().startswith("[C]") and "решает владелец" in c
    d = _row(out, four_classes["D"])
    assert d.lstrip().startswith("[D]") and "нет ни адреса, ни мест" in d
    assert "итого по классам: A=1 B=1 C=1 D=1 · сумма 4 = без места 4" in out


def test_place_of_another_tenant_is_never_proposed():
    """Тот же адрес у места чужого тенанта — не повод привязывать."""
    foreign = _tenant("t1924-foreign")
    foreign_place = _place(foreign, PLACE_ADDRESS, status=LocationStatus.INACTIVE)
    mine = _tenant("t1924-mine")
    master = _master(mine, PLACE_ADDRESS)

    out = _run()
    row = _row(out, master)
    assert row.lstrip().startswith("[D]")
    assert str(foreign_place.pk) not in out


def test_already_placed_master_is_excluded_but_counted(four_classes):
    placed = _master(four_classes["A"].tenant, PLACE_ADDRESS)
    SpecialistProfile.objects.filter(pk=placed.pk).update(works_at=four_classes["place"])

    out = _run()
    assert f"профиль={placed.pk}" not in out
    assert "уже с местом (исключены)       : 1" in out
    assert "без места (строки ниже)        : 4" in out


def test_master_without_address_is_counted_but_not_listed(four_classes):
    no_address = _master(four_classes["D"].tenant, "")

    out = _run()
    assert f"профиль={no_address.pk}" not in out
    assert "профилей мастеров всего        : 5" in out
    assert "со старым адресом              : 4" in out


def test_addresses_are_printed_only_with_the_flag_and_then_with_a_warning(four_classes):
    plain = _run()
    for address in (PLACE_ADDRESS, SALON_ADDRESS, OTHER_PLACE_ADDRESS, HOME_ADDRESS, NOWHERE_ADDRESS):
        assert address not in plain
    assert "ПДн" not in plain

    shown = _run(with_address=True)
    assert HOME_ADDRESS in shown and SALON_ADDRESS in shown
    warning = shown.index("вывод содержит адреса мастеров (ПДн) — в docs, Linear и чат не переносить")
    assert warning < shown.index("профиль=")


def test_kind_default_is_named_and_masters_per_tenant_are_printed(four_classes):
    _master(four_classes["B"].tenant, SALON_ADDRESS)

    out = _run()
    assert "kind=salon у тенантов, заведённых до G4, — умолчание, а не доказательство салона" in out
    assert "тенант=t1924-address (salon, мастеров в тенанте 2)" in _row(out, four_classes["B"])
    assert "тенант=t1924-empty (salon, мастеров в тенанте 1)" in _row(out, four_classes["D"])


def test_subject_is_printed_first(four_classes):
    out = _run()
    assert out.index("== ПРЕДМЕТ") < out.index("== ПРЕДЛОЖЕНИЕ")
