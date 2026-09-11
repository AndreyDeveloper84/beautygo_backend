"""Команда ``geocode_tenants``: сухой прогон по умолчанию, отказ до первого запроса.

Провайдер здесь — подставной и **считает вызовы**. Это главное: лицензия
DaData считает запросы, и «команда отказалась» обязано значить «ни одного
запроса не ушло», а не «ушло десять, потом отказалась».
"""
from __future__ import annotations

from decimal import Decimal as D
from io import StringIO

import pytest
from django.core.management import call_command

from core.geocoding.contract import GeocodeResult, Outcome, Precision
from core.geocoding.providers import PROVIDERS
from tenants.management.commands import geocode_tenants as cmd_module
from tenants.models import GeocodeStatus, Tenant

pytestmark = pytest.mark.django_db

PENZA_LAT, PENZA_LNG = D("53.195878"), D("45.018316")


class FakeGeocoder:
    """Отвечает по словарю адрес → результат; всё остальное — NOT_FOUND."""

    name = "fake"
    calls: list[str] = []
    script: dict[str, GeocodeResult] = {}
    refuse: str | None = None

    def check(self):
        if self.refuse:
            return GeocodeResult(outcome=Outcome.MISCONFIGURED, provider=self.name, reason=self.refuse)
        return None

    def geocode(self, address: str, *, city: str = "") -> GeocodeResult:
        FakeGeocoder.calls.append(address)
        return self.script.get(
            address, GeocodeResult(outcome=Outcome.NOT_FOUND, provider=self.name),
        )


def _found(locality="г Пенза"):
    return GeocodeResult(
        outcome=Outcome.FOUND, provider="fake", latitude=PENZA_LAT, longitude=PENZA_LNG,
        normalized_address="г Пенза, ул Кирова, д 20", precision=Precision.HOUSE,
        provider_precision="0", locality=locality,
    )


@pytest.fixture
def fake(monkeypatch):
    FakeGeocoder.calls = []
    FakeGeocoder.script = {}
    FakeGeocoder.refuse = None
    monkeypatch.setitem(PROVIDERS, "fake", FakeGeocoder)
    return FakeGeocoder


def _run(*args, **kw):
    out, err = StringIO(), StringIO()
    code = 0
    try:
        call_command("geocode_tenants", *args, stdout=out, stderr=err, **kw)
    except SystemExit as exc:
        code = exc.code
    return code, out.getvalue(), err.getvalue()


def _tenant(slug, address, **kw):
    return Tenant.objects.create(name=slug, slug=slug, address=address, city="Пенза", **kw)


def test_dry_run_is_the_default_and_writes_nothing(fake):
    t = _tenant("salon-a", "ул Кирова, д 20")
    fake.script["ул Кирова, д 20"] = _found()

    code, out, _ = _run(provider="fake")

    assert code == 0
    assert "СУХОЙ ПРОГОН" in out and "не записано ничего" in out
    assert "  ok            : 1" in out  # посчитано то, что БЫЛО БЫ записано
    t.refresh_from_db()
    assert t.geocode_status == GeocodeStatus.NOT_ATTEMPTED and t.latitude is None
    assert fake.calls == ["ул Кирова, д 20"]


def test_apply_writes_and_counts_by_all_six_statuses(fake):
    _tenant("s-ok", "ул Кирова, д 20")
    _tenant("s-amb", "ул Ладожская, д 130")
    _tenant("s-fail", "нет такого адреса")
    _tenant("s-down", "ул Лежит, д 1")
    fake.script["ул Кирова, д 20"] = _found()
    fake.script["ул Ладожская, д 130"] = _found(locality="г Кузнецк")   # не там
    fake.script["ул Лежит, д 1"] = GeocodeResult(outcome=Outcome.UNAVAILABLE, provider="fake", reason="timeout")

    code, out, _ = _run(provider="fake", apply=True)

    assert code == 1  # есть строки, ждущие человека
    assert "  ok            : 1" in out
    assert "  ambiguous     : 1" in out
    assert "  failed        : 1" in out
    assert "  pending       : 1" in out
    assert "ЖДУТ ЧЕЛОВЕКА: 1" in out
    assert Tenant.objects.get(slug="s-ok").is_geocoded
    assert Tenant.objects.get(slug="s-amb").geocode_status == GeocodeStatus.AMBIGUOUS
    assert Tenant.objects.get(slug="s-fail").geocode_status == GeocodeStatus.FAILED
    assert Tenant.objects.get(slug="s-down").geocode_status == GeocodeStatus.PENDING


