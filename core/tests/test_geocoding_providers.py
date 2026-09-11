"""DaData и Nominatim — без сети, через подставную сессию.

Живых запросов в тестах нет ни одного: сессия подставная и **считает
вызовы**, потому что «отказал до первого запроса» обязано значить ноль, а
не «один пробный». Ответы — в форме, которую отдают настоящие API (поля из
документации DaData ``suggest/address`` и Nominatim ``search?format=jsonv2``).
"""
from __future__ import annotations

from decimal import Decimal as D

import pytest
import requests

from core.geocoding.contract import Outcome, Precision, status_for
from core.geocoding.providers import GEOCODING_MODULES, PROVIDERS
from core.geocoding.providers.dadata import QC_GEO_TO_PRECISION, DaDataGeocoder
from core.geocoding.providers.nominatim import NominatimGeocoder


class _Resp:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code = status
        self._payload = payload
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Отдаёт заранее заданные ответы и запоминает, что у неё спрашивали."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.raise_exc: Exception | None = None

    def _take(self, **kw):
        self.calls.append(kw)
        if self.raise_exc:
            raise self.raise_exc
        return self.responses.pop(0)

    def post(self, url, **kw):
        return self._take(url=url, **kw)

    def get(self, url, **kw):
        return self._take(url=url, **kw)


# ---------------------------------------------------------------------------
# DaData
# ---------------------------------------------------------------------------


def _dd(value, lat, lon, qc_geo, house, city="Пенза"):
    return {
        "value": value, "unrestricted_value": f"{city}, {value}",
        "data": {"geo_lat": lat, "geo_lon": lon, "qc_geo": qc_geo, "house": house,
                 "city": city, "city_with_type": f"г {city}"},
    }


def test_dadata_refuses_before_any_request_when_the_key_is_empty():
    s = FakeSession()
    p = DaDataGeocoder(api_key="", session=s)
    r = p.check()
    assert r is not None and r.outcome is Outcome.MISCONFIGURED and "DADATA_API_KEY" in r.reason
    assert s.calls == []


def test_dadata_reads_the_key_from_declared_settings(settings):
    """Не getattr(..., ""): строка объявлена в base.py, и её отсутствие —
    ошибка кода. Здесь проверяется, что провайдер её читает."""
    settings.DADATA_API_KEY = "k"
    assert DaDataGeocoder(session=FakeSession()).check() is None
    settings.DADATA_API_KEY = ""
    assert DaDataGeocoder(session=FakeSession()).check() is not None


def test_dadata_found_maps_qc_geo_and_narrows_by_city():
    s = FakeSession(_Resp(200, {"suggestions": [_dd("ул Кирова, д 20", "53.19", "45.01", 0, "20")]}))
    r = DaDataGeocoder(api_key="k", session=s).geocode("ул Кирова, д 20", city="Пенза")

    assert r.outcome is Outcome.FOUND
    assert (r.latitude, r.longitude) == (D("53.19"), D("45.01"))
    assert r.precision is Precision.HOUSE and r.provider_precision == "0"
    assert r.locality == "Пенза" and r.normalized_address.startswith("Пенза, ")
    assert status_for(r, expected_city="Пенза").value == "ok"
    # один запрос, с фильтром по городу и count=2 (это ОДИН запрос)
    assert len(s.calls) == 1
    body = s.calls[0]["json"]
    assert body["locations"] == [{"city": "Пенза"}] and body["count"] == 2
    assert s.calls[0]["headers"]["Authorization"] == "Token k"


@pytest.mark.parametrize("qc_geo, precision", [
    ("0", Precision.HOUSE), ("1", Precision.HOUSE), ("2", Precision.STREET),
    ("3", Precision.LOCALITY), ("4", Precision.LOCALITY), ("5", Precision.UNKNOWN),
])
def test_dadata_qc_geo_table_is_complete(qc_geo, precision):
    assert QC_GEO_TO_PRECISION[qc_geo] is precision


def test_dadata_street_precision_is_ok_but_locality_is_ambiguous():
    """«Ладожская 130» → улица (qc_geo=2) — OK; qc_geo=3/4 — центр города — AMBIGUOUS."""
    s = FakeSession(
        _Resp(200, {"suggestions": [_dd("ул Ладожская", "53.2", "45.0", 2, "")]}),
        _Resp(200, {"suggestions": [_dd("Пенза", "53.2", "45.0", 4, "")]}),
    )
    p = DaDataGeocoder(api_key="k", session=s)
    assert status_for(p.geocode("ул Ладожская, д 130", city="Пенза"), expected_city="Пенза").value == "ok"
    assert status_for(p.geocode("Пенза", city="Пенза"), expected_city="Пенза").value == "ambiguous"


