"""M12a (DRF-1804) — подсказки адреса места мастера через DaData, без сети в тестах.

Что стережётся:

* подсказки — в городе workspace мастера; без города провайдер не вызывается;
* не 500 ни при одном сбое: пустая настройка провайдера, пустой ключ, чужой
  провайдер, лимит, сеть, неожиданное исключение — 503 по имени, экран падает
  в ручной ввод;
* «отказал до запроса» значит ноль запросов — подставная сессия считает вызовы;
* ввод мастера (адрес, часто домашний) не остаётся ни в логах, ни в ответе
  об ошибке — ни при сбое сети, ни при исключении провайдера с вводом в тексте;
* журнал §96 не пишется: ввод не хранится и о субъекте не читается;
* субъект: чужой workspace — 403, мастер салона — 409 по имени.

Каждый отказ — рядом с положительной половиной на тех же данных.
"""
from __future__ import annotations

import logging
import uuid

import pytest
import requests
from rest_framework.test import APIClient

from core.geocoding.providers.dadata import SUGGEST_COUNT_MAX, SUGGEST_URL, DaDataGeocoder
from core.tests.test_geocoding_providers import FakeSession, _dd, _Resp
from privacy_audit.models import PersonalDataAccessLog
from tenants.models import Tenant
from tenants.solo_provisioning import provision_solo_workspace
from users import internal_address_suggest_api
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token-1804"  # noqa: S105
OLGA = "bot:max:1804001"
IRINA = "bot:max:1804002"
URL = "/api/v1/internal/specialists/{sid}/geocoding/suggest/"
HOME = "Пушкина 45 кв 12"


