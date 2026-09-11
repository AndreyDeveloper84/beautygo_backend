"""Контракт исхода и путь записи адаптера геокодера (§139, DRF-1662).

Что здесь доказывается и чем:

* каждый исход провайдера отображается в **свой** статус §139, и два
  статуса — ``CONFIRMED`` и ``NOT_ATTEMPTED`` — не выходят из адаптера
  никогда (перебор по всем исходам, не по выбранным);
* «нашёл» — ещё не «нашёл там»: результат в другом городе и результат с
  точностью до населённого пункта — ``AMBIGUOUS``, не ``OK``. Оба случая
  из живой пробы Nominatim 11.09.2026 («Кирова 20» → Кузнецк, «Ладожская
  130» → улица);
* путь записи не перезаписывает человека, не портит хорошее
  недоступностью и не обходит правила модели — (0, 0) от провайдера
  поднимает ``ValidationError`` и ничего не сохраняет;
* набор модулей, ходящих к внешним геокодерам, закрыт — с положительным
  контролем, что сканер вообще видит ``services/geocoding.py``.
"""
from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError

from core.geocoding.apply import WRITTEN_FIELDS, apply_result
from core.geocoding.contract import (
    MIN_PRECISION_FOR_OK,
    GeocodeResult,
    Outcome,
    Precision,
    status_for,
)
from core.geocoding.providers import (
    GEOCODER_HOST_MARKERS,
    GEOCODING_MODULES,
    PROVIDERS,
    get_provider,
)
from core.geocoding.providers.yandex import LICENCE_NOTE, YandexGeocoder
from tenants.models import GeocodeStatus, Tenant

PENZA = ("Пенза", D("53.195878"), D("45.018316"))


def _found(lat=PENZA[1], lng=PENZA[2], *, precision=Precision.HOUSE, locality="г Пенза"):
    return GeocodeResult(
        outcome=Outcome.FOUND, provider="fake", latitude=lat, longitude=lng,
        normalized_address="г Пенза, ул Кирова, д 20", precision=precision,
        provider_precision="0", locality=locality,
    )


# ---------------------------------------------------------------------------
# Отображение исходов
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome, expected",
    [
        (Outcome.UNAVAILABLE, GeocodeStatus.PENDING),
        (Outcome.NOT_FOUND, GeocodeStatus.FAILED),
        (Outcome.MULTIPLE, GeocodeStatus.AMBIGUOUS),
    ],
)
def test_each_provider_outcome_has_its_own_status(outcome, expected):
    """Три исхода — три разных статуса. Слить их значило бы повторять вечно
    то, что не изменится, или звать человека выбирать из пустого списка."""
    r = GeocodeResult(outcome=outcome, provider="fake")
    assert status_for(r, expected_city="Пенза") == expected


def test_found_in_the_right_city_at_house_precision_is_ok():
    assert status_for(_found(), expected_city="Пенза") == GeocodeStatus.OK


def test_found_in_another_city_is_ambiguous_not_ok():
    """«Кирова 20» уехала в Кузнецк — с домовой точностью, уверенно, в
    другой город. Одного FOUND мало."""
    r = _found(locality="г Кузнецк")
    assert status_for(r, expected_city="Пенза") == GeocodeStatus.AMBIGUOUS


def test_found_without_locality_is_ambiguous():
    """Провайдер не сказал, где нашёл, — уверенность взять неоткуда."""
    assert status_for(_found(locality=""), expected_city="Пенза") == GeocodeStatus.AMBIGUOUS


def test_street_precision_is_still_ok_but_locality_precision_is_not():
    """Улица ошибается на сотни метров при пороге 25 км — приемлемо.
    Населённый пункт кладёт все места в одну точку — это не координаты."""
    assert status_for(_found(precision=Precision.STREET), expected_city="Пенза") == GeocodeStatus.OK
    assert status_for(_found(precision=Precision.LOCALITY), expected_city="Пенза") == GeocodeStatus.AMBIGUOUS
    assert Precision.LOCALITY not in MIN_PRECISION_FOR_OK


def test_found_without_coordinates_is_ambiguous():
    r = _found(lat=None, lng=None)
    assert status_for(r, expected_city="Пенза") == GeocodeStatus.AMBIGUOUS