def test_dadata_twin_house_numbers_are_multiple_but_a_neighbour_is_not():
    """Два «д 20» на разных координатах — неоднозначность (человек).
    «д 20» и «д 20А» — нет: первый и есть ответ."""
    twins = [_dd("ул Кирова, д 20", "53.19", "45.01", 0, "20"), _dd("ул Кирова, д 20", "53.30", "45.20", 0, "20")]
    neighbour = [_dd("ул Кирова, д 20", "53.19", "45.01", 0, "20"), _dd("ул Кирова, д 20А", "53.19", "45.02", 0, "20А")]
    p = DaDataGeocoder(api_key="k", session=FakeSession(_Resp(200, {"suggestions": twins})))
    r = p.geocode("ул Кирова, д 20", city="Пенза")
    assert r.outcome is Outcome.MULTIPLE and r.latitude == D("53.19")  # справочные координаты первого
    p = DaDataGeocoder(api_key="k", session=FakeSession(_Resp(200, {"suggestions": neighbour})))
    assert p.geocode("ул Кирова, д 20", city="Пенза").outcome is Outcome.FOUND


def test_dadata_empty_suggestions_is_not_found_not_pending():
    p = DaDataGeocoder(api_key="k", session=FakeSession(_Resp(200, {"suggestions": []})))
    assert p.geocode("нет такого", city="Пенза").outcome is Outcome.NOT_FOUND


@pytest.mark.parametrize("status", [429, 500, 503])
def test_dadata_limit_or_outage_is_unavailable(status):
    p = DaDataGeocoder(api_key="k", session=FakeSession(_Resp(status)))
    r = p.geocode("ул Кирова, д 20", city="Пенза")
    assert r.outcome is Outcome.UNAVAILABLE and str(status) in r.reason


@pytest.mark.parametrize("status", [401, 403])
def test_dadata_rejected_key_is_misconfigured_not_unavailable(status):
    """Отклонённый ключ — про нас, не про сервис; pending на нём повторялся бы вечно."""
    p = DaDataGeocoder(api_key="bad", session=FakeSession(_Resp(status)))
    assert p.geocode("ул Кирова, д 20", city="Пенза").outcome is Outcome.MISCONFIGURED


def test_dadata_network_error_is_unavailable_not_an_exception():
    s = FakeSession()
    s.raise_exc = requests.ConnectionError("boom")
    r = DaDataGeocoder(api_key="k", session=s).geocode("ул Кирова, д 20", city="Пенза")
    assert r.outcome is Outcome.UNAVAILABLE and "сеть" in r.reason


def test_dadata_malformed_json_is_unavailable():
    p = DaDataGeocoder(api_key="k", session=FakeSession(_Resp(200, bad_json=True)))
    assert p.geocode("x", city="Пенза").outcome is Outcome.UNAVAILABLE


# ---------------------------------------------------------------------------
# Nominatim
# ---------------------------------------------------------------------------


def _nm(lat, lon, *, house="20", road="улица Кирова", city="Пенза", display=None):
    address = {"city": city}
    if road:
        address["road"] = road
    if house:
        address["house_number"] = house
    return {"lat": lat, "lon": lon, "display_name": display or f"{house}, {road}, {city}",
            "addresstype": "building" if house else "road", "address": address}


def test_nominatim_refuses_before_any_request_when_the_host_is_empty():
    s = FakeSession()
    r = NominatimGeocoder(base_url="", session=s).check()
    assert r is not None and r.outcome is Outcome.MISCONFIGURED and "NOMINATIM_BASE_URL" in r.reason
    assert s.calls == []


def test_nominatim_found_with_house_and_city_in_query():
    s = FakeSession(_Resp(200, [_nm("53.19", "45.01")]))
    r = NominatimGeocoder(base_url="https://nominatim.local", session=s).geocode("ул Кирова, д 20", city="Пенза")
    assert r.outcome is Outcome.FOUND and r.precision is Precision.HOUSE and r.locality == "Пенза"
    assert status_for(r, expected_city="Пенза").value == "ok"
    params = s.calls[0]["params"]
    assert params["q"].endswith(", Пенза") and params["limit"] == 2 and params["format"] == "jsonv2"
    assert "User-Agent" in s.calls[0]["headers"]


def test_nominatim_result_in_another_city_is_ambiguous_by_contract():
    """«Кирова 20» → Кузнецк: домовая точность, другой город."""
    s = FakeSession(_Resp(200, [_nm("53.12", "46.60", city="Кузнецк")]))
    r = NominatimGeocoder(base_url="https://nominatim.local", session=s).geocode("ул Кирова, д 20", city="Пенза")
    assert r.outcome is Outcome.FOUND and r.locality == "Кузнецк"
    assert status_for(r, expected_city="Пенза").value == "ambiguous"


def test_nominatim_street_only_is_street_precision():
    """«Ладожская 130» — только улица."""
    s = FakeSession(_Resp(200, [_nm("53.2", "45.0", house="", road="Ладожская улица")]))
    r = NominatimGeocoder(base_url="https://nominatim.local", session=s).geocode("ул Ладожская, д 130", city="Пенза")
    assert r.precision is Precision.STREET


