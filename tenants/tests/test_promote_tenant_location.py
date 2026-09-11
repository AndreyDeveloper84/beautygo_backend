"""`promote_tenant_location` — перенос адреса одного салона (§9, DRF-1687, L3).

Три вещи, которые команда обязана НЕ делать, и по одной проверке на каждую:
не переносить массово (без `--slug` — отказ, ноль записей), не объявлять
подтверждённым то, что никто не подтвердил (без `--confirm --by --source-ref`
— только `review_required`), не плодить дубли (тот же адрес — существующее
место, не второе).
"""
from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from tenants.models import LocationStatus, ServiceLocation, Tenant
from users.models import User

pytestmark = pytest.mark.django_db


def _run(**kw):
    out, err = StringIO(), StringIO()
    code = 0
    try:
        call_command("promote_tenant_location", stdout=out, stderr=err, **kw)
    except SystemExit as exc:
        code = exc.code
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def formula():
    return Tenant.objects.create(slug="formula-tela-t", name="Формула тела",
                                 city="Пенза", address="г. Пенза, ул. Пушкина, д. 45")


@pytest.fixture
def operator():
    return User.objects.create_user(username="op-l3", password="x", phone="+79990001688")


def test_without_slug_it_refuses_and_writes_nothing(formula):
    """§9: массовый перенос запрещён — и по умолчанию его нет."""
    code, _, err = _run(apply=True)
    assert code == 2 and "массовый перенос" in err
    assert ServiceLocation.objects.count() == 0


def test_dry_run_is_the_default_and_writes_nothing(formula):
    code, out, _ = _run(slug="formula-tela-t")
    assert code == 0 and "СУХОЙ ПРОГОН" in out and "не записано" in out
    assert "review_required" in out and "происхождение не названо" in out
    assert ServiceLocation.objects.count() == 0


def test_apply_without_confirm_creates_review_required(formula):
    """Команда не знает, подтверждён ли адрес; знает человек — и говорит флагом."""
    code, out, _ = _run(slug="formula-tela-t", apply=True)
    assert code == 0 and "создано" in out
    loc = ServiceLocation.objects.get(tenant=formula)
    assert loc.status == LocationStatus.REVIEW_REQUIRED
    assert loc.address == "г. Пенза, ул. Пушкина, д. 45" and loc.city == "Пенза"
    assert loc.confirmed_by is None and loc.confirmed_source_ref == ""
    assert not loc.participates_in_distance
    formula.refresh_from_db()
    assert formula.address == "г. Пенза, ул. Пушкина, д. 45"  # вход не тронут


def test_confirm_requires_who_and_why(formula):
    code, _, err = _run(slug="formula-tela-t", apply=True, confirm=True)
    assert code == 2 and "--by" in err and "--source-ref" in err
    assert ServiceLocation.objects.count() == 0
    code, _, err = _run(slug="formula-tela-t", apply=True, confirm=True, by="nobody", source_ref="§10")
    assert code == 2 and "nobody" in err
    assert ServiceLocation.objects.count() == 0


def test_confirm_with_provenance_creates_confirmed(formula, operator):
    """§10: адрес Формулы тела — подтверждён владельцем, основание названо."""
    code, out, _ = _run(slug="formula-tela-t", apply=True, confirm=True, by="op-l3",
                        source_ref="ayla-owner-decisions-2026-09-11 §10", label="Салон на Пушкина")
    assert code == 0 and "confirmed" in out
    loc = ServiceLocation.objects.get(tenant=formula)
    assert loc.status == LocationStatus.CONFIRMED and loc.confirmed_by == operator
    assert loc.confirmed_at is not None and "§10" in loc.confirmed_source_ref
    assert loc.label == "Салон на Пушкина"
    assert not loc.participates_in_distance  # подтверждено, но ещё не геокодировано (L4)


def test_the_same_address_is_not_created_twice(formula, operator):
    _run(slug="formula-tela-t", apply=True)
    formula.address = "г. Пенза,  ул. Пушкина, д. 45"  # те же слова, другие пробелы
    formula.save(update_fields=["address"])
    code, out, _ = _run(slug="formula-tela-t", apply=True, confirm=True, by="op-l3", source_ref="§10")
    assert code == 0 and "дубль не создаётся" in out
    assert ServiceLocation.objects.filter(tenant=formula).count() == 1


def test_empty_address_and_unknown_slug_are_named_refusals(formula):
    code, _, err = _run(slug="no-such-salon", apply=True)
    assert code == 2 and "no-such-salon" in err
    formula.address = ""
    formula.save(update_fields=["address"])
    code, _, err = _run(slug="formula-tela-t", apply=True)
    assert code == 2 and "пустой адрес" in err
    assert ServiceLocation.objects.count() == 0


def test_subject_is_printed_first(formula):
    _, out, _ = _run(slug="formula-tela-t")
    assert out.index("== ПРЕДМЕТ") < out.index("== ПЕРЕНОС")