def test_misconfigured_has_no_status_and_stops_the_caller():
    """Пустая переменная окружения — не лежащий сервис. Одиннадцать pending
    из-за неё выглядели бы как отказ провайдера."""
    r = GeocodeResult(outcome=Outcome.MISCONFIGURED, provider="fake", reason="ключа нет")
    with pytest.raises(ValueError, match="MISCONFIGURED"):
        status_for(r, expected_city="Пенза")


def test_confirmed_and_not_attempted_never_come_out_of_the_adapter():
    """Перебор ВСЕХ исходов, а не выбранных: положительная стража на то,
    что таблица отображения полна и не содержит исходов человека."""
    produced = set()
    for outcome in Outcome:
        if outcome is Outcome.MISCONFIGURED:
            continue
        for locality in ("г Пенза", "г Кузнецк", ""):
            for precision in Precision:
                r = GeocodeResult(
                    outcome=outcome, provider="fake", latitude=PENZA[1], longitude=PENZA[2],
                    precision=precision, locality=locality,
                )
                produced.add(status_for(r, expected_city="Пенза"))
    assert GeocodeStatus.CONFIRMED not in produced
    assert GeocodeStatus.NOT_ATTEMPTED not in produced
    # и все четыре машинных исхода достижимы — иначе таблица неполна
    assert produced == {
        GeocodeStatus.OK, GeocodeStatus.AMBIGUOUS, GeocodeStatus.PENDING, GeocodeStatus.FAILED,
    }


# ---------------------------------------------------------------------------
# Путь записи
# ---------------------------------------------------------------------------

def _tenant(**kw) -> Tenant:
    defaults = dict(name="Салон", slug="salon", address="ул Кирова, д 20", city="Пенза")
    defaults.update(kw)
    return Tenant.objects.create(**defaults)


@pytest.mark.django_db
def test_apply_writes_exactly_the_eight_fields_of_139():
    t = _tenant()
    a = apply_result(t, _found(), source_address=t.address)
    assert a.written and a.status == GeocodeStatus.OK
    t.refresh_from_db()
    assert t.is_geocoded
    assert t.geocode_provider == "fake"
    assert t.geocode_source_address == "ул Кирова, д 20"
    assert t.geocode_normalized_address == "г Пенза, ул Кирова, д 20"
    assert t.geocode_precision == "0"
    assert t.geocoded_at is not None
    assert set(WRITTEN_FIELDS) == {
        "geocode_source_address", "geocode_normalized_address", "latitude", "longitude",
        "geocode_provider", "geocode_precision", "geocode_status", "geocoded_at",
    }


@pytest.mark.django_db
def test_dry_run_changes_nothing_in_the_database_but_reports_the_same():
    t = _tenant()
    a = apply_result(t, _found(), source_address=t.address, dry_run=True)
    assert a.status == GeocodeStatus.OK  # тот же ответ, что при записи
    fresh = Tenant.objects.get(pk=t.pk)
    assert fresh.geocode_status == GeocodeStatus.NOT_ATTEMPTED
    assert fresh.latitude is None


@pytest.mark.django_db
def test_confirmed_by_a_human_is_never_overwritten():
    t = _tenant(
        geocode_status=GeocodeStatus.CONFIRMED, latitude=PENZA[1], longitude=PENZA[2],
        geocode_provider="manual",
    )
    a = apply_result(t, _found(lat=D("1"), lng=D("1")), source_address=t.address, overwrite_ok=True)
    assert not a.written and "человеком" in a.skipped_because
    t.refresh_from_db()
    assert t.geocode_provider == "manual" and t.latitude == PENZA[1]


@pytest.mark.django_db
def test_ok_is_not_regeocoded_without_the_flag_and_is_with_it():
    t = _tenant(geocode_status=GeocodeStatus.OK, latitude=PENZA[1], longitude=PENZA[2],
                geocode_provider="old")
    a = apply_result(t, _found(), source_address=t.address)
    assert not a.written and "overwrite-ok" in a.skipped_because
    a = apply_result(t, _found(), source_address=t.address, overwrite_ok=True)
    assert a.written
    t.refresh_from_db()
    assert t.geocode_provider == "fake"