def test_nominatim_twins_are_multiple():
    s = FakeSession(_Resp(200, [_nm("53.19", "45.01"), _nm("53.30", "45.20")]))
    r = NominatimGeocoder(base_url="https://nominatim.local", session=s).geocode("ул Кирова, д 20", city="Пенза")
    assert r.outcome is Outcome.MULTIPLE


def test_nominatim_empty_list_is_not_found_and_429_is_unavailable():
    p = NominatimGeocoder(base_url="https://nominatim.local", session=FakeSession(_Resp(200, [])))
    assert p.geocode("x", city="Пенза").outcome is Outcome.NOT_FOUND
    p = NominatimGeocoder(base_url="https://nominatim.local", session=FakeSession(_Resp(429)))
    assert p.geocode("x", city="Пенза").outcome is Outcome.UNAVAILABLE


def test_nominatim_public_host_is_throttled_to_one_request_per_second_and_own_host_is_not():
    """Политика публичного хоста соблюдается провайдером, а не надеждой."""
    slept: list[float] = []
    s = FakeSession(_Resp(200, []), _Resp(200, []))
    p = NominatimGeocoder(base_url="https://nominatim.openstreetmap.org", session=s, sleep=slept.append)
    p.geocode("a", city="Пенза")
    p.geocode("b", city="Пенза")
    assert len(slept) == 1 and 0 < slept[0] <= 1.0

    slept.clear()
    s = FakeSession(_Resp(200, []), _Resp(200, []))
    p = NominatimGeocoder(base_url="https://nominatim.local", session=s, sleep=slept.append)
    p.geocode("a", city="Пенза")
    p.geocode("b", city="Пенза")
    assert slept == []


# ---------------------------------------------------------------------------
# Реестр
# ---------------------------------------------------------------------------


def test_registry_order_is_dadata_first_nominatim_second_yandex_stub():
    assert list(PROVIDERS) == ["dadata", "nominatim", "yandex"]
    assert {"core/geocoding/providers/dadata.py", "core/geocoding/providers/nominatim.py"} <= GEOCODING_MODULES


# ---------------------------------------------------------------------------
# Обратное геокодирование (DRF-1685) — тем же провайдером
# ---------------------------------------------------------------------------


def test_dadata_reverse_returns_the_locality_via_geolocate():
    s = FakeSession(_Resp(200, {"suggestions": [_dd("ул Кирова, д 20", "53.19", "45.01", 0, "20")]}))
    r = DaDataGeocoder(api_key="k", session=s).reverse(53.19, 45.01)
    assert r.outcome is Outcome.FOUND and r.locality == "Пенза"
    assert s.calls[0]["url"].endswith("/geolocate/address")
    assert s.calls[0]["json"] == {"lat": 53.19, "lon": 45.01, "count": 1}


def test_dadata_reverse_shares_the_http_outcomes_with_forward():
    """Один разбор HTTP на оба направления: отклонённый ключ и здесь MISCONFIGURED."""
    assert DaDataGeocoder(api_key="bad", session=FakeSession(_Resp(403))).reverse(1, 1).outcome is Outcome.MISCONFIGURED
    assert DaDataGeocoder(api_key="k", session=FakeSession(_Resp(503))).reverse(1, 1).outcome is Outcome.UNAVAILABLE
    empty = FakeSession(_Resp(200, {"suggestions": []}))
    assert DaDataGeocoder(api_key="k", session=empty).reverse(1, 1).outcome is Outcome.NOT_FOUND


def test_dadata_reverse_without_a_city_in_the_answer_is_not_found():
    no_city = {"value": "Россия", "data": {"geo_lat": "1", "geo_lon": "1", "qc_geo": 5}}
    r = DaDataGeocoder(api_key="k", session=FakeSession(_Resp(200, {"suggestions": [no_city]}))).reverse(1, 1)
    assert r.outcome is Outcome.NOT_FOUND


def test_nominatim_reverse_returns_the_locality_and_respects_the_public_policy():
    slept: list[float] = []
    s = FakeSession(_Resp(200, _nm("53.19", "45.01")), _Resp(200, {"error": "Unable to geocode"}))
    p = NominatimGeocoder(base_url="https://nominatim.openstreetmap.org", session=s, sleep=slept.append)
    r = p.reverse(53.19, 45.01)
    assert r.outcome is Outcome.FOUND and r.locality == "Пенза"
    assert s.calls[0]["url"].endswith("/reverse") and s.calls[0]["params"]["lat"] == 53.19
    # объект с error — «ничего нет», и интервал между вызовами соблюдён
    assert p.reverse(0, 0).outcome is Outcome.NOT_FOUND
    assert len(slept) == 1
