"""«Мастер продаётся клиенту» — одно определение и его сторож (DRF-1845, K1a).

Перепись на 15.09 (dev 3a2cdb3): 7 выборок мастеров в не-тестовом коде, 4 из
них продавали без ``is_booking_enabled`` (публичный список, три выборки
поиска). Мастер на паузе оставался в выдаче, клиент упирался в отказ записи.

Что заперто:

* **класс**: в не-тестовом коде нет сырых ``is_available=True`` /
  ``ProfileStatus.ACTIVE`` вне ``users/sellable.py`` (и названного
  исключения — ворот записи); сторож проверен на самом себе и не пуст —
  предикат зовут не меньше пяти модулей переписи;
* **поверхности**: мастер на паузе — нет в публичном списке и в поиске
  (мастера и услуги); по прямой ссылке профиль открывается с
  ``accepting_bookings=false``; в фиде для бота он есть с
  ``is_booking_enabled=false`` (синк upsert-only — выпадение из фида
  оставило бы его старую активную строку);
* **положительная стража**: мастер, принимающий записи, есть везде.

Предел сторожа: он читает написание, а не смысл. Алиас
(``Status = SpecialistProfile.ProfileStatus``; ``Status.ACTIVE``) и фильтр,
собранный из переменных, ему не видны. Зелёный сторож не доказывает, что
все выборки для продажи идут через предикат. Вне класса законно:
проверка статуса одной строки, а не выборка мастеров, например
идемпотентность публикации в ``users/publication.py``.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
from rest_framework.test import APIClient

from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User

REPO = Path(__file__).resolve().parents[2]
RAW = re.compile(r"(?:\w+__)?is_available\s*=\s*True|ProfileStatus\.ACTIVE")
#: `.claude` — рабочие деревья. Без него сканер видит 28 вложенных копий
#: репозитория (9011 файлов, ~54 с), и СВОИ ЖЕ законные файлы становятся
#: «неожиданными» через префикс worktree: сторож красный на машине автора и
#: зелёный в CI, где свежий клон вложенных деревьев не имеет.
SKIP_PARTS = {
    "tests", "migrations", "commands", "seeds", "venv", ".venv",
    "node_modules", ".claude",
}

#: Where the raw form may stay, each with its reason.
ALLOWED = {
    "users/sellable.py": "the definition itself",
    "appointments/application/services/create_booking_service.py": (
        "booking gate: attribute checks with their own refusal messages "
        "(«not accepting bookings», «profile is not active»), not a pool"
    ),
}

#: The census — modules that must go through the predicate. A lower bound, so a
#: guard scanning the wrong root cannot pass on an empty result.
CENSUS = [
    "users/specialists_api.py",
    "search/views.py",
    "ai/application/services/recommendation_engine.py",
    "users/recommendation_source.py",
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


class TestTheClassGuard:
    def test_the_pattern_catches_every_spelling_it_names(self):
        for sample in (
            "is_available=True",
            "specialist__is_available = True",
            "status=SpecialistProfile.ProfileStatus.ACTIVE",
        ):
            assert RAW.search(sample), sample
        assert not RAW.search("is_available=False")

    def test_the_scan_is_not_vacuous(self):
        assert (REPO / "users" / "sellable.py").exists(), REPO
        for module in CENSUS:
            text = (REPO / module).read_text(encoding="utf-8")
            assert "sellable_q(" in text or "catalog_pool_q(" in text, module

    def test_no_raw_sale_filter_outside_the_definition(self):
        hits = _raw_hits()
        stray = {rel: n for rel, n in hits.items() if rel not in ALLOWED}
        assert stray == {}, (
            "raw is_available=True / ProfileStatus.ACTIVE outside users/sellable.py — "
            "use sellable_q() for sale, catalog_pool_q() for the pool: " f"{stray}"
        )
        for rel, reason in ALLOWED.items():
            assert reason.strip(), rel


# --- surfaces --------------------------------------------------------------------

TOKEN = "test-internal-token-1845a"  # noqa: S105


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = TOKEN


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="sell1845", name="Sellable Salon")


def _master(salon, *, username, phone, name, taking_bookings: bool) -> SpecialistProfile:
    user = User.objects.create_user(username=username, password="x", role="specialist", phone=phone)
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = salon
    profile.display_name = name
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = taking_bookings
    profile.save()
    category = ServiceCategory.objects.create(name=f"Cat {username}", slug=f"cat-{username}")
    Service.objects.create(
        specialist=profile, category=category, name=f"Процедура {name}",
        price=Decimal("1000.00"), duration_minutes=60, is_active=True,
    )
    return profile


@pytest.fixture
def taking(salon):
    return _master(salon, username="sell1845_on", phone="+79991845101", name="Зарина", taking_bookings=True)


@pytest.fixture
def paused(salon):
    return _master(salon, username="sell1845_off", phone="+79991845102", name="Полина", taking_bookings=False)


@pytest.fixture
def client_api(db):
    user = User.objects.create_user(username="sell1845_client", password="x", role="client", phone="+79991845103")
    api = APIClient()
    # The public catalog refuses a caller that does not name its app (APP_TYPE_MISSING).
    api.defaults["HTTP_X_APP_TYPE"] = "client"
    api.force_authenticate(user=user)
    return api


def _ids(payload) -> set[str]:
    """Every ``id`` anywhere in a JSON payload — the shape of each surface differs."""
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in ("id", "specialist_id") and isinstance(value, str):
                found.add(value)
            found |= _ids(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= _ids(item)
    return found


def _service_id(profile) -> str:
    return str(Service.objects.get(specialist=profile).id)


class TestPublicList:
    def test_a_paused_master_is_not_offered(self, client_api, taking, paused):
        resp = client_api.get("/api/v1/specialists/")
        assert resp.status_code == 200, resp.content
        ids = _ids(resp.json())
        assert str(taking.id) in ids
        assert str(paused.id) not in ids

    def test_a_direct_link_still_opens_and_says_so(self, client_api, paused, taking):
        resp = client_api.get(f"/api/v1/specialists/{paused.id}/")
        assert resp.status_code == 200, resp.content
        body = resp.json()
        data = body.get("data", body)
        assert data["accepting_bookings"] is False
        on = client_api.get(f"/api/v1/specialists/{taking.id}/").json()
        assert on.get("data", on)["accepting_bookings"] is True


class TestSearch:
    def test_a_paused_master_is_not_found(self, client_api, taking, paused):
        for q in ("Зарина", "Полина"):
            client_api.get("/api/v1/search/", {"q": q})
        found = _ids(client_api.get("/api/v1/search/", {"q": "Полина"}).json())
        assert str(paused.id) not in found
        assert str(taking.id) in _ids(client_api.get("/api/v1/search/", {"q": "Зарина"}).json())

    def test_a_paused_masters_services_are_not_found(self, client_api, taking, paused):
        off = _ids(client_api.get("/api/v1/search/", {"q": "Процедура Полина"}).json())
        on = _ids(client_api.get("/api/v1/search/", {"q": "Процедура Зарина"}).json())
        assert _service_id(paused) not in off
        assert _service_id(taking) in on


class TestBotFeed:
    def _feed(self, salon):
        api = APIClient()
        api.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
        resp = api.get("/api/v1/internal/specialists/", {"tenant": str(salon.id)})
        assert resp.status_code == 200, resp.content
        body = resp.json()
        rows = body.get("results", body.get("data", body))
        if isinstance(rows, dict):
            rows = rows.get("results", [])
        return {row["id"]: row for row in rows}

    def test_a_paused_master_stays_in_the_feed_with_the_flag(self, salon, taking, paused):
        rows = self._feed(salon)
        assert str(paused.id) in rows, "dropping him would leave the bot's old active row selling"
        assert rows[str(paused.id)]["is_booking_enabled"] is False
        assert rows[str(taking.id)]["is_booking_enabled"] is True