def test_a_provider_that_is_not_ready_stops_before_the_first_request(fake):
    """«Команда отказалась» = «ни одного запроса не ушло»."""
    _tenant("s-a", "ул Кирова, д 20")
    fake.refuse = "ключ DADATA_API_KEY не задан"

    code, _, err = _run(provider="fake", apply=True)

    assert code == 2
    assert "ни одного запроса не сделано" in err and "ключ" in err
    assert fake.calls == []
    assert Tenant.objects.get(slug="s-a").geocode_status == GeocodeStatus.NOT_ATTEMPTED


def test_yandex_stub_is_refused_with_the_price_and_makes_no_call():
    _tenant("s-a", "ул Кирова, д 20")
    code, _, err = _run(provider="yandex", apply=True)
    assert code == 2 and "226 200" in err


def test_batch_above_the_ceiling_is_refused_before_any_request(fake, monkeypatch):
    """Порог — про число, не про способ: тысяча адресов из импорта это
    «автоматическая обработка» по п. 4.2.1, сколько бы их ни ввели люди."""
    monkeypatch.setattr(cmd_module, "BATCH_CEILING", 2)
    for i in range(3):
        _tenant(f"s{i}", f"ул Тестовая, д {i}")

    code, _, err = _run(provider="fake")

    assert code == 2 and "4.2.1" in err
    assert fake.calls == []


def test_rows_without_an_address_are_not_sent_and_are_counted(fake):
    """formula-tela: настоящий салон, адрес пуст намеренно. Геокодеру
    нечего дать, и pending на пустой строке выглядел бы как лежащий сервис."""
    _tenant("with-addr", "ул Кирова, д 20")
    _tenant("no-addr", "")  # как formula-tela на пилоте; тот slug занят миграцией
    fake.script["ул Кирова, д 20"] = _found()

    # --slug: в тестовой базе уже сидят тенанты из миграций без адреса, и
    # счётчик «без адреса» посчитал бы их вместе с нашим.
    code, out, _ = _run(provider="fake", slug=["with-addr", "no-addr"])

    assert fake.calls == ["ул Кирова, д 20"]
    assert "без адреса=1" in out and "  без адреса     : 1" in out


def test_confirmed_rows_are_skipped_and_the_reason_is_counted(fake):
    _tenant("human", "ул Кирова, д 20", geocode_status=GeocodeStatus.CONFIRMED,
            latitude=PENZA_LAT, longitude=PENZA_LNG, geocode_provider="manual")
    fake.script["ул Кирова, д 20"] = _found()

    code, out, _ = _run(provider="fake", apply=True)

    assert "пропуск — подтверждено человеком" in out
    assert Tenant.objects.get(slug="human").geocode_provider == "manual"


def test_slug_filter_narrows_the_run(fake):
    _tenant("s-a", "ул Кирова, д 20")
    _tenant("s-b", "ул Ладожская, д 130")
    _run(provider="fake", slug=["s-b"])
    assert fake.calls == ["ул Ладожская, д 130"]


def test_subject_is_printed_before_the_run_header(fake):
    _tenant("s-a", "ул Кирова, д 20")
    _, out, _ = _run(provider="fake")
    assert out.index("== ПРЕДМЕТ") < out.index("== ПРОГОН")


def test_missing_provider_argument_is_an_error_not_a_default(fake):
    """Провайдер по умолчанию подставил бы того, кого никто не выбирал."""
    _tenant("s-a", "ул Кирова, д 20")
    code, _, err = _run()
    assert code == 2 and "--provider обязателен" in err
    assert fake.calls == []


def test_a_key_rejected_mid_run_stops_the_run_and_keeps_what_was_written(fake):
    """Отклонённый ключ — про нас, не про сервис. Остальные строки не
    получают pending, который выглядел бы как лежащий сервис."""
    _tenant("s-first", "ул Кирова, д 20")
    _tenant("s-second", "ул Ладожская, д 130")
    fake.script["ул Кирова, д 20"] = _found()
    fake.script["ул Ладожская, д 130"] = GeocodeResult(
        outcome=Outcome.MISCONFIGURED, provider="fake", reason="DaData отклонила ключ (HTTP 403)",
    )

    code, _, err = _run(provider="fake", apply=True)

    assert code == 2 and "отклонила ключ" in err and "записано строк: 1" in err
    assert Tenant.objects.get(slug="s-first").is_geocoded
    assert Tenant.objects.get(slug="s-second").geocode_status == GeocodeStatus.NOT_ATTEMPTED