@pytest.mark.django_db
def test_unavailable_does_not_downgrade_a_geocoded_row():
    """«Новостей нет» — не «новости плохие». Один сбой сервиса не снимает
    с карты весь город."""
    t = _tenant(geocode_status=GeocodeStatus.OK, latitude=PENZA[1], longitude=PENZA[2],
                geocode_provider="old")
    down = GeocodeResult(outcome=Outcome.UNAVAILABLE, provider="fake", reason="timeout")
    a = apply_result(t, down, source_address=t.address, overwrite_ok=True)
    assert not a.written and "не портим" in a.skipped_because
    t.refresh_from_db()
    assert t.is_geocoded


@pytest.mark.django_db
def test_unavailable_on_a_fresh_row_becomes_pending_with_a_timestamp():
    t = _tenant()
    down = GeocodeResult(outcome=Outcome.UNAVAILABLE, provider="fake", reason="timeout")
    a = apply_result(t, down, source_address=t.address)
    assert a.status == GeocodeStatus.PENDING
    t.refresh_from_db()
    assert t.geocode_status == GeocodeStatus.PENDING
    assert t.geocoded_at is not None  # «звали час назад и сервис лежал» ≠ «не звали»
    assert not t.is_geocoded


@pytest.mark.django_db
def test_zero_zero_from_a_provider_is_refused_and_nothing_is_saved():
    """Правило модели из #333 срабатывает на пути записи, а не только в
    admin-форме: адаптер идёт через full_clean, не через update()."""
    t = _tenant()
    with pytest.raises(ValidationError):
        apply_result(t, _found(lat=D("0"), lng=D("0")), source_address=t.address)
    fresh = Tenant.objects.get(pk=t.pk)
    assert fresh.geocode_status == GeocodeStatus.NOT_ATTEMPTED and fresh.latitude is None


@pytest.mark.django_db
def test_misconfigured_never_reaches_the_write_path():
    t = _tenant()
    r = GeocodeResult(outcome=Outcome.MISCONFIGURED, provider="fake", reason="ключа нет")
    with pytest.raises(ValueError, match="check\\(\\)"):
        apply_result(t, r, source_address=t.address)


# ---------------------------------------------------------------------------
# Провайдеры и закрытый список модулей
# ---------------------------------------------------------------------------


def test_yandex_is_a_stub_that_names_the_price_before_the_key():
    p = YandexGeocoder()
    refusal = p.check()
    assert refusal is not None and refusal.outcome is Outcome.MISCONFIGURED
    assert "226 200" in refusal.reason
    with pytest.raises(NotImplementedError, match="226 200"):
        p.geocode("ул Кирова, д 20")
    assert "226 200" in LICENCE_NOTE


def test_unknown_provider_names_the_known_ones():
    with pytest.raises(ValueError, match="yandex"):
        get_provider("google")
    assert "yandex" in PROVIDERS


def test_the_set_of_modules_that_talk_to_geocoders_is_closed():
    """Два геокодера без связи уже есть; третьего не распутает никто.

    Положительный контроль: сканер обязан найти ``providers/dadata.py`` —
    иначе зелень означала бы «сканер слеп», а не «лишних нет».
    """
    root = Path(__file__).resolve().parents[2]
    found = set()
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if "/tests/" in rel or rel.startswith("tests/") or "/migrations/" in rel:
            continue
        if any(part in (".venv", "venv", "node_modules") for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(marker in text for marker in GEOCODER_HOST_MARKERS):
            found.add(rel)
    assert "core/geocoding/providers/dadata.py" in found, "положительный контроль: сканер слеп"
    # DRF-1685: старый модуль больше не ходит к геокодеру сам — и сканер
    # обязан это ПОДТВЕРДИТЬ, а не просто не жаловаться.
    assert "services/geocoding.py" not in found, "services/geocoding.py снова зовёт геокодер напрямую"
    extra = found - GEOCODING_MODULES
    assert not extra, (
        "появился модуль, который ходит к геокодеру и не объявлен в GEOCODING_MODULES: "
        + ", ".join(sorted(extra))
    )