@pytest.fixture(autouse=True)
def _settings(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN
    settings.GEOCODING_PROVIDER = "dadata"
    settings.DADATA_API_KEY = "k"


def _provision(external_user_id: str, slug: str, city: str = "Пенза") -> SpecialistProfile:
    return provision_solo_workspace(
        tenant_id=uuid.uuid4(), slug=slug, name=f"Студия {slug}", city=city,
        external_user_id=external_user_id, display_name="Мастер",
    ).profile


@pytest.fixture
def olga() -> SpecialistProfile:
    return _provision(OLGA, "solo-max-1804olga")


@pytest.fixture
def irina() -> SpecialistProfile:
    return _provision(IRINA, "solo-max-1804irin")


@pytest.fixture
def session(monkeypatch) -> FakeSession:
    """Подставная сессия DaData; фабрика провайдера считает, сколько раз её звали."""
    fake = FakeSession()
    fake.built = 0

    def build() -> DaDataGeocoder:
        fake.built += 1
        return DaDataGeocoder(session=fake)

    monkeypatch.setattr(internal_address_suggest_api, "_provider", build)
    return fake


#: Логгеры приложения с ``propagate: False`` (settings.LOGGING): ``caplog`` висит на
#: корне и их записей не видит. Без обработчика на них «адреса нет в логах»
#: проходило бы на пустом захвате — положительная стража это и поймала.
_APP_LOGGERS = ("users", "core", "django", "django.request", "privacy_audit", "tenants", "services")


@pytest.fixture
def app_logs(caplog):
    loggers = [logging.getLogger(name) for name in _APP_LOGGERS]
    saved = [(lg, lg.level) for lg in loggers]
    for lg in loggers:
        lg.addHandler(caplog.handler)
        lg.setLevel(logging.DEBUG)
    try:
        yield caplog
    finally:
        for lg, level in saved:
            lg.removeHandler(caplog.handler)
            lg.setLevel(level)


def _post(profile: SpecialistProfile, body, actor: str = OLGA):
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RUNTIME_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = actor
    return c.post(URL.format(sid=profile.pk), body, format="json")


def _reason(resp) -> str:
    return resp.json()["error"]["details"]["reason"]


class TestProviderSuggest:
    def test_suggestions_are_values_in_the_given_city_and_the_count_is_capped(self):
        s = FakeSession(_Resp(200, {"suggestions": [_dd("ул Пушкина, д 45", "53.19", "45.01", 0, "45")]}))

        got = DaDataGeocoder(api_key="k", session=s).suggest(HOME, city="Пенза", count=50)

        assert [(g.value, g.unrestricted_value) for g in got] == [("ул Пушкина, д 45", "Пенза, ул Пушкина, д 45")]
        [call] = s.calls
        assert call["url"] == SUGGEST_URL
        assert call["json"] == {"query": HOME, "count": SUGGEST_COUNT_MAX, "locations": [{"city": "Пенза"}]}

    def test_without_a_city_the_provider_is_not_called(self):
        s = FakeSession()
        with pytest.raises(ValueError):
            DaDataGeocoder(api_key="k", session=s).suggest(HOME, city="  ")
        assert s.calls == []
        # Положительная половина: с городом — один запрос.
        s.responses.append(_Resp(200, {"suggestions": []}))
        DaDataGeocoder(api_key="k", session=s).suggest(HOME, city="Пенза")
        assert len(s.calls) == 1

    def test_empty_key_refuses_before_any_request(self):
        s = FakeSession()
        got = DaDataGeocoder(api_key="", session=s).suggest(HOME, city="Пенза")
        assert got.outcome.value == "misconfigured"
        assert s.calls == []


class TestSuggest:
    def test_suggestions_come_from_the_masters_city(self, olga, session):
        session.responses.append(_Resp(200, {"suggestions": [_dd("ул Пушкина, д 45", "53.19", "45.01", 0, "45")]}))

        resp = _post(olga, {"q": f"  {HOME}  "})

        assert resp.status_code == 200, resp.content
        assert resp.json()["data"] == {
            "city": "Пенза",
            "suggestions": [{"value": "ул Пушкина, д 45", "unrestricted_value": "Пенза, ул Пушкина, д 45"}],
        }
        [call] = session.calls
        assert call["json"]["query"] == HOME and call["json"]["locations"] == [{"city": "Пенза"}]

    def test_nothing_found_is_an_empty_list_not_an_error(self, olga, session):
        session.responses.append(_Resp(200, {"suggestions": []}))

        resp = _post(olga, {"q": HOME})

        assert resp.status_code == 200, resp.content
        assert resp.json()["data"]["suggestions"] == []

    def test_empty_key_is_503_without_a_request(self, olga, session, settings):
        settings.DADATA_API_KEY = ""

        resp = _post(olga, {"q": HOME})

        assert resp.status_code == 503
        assert _reason(resp) == "misconfigured"
        assert session.calls == []

    def test_empty_provider_setting_is_503_not_500_and_no_provider_is_built(self, olga, session, settings):
        settings.GEOCODING_PROVIDER = ""

        resp = _post(olga, {"q": HOME})

        assert resp.status_code == 503
        assert _reason(resp) == "misconfigured"
        assert session.built == 0 and session.calls == []

    def test_another_provider_is_named_not_supported(self, olga, session, settings):
        settings.GEOCODING_PROVIDER = "nominatim"

        resp = _post(olga, {"q": HOME})

        assert resp.status_code == 503
        assert _reason(resp) == "suggest_not_supported"
        assert session.built == 0

    def test_workspace_without_a_city_is_409_without_a_request(self, olga, session):
        Tenant.all_objects.filter(pk=olga.tenant_id).update(city="")

        resp = _post(olga, {"q": HOME})

        assert resp.status_code == 409
        assert _reason(resp) == "no_city"
        assert session.built == 0 and session.calls == []

    def test_short_query_or_extra_field_is_400_without_a_request(self, olga, session):
        assert _post(olga, {"q": "ул"}).status_code == 400
        assert _post(olga, {"q": HOME, "city": "Москва"}).status_code == 400
        assert _post(olga, {"q": "я" * 301}).status_code == 400
        assert session.built == 0 and session.calls == []
        session.responses.append(_Resp(200, {"suggestions": []}))
        assert _post(olga, {"q": HOME}).status_code == 200

    @pytest.mark.parametrize("status", [429, 500, 502])
    def test_limit_or_outage_is_503(self, olga, session, status):
        session.responses.append(_Resp(status))

        resp = _post(olga, {"q": HOME})

        assert resp.status_code == 503
        assert _reason(resp) == "unavailable"


class TestTheQueryLeavesNoTrace:
    def test_network_failure_keeps_the_address_out_of_logs_and_the_response(self, olga, session, app_logs):
        session.raise_exc = requests.ConnectionError(f"connection reset while sending {HOME}")

        resp = _post(olga, {"q": HOME})

        assert resp.status_code == 503
        assert "internal.address_suggest.unavailable" in app_logs.text
        assert HOME not in app_logs.text
        assert HOME not in resp.content.decode("utf-8")

    def test_unexpected_provider_exception_is_503_and_leaves_no_trace(self, olga, monkeypatch, app_logs):
        class _Broken:
            def suggest(self, query, *, city):
                raise RuntimeError(f"provider exploded on {query}")

        monkeypatch.setattr(internal_address_suggest_api, "_provider", _Broken)

        resp = _post(olga, {"q": HOME})

        assert resp.status_code == 503
        assert _reason(resp) == "unavailable"
        assert "internal.address_suggest.provider_error" in app_logs.text
        assert HOME not in app_logs.text
        assert HOME not in resp.content.decode("utf-8")


class TestJournal:
    def test_suggest_writes_no_journal_row(self, olga, session):
        before = PersonalDataAccessLog.objects.count()
        session.responses.append(_Resp(200, {"suggestions": []}))

        resp = _post(olga, {"q": HOME})

        assert resp.status_code == 200
        assert PersonalDataAccessLog.objects.count() == before


class TestSubject:
    def test_foreign_workspace_is_403_own_is_200(self, olga, irina, session):
        assert _post(olga, {"q": HOME}, actor=IRINA).status_code == 403
        assert session.calls == []

        session.responses.append(_Resp(200, {"suggestions": []}))
        assert _post(olga, {"q": HOME}).status_code == 200

    def test_salon_master_is_refused_by_name(self, olga, session):
        salon = Tenant.objects.create(slug="salon-1804", name="Салон 1804", city="Пенза", kind=Tenant.Kind.SALON)
        user = User.objects.create_user(
            username="salon-master-1804",
            password="x",  # pragma: allowlist secret
            role="specialist",
            phone="+79990001804",
        )
        SpecialistProfile.objects.filter(user=user).update(tenant=salon, display_name="Мастер салона")
        master = SpecialistProfile.objects.get(user=user)
        header = f"bot:test:{user.pk.hex[:12]}"
        User.objects.get_or_create(
            username=header, defaults={"role": "client", "is_proxy": True, "is_guest": False, "linked_user": user},
        )

        resp = _post(master, {"q": HOME}, actor=header)

        assert resp.status_code == 409
        assert _reason(resp) == "salon_place_owner_managed"
        assert session.calls == []
        session.responses.append(_Resp(200, {"suggestions": []}))
        assert _post(olga, {"q": HOME}).status_code == 200
