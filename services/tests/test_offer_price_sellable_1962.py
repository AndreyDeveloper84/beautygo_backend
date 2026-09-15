"""DRF-1962 — предложение с ценой ниже 1 ₽ не продаётся (решение владельца 15.09, §10).

«0 ₽ не является реальной продажной ценой»: не «бесплатно», не 1 ₽, валидатор
не ослабляется, цена не выдумывается. Маппинг может существовать, но
предложение исключено из продаваемого каталога, записи и исполнения
рекомендаций, пока реальная цена не станет ≥ 1.

Цена правила — цена ребра ``SpecialistService.price`` (D4: авторитетна опция,
которую видел клиент; её же снимает запись). ``SalonService.base_price`` в
правиле не участвует.

До правки цену не смотрело ничто на пути продажи: предложение за 0 ₽
показывалось, давало слоты, бронировалось (сбор 0) и допускалось в
рекомендации. Замер пилота 15.09 16:25 UTC: 9 активных таких предложений
(formula-tela 4, mkt-afrodita 5), VERIFIED среди них 0.

Сторожа:

* одно определение ``services.offer_sellable`` — ребро активно, салонная
  услуга активна, цена ≥ 1; легаси-услуга — активна и цена ≥ 1;
* поверхности: резолвер (слоты, запись, инструмент ИИ) отказывает с именем
  причины; запись — 422 ``SERVICE_NOT_ACTIVE`` с ``details.reason``;
  список услуг мастера, поиск, факты кандидата рекомендаций — без такого
  предложения; зеркало для бота — строка остаётся с ``sellable: false`` и
  ``unsellable_reason``;
* положительная пара на каждой поверхности — предложение за 1500 ₽ продаётся;
* маппинг услуги не трогается;
* перепись: сырой фильтр продажи предложений вне определения — красный.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment
from services.models import SalonService, ServiceCategory, SpecialistService
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

REPO = Path(__file__).resolve().parents[2]
TOKEN = "test-internal-token-1962"  # pragma: allowlist secret
BOT_ID = "bot:1962"
REASON = "price_below_minimum"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = TOKEN


def _future_iso(hours: int = 3) -> str:
    dt = (datetime.now(tz=dt_timezone.utc) + timedelta(hours=hours)).replace(second=0, microsecond=0)
    return dt.replace(minute=dt.minute - (dt.minute % 30)).isoformat()


def _offer(slug: str, name: str, edge_price: str, phone: str) -> tuple[SpecialistProfile, SalonService, SpecialistService]:
    tenant = Tenant.objects.create(slug=slug, name=f"Salon {slug}")
    u = User.objects.create_user(
        username=f"{slug}-master", password="x",  # pragma: allowlist secret
        role="specialist", phone=phone,
    )
    sp = SpecialistProfile.objects.get(user=u)
    sp.tenant = tenant
    sp.display_name = f"Master {slug}"
    sp.status = SpecialistProfile.ProfileStatus.ACTIVE
    sp.is_available = True
    sp.is_booking_enabled = True
    sp.timezone = "Europe/Moscow"
    sp.save()
    category = ServiceCategory.objects.create(name=f"Cat {slug}", slug=f"cat-{slug}")
    salon = SalonService.objects.create(
        tenant=tenant, category=category, name=name, duration_minutes=60,
        base_price=None, is_active=True, requires_health_check=False,
    )
    # Цена ребра записана мимо валидатора — так она лежит на пилоте.
    edge = SpecialistService.objects.create(
        salon_service=salon, specialist=sp, duration_minutes=60,
        price=Decimal(edge_price), buffer_after_minutes=0, is_active=True,
    )
    return sp, salon, edge


@pytest.fixture
def zero():
    return _offer("p1962-zero", "Массаж зоны 1962 ноль", "0.00", "+79991962001")


@pytest.fixture
def paid():
    return _offer("p1962-paid", "Массаж зоны 1962 платный", "1500.00", "+79991962002")


def _bot() -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = BOT_ID
    return c


def _customer() -> User:
    return User.objects.create_user(
        username=BOT_ID, password="x",  # pragma: allowlist secret
        role="client", phone="+79991962003", is_proxy=True,
    )


def _client_user() -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=User.objects.create_user(
        username="p1962-client", password="x",  # pragma: allowlist secret
        role="client", phone="+79991962004",
    ))
    return c


# ---------------------------------------------------------------------------
# Определение
# ---------------------------------------------------------------------------


class TestTheDefinition:
    def test_the_minimum_is_one_rouble_and_the_reason_has_a_name(self):
        from services.offer_sellable import OFFER_MIN_PRICE, PRICE_BELOW_MINIMUM, offer_refusal

        assert OFFER_MIN_PRICE == Decimal("1")
        assert PRICE_BELOW_MINIMUM == REASON
        assert offer_refusal(Decimal("0.00")) == REASON
        assert offer_refusal(Decimal("0.99")) == REASON
        assert offer_refusal(Decimal("1.00")) is None
        assert offer_refusal(Decimal("1500.00")) is None


# ---------------------------------------------------------------------------
# Резолвер — слоты, запись и инструмент ИИ идут через него
# ---------------------------------------------------------------------------


class TestTheResolver:
    def test_a_zero_price_edge_is_refused_with_the_reason(self, zero):
        from services.service_resolver import ServiceUnavailableForSpecialistError, resolve_bookable_service

        sp, salon, _ = zero
        with pytest.raises(ServiceUnavailableForSpecialistError) as exc:
            resolve_bookable_service(service_id=salon.id, specialist=sp, tenant=sp.tenant)
        assert getattr(exc.value, "reason", None) == REASON

    def test_a_paid_edge_resolves(self, paid):
        from services.service_resolver import resolve_bookable_service

        sp, salon, _ = paid
        resolved = resolve_bookable_service(service_id=salon.id, specialist=sp, tenant=sp.tenant)
        assert resolved.price == Decimal("1500.00")


# ---------------------------------------------------------------------------
# Запись — внутренний REST бота
# ---------------------------------------------------------------------------


class TestBooking:
    def test_booking_a_zero_price_offer_is_422_with_the_reason_and_creates_nothing(self, zero):
        sp, salon, _ = zero
        customer = _customer()

        r = _bot().post("/api/v1/internal/appointments/", {
            "client_id": str(customer.id), "specialist_id": str(sp.id),
            "service_id": str(salon.id), "start_datetime": _future_iso(3),
        }, format="json")

        assert r.status_code == 422, r.content
        err = r.json()["error"]
        assert err["code"] == "SERVICE_NOT_ACTIVE"
        assert (err.get("details") or {}).get("reason") == REASON
        assert not Appointment.objects.filter(specialist=sp).exists()

    def test_booking_a_paid_offer_is_201(self, paid):
        sp, salon, _ = paid
        customer = _customer()

        r = _bot().post("/api/v1/internal/appointments/", {
            "client_id": str(customer.id), "specialist_id": str(sp.id),
            "service_id": str(salon.id), "start_datetime": _future_iso(3),
        }, format="json")

        assert r.status_code == 201, r.content


# ---------------------------------------------------------------------------
# Слоты — внутренний REST бота
# ---------------------------------------------------------------------------


class TestSlots:
    @staticmethod
    def _slots(sp, salon):
        date = (datetime.now(tz=dt_timezone.utc) + timedelta(days=1)).date().isoformat()
        return _bot().get(f"/api/v1/internal/specialists/{sp.id}/slots/?service_id={salon.id}&date={date}")

    def test_a_zero_price_offer_has_no_slots(self, zero):
        sp, salon, _ = zero
        assert self._slots(sp, salon).status_code == 404

    def test_a_paid_offer_answers_slots(self, paid):
        sp, salon, _ = paid
        assert self._slots(sp, salon).status_code == 200


# ---------------------------------------------------------------------------
# Продаваемый каталог
# ---------------------------------------------------------------------------


class TestCatalogSurfaces:
    def test_the_masters_services_list_omits_a_zero_price_offer(self, zero):
        sp, salon, _ = zero
        r = _client_user().get(f"/api/v1/specialists/{sp.id}/services/")
        assert r.status_code == 200, r.content
        assert str(salon.id) not in r.content.decode("utf-8")

    def test_the_masters_services_list_keeps_a_paid_offer(self, paid):
        sp, salon, _ = paid
        r = _client_user().get(f"/api/v1/specialists/{sp.id}/services/")
        assert r.status_code == 200, r.content
        assert str(salon.id) in r.content.decode("utf-8")

    def test_search_does_not_find_a_zero_price_offer(self, zero):
        sp, salon, _ = zero
        r = _client_user().get("/api/v1/search/", {"q": "1962 ноль", "type": "services"})
        assert r.status_code == 200, r.content
        assert str(salon.id) not in r.content.decode("utf-8")

    def test_search_finds_a_paid_offer(self, paid):
        sp, salon, _ = paid
        r = _client_user().get("/api/v1/search/", {"q": "1962 платный", "type": "services"})
        assert r.status_code == 200, r.content
        assert str(salon.id) in r.content.decode("utf-8")

    def test_a_recommendation_candidate_with_only_a_zero_price_offer_has_no_offer(self, zero):
        from users.recommendation_source import SpecialistCandidateSource, _MappingFacts

        sp, _, _ = zero
        facts = SpecialistCandidateSource()._facts_for(
            SpecialistProfile.objects.get(pk=sp.pk), mapping=_MappingFacts(), needle="", goal_categories=None,
        )
        assert facts.is_active_offer is False
        assert facts.is_capable is False


class TestTheBotMirror:
    URL = "/api/v1/internal/catalog/specialist-services/"

    def _rows(self, sp):
        c = APIClient()
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
        r = c.get(f"{self.URL}?specialist={sp.id}")
        assert r.status_code == 200, r.content
        results = r.json().get("data", r.json())
        return results.get("results", results) if isinstance(results, dict) else results

    def test_a_zero_price_edge_stays_in_the_mirror_marked_not_sellable(self, zero):
        sp, _, edge = zero
        row = next(item for item in self._rows(sp) if item["id"] == str(edge.id))
        assert row["sellable"] is False
        assert row["unsellable_reason"] == REASON

    def test_a_paid_edge_is_sellable_in_the_mirror(self, paid):
        sp, _, edge = paid
        row = next(item for item in self._rows(sp) if item["id"] == str(edge.id))
        assert row["sellable"] is True
        assert row["unsellable_reason"] is None


class TestTheMappingIsKept:
    def test_refusing_and_listing_do_not_touch_the_mapping(self, zero):
        from services.service_resolver import ServiceUnavailableForSpecialistError, resolve_bookable_service

        sp, salon, _ = zero
        before = SalonService.objects.values_list("mapping_status", "template_id").get(pk=salon.pk)
        try:
            resolve_bookable_service(service_id=salon.id, specialist=sp, tenant=sp.tenant)
        except ServiceUnavailableForSpecialistError:
            pass
        _client_user().get(f"/api/v1/specialists/{sp.id}/services/")
        assert SalonService.objects.values_list("mapping_status", "template_id").get(pk=salon.pk) == before


# ---------------------------------------------------------------------------
# Перепись: продажа предложений идёт через одно определение
# ---------------------------------------------------------------------------

RAW = re.compile(
    r"(?:\w+__)?salon_service__is_active\s*=\s*True"
    r"|specialist_services__is_active\s*=\s*True"
    r"|\b_ACTIVE_CANONICAL\b"
)
SKIP_PARTS = {"tests", "migrations", "commands", "seeds", "venv", ".venv", "node_modules"}

#: Where the raw form may stay, each with its reason.
ALLOWED = {
    "services/offer_sellable.py": "the definition itself",
}

#: Modules that sell offers and must go through the definition (a lower bound).
CENSUS = [
    "services/catalog_reads.py",
    "search/views.py",
    "users/recommendation_source.py",
    "users/specialists_api.py",
    "ai/application/services/recommendation_engine.py",
    "users/catalog_recommendations_api.py",
]


def _raw_hits() -> dict[str, int]:
    out: dict[str, int] = {}
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO).as_posix()
        if set(rel.split("/")) & SKIP_PARTS or rel.endswith("admin.py"):
            continue
        found = RAW.findall(path.read_text(encoding="utf-8", errors="ignore"))
        if found:
            out[rel] = len(found)
    return out


class TestTheOfferCensus:
    def test_the_pattern_catches_every_spelling_it_names(self):
        for sample in (
            "salon_service__is_active=True",
            "specialist_services__salon_service__is_active = True",
            "specialist_services__is_active=True",
            "filter=_ACTIVE_CANONICAL",
        ):
            assert RAW.search(sample), sample
        assert not RAW.search("salon_service__is_active=False")

    def test_the_scan_is_not_vacuous(self):
        assert (REPO / "services" / "offer_sellable.py").exists(), REPO
        for module in CENSUS:
            text = (REPO / module).read_text(encoding="utf-8")
            assert "sellable_offer_q(" in text or "offer_refusal(" in text, module

    def test_no_raw_offer_sale_filter_outside_the_definition(self):
        stray = {rel: n for rel, n in _raw_hits().items() if rel not in ALLOWED}
        assert stray == {}, (
            "raw offer sale filter outside services/offer_sellable.py — use sellable_offer_q(): " f"{stray}"
        )
        for rel, reason in ALLOWED.items():
            assert reason.strip(), rel
