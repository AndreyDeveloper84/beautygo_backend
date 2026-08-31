"""Tests for POST /api/v1/internal/me/catalog/recommendations/ (#99).

W1 booking flow Phase B endpoint. Three-layer catalog
recommendations per Tau's §10.1.

Coverage:
- Auth boundary (Bearer + X-External-User-ID; smoke — deep tests in PR #158)
- Layer 1: customer's history tenants surfaced; non-history hidden
- Layer 2: top-3 cap; ranked by composite score; reasoning_text built
- Layer 3: category aggregate counts
- Eligibility filter: inactive/disabled specialists excluded
- Goal filter: ILIKE on service name / category slug
- Distance: haversine when lat/lon provided; null otherwise
- Reasoning text: priority order (goal > distance > rating); fallback
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User


VALID_TOKEN = "test-ayla-internal-token-catalog"
URL = "/api/v1/internal/me/catalog/recommendations/"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def external_user_id():
    return "bot:catalog"


@pytest.fixture
def customer(db, external_user_id):
    return User.objects.create_user(
        username=external_user_id, password="x", role="client",
        phone="+79994000001", is_proxy=True,
    )


@pytest.fixture
def tenant_known(db):
    return Tenant.objects.create(slug="cat-known", name="Customer's Salon")


@pytest.fixture
def tenant_new(db):
    return Tenant.objects.create(slug="cat-new", name="New Salon A")


@pytest.fixture
def tenant_explore(db):
    return Tenant.objects.create(slug="cat-explore", name="Explore Salon")


@pytest.fixture
def customer_known_tur(db, customer, tenant_known):
    """Customer has an active CUSTOMER-role TUR in tenant_known."""
    TenantUserRelationship.objects.filter(user=customer).delete()
    return TenantUserRelationship.objects.create(
        user=customer, tenant=tenant_known,
        role=TenantUserRelationship.Role.CUSTOMER,
    )


def _make_specialist(
    tenant, *, suffix: str, name: str, lat=None, lon=None,
    rating: Decimal = Decimal("4.5"), reviews: int = 10,
    is_available: bool = True, is_booking_enabled: bool = True,
    status_value: str = "active",
):
    user = User.objects.create_user(
        username=f"cat_spec_{suffix}", password="x", role="specialist",
        phone=f"+79994{suffix}",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.display_name = name
    profile.tenant = tenant
    profile.status = status_value
    profile.is_available = is_available
    profile.is_booking_enabled = is_booking_enabled
    profile.timezone = "Europe/Moscow"
    profile.rating = rating
    profile.reviews_count = reviews
    if lat is not None:
        profile.location_lat = Decimal(str(lat))
    if lon is not None:
        profile.location_lng = Decimal(str(lon))
    profile.save()
    return profile


@pytest.fixture
def manicure_category(db):
    return ServiceCategory.objects.create(
        name="Маникюр", slug="manicure",
    )


@pytest.fixture
def massage_category(db):
    return ServiceCategory.objects.create(
        name="Массаж", slug="massage",
    )


def _make_service(specialist, category, *, name="Service", price="1500.00"):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name=name,
        price=Decimal(price),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
    )


def _api(
    *, bearer: str | None = VALID_TOKEN,
    external_user_id: str = "bot:catalog",
) -> APIClient:
    c = APIClient()
    # /internal/me/ excluded from AppType + Tenant middleware.
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if external_user_id is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


# ---------------------------------------------------------------------------
# Auth boundary (smoke only — deep coverage in PR #158)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuthBoundary:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_missing_bearer_denied(self, customer):
        r = _api(bearer=None).post(URL, {}, format="json")
        assert r.status_code == 403

    def test_wrong_bearer_denied(self, customer):
        r = _api(bearer="wrong").post(URL, {}, format="json")
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Layer 1 — customer's history tenants
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLayer1YourPlaces:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_history_tenant_specialists_in_layer_1(
        self, customer, customer_known_tur, tenant_known,
        manicure_category,
    ):
        spec = _make_specialist(
            tenant_known, suffix="0001", name="Известный",
        )
        _make_service(spec, manicure_category, name="Маникюр")

        r = _api().post(URL, {}, format="json")
        assert r.status_code == 200
        body = r.json()["data"]
        l1_ids = {item["id"] for item in body["layer_1_your_places"]}
        assert str(spec.id) in l1_ids

    def test_non_history_tenant_not_in_layer_1(
        self, customer, customer_known_tur, tenant_new,
        manicure_category,
    ):
        spec = _make_specialist(
            tenant_new, suffix="0002", name="Новый",
        )
        _make_service(spec, manicure_category, name="Маникюр")

        r = _api().post(URL, {}, format="json")
        body = r.json()["data"]
        l1_ids = {item["id"] for item in body["layer_1_your_places"]}
        assert str(spec.id) not in l1_ids

    def test_no_history_returns_empty_layer_1(
        self, customer, tenant_new, manicure_category,
    ):
        """Customer has zero CUSTOMER-role TURs → layer_1 is []."""
        _make_specialist(tenant_new, suffix="0003", name="X")

        r = _api().post(URL, {}, format="json")
        body = r.json()["data"]
        assert body["layer_1_your_places"] == []

    def test_layer_1_not_filtered_by_goal(
        self, customer, customer_known_tur, tenant_known,
        manicure_category, massage_category,
    ):
        """Code Reviewer MUST_FIX (ae4a9f66195355a6c): typing a goal
        must NOT hide the customer's known salon when that salon
        doesn't happen to offer the goal. Layer 1 is identity/
        relationship-anchored, not goal-scoped."""
        masseur = _make_specialist(
            tenant_known, suffix="0004", name="OnlyMassage",
        )
        _make_service(masseur, massage_category, name="Массаж")

        r = _api().post(URL, {"goal": "маникюр"}, format="json")
        body = r.json()["data"]
        l1_ids = {item["id"] for item in body["layer_1_your_places"]}
        # Salon offers no manicure, but it's still in 'your places'.
        assert str(masseur.id) in l1_ids
        # Layer 2 should NOT surface this masseur — they don't match
        # the goal and aren't in history.
        l2_ids = {item["id"] for item in body["layer_2_ayla_picks"]}
        assert str(masseur.id) not in l2_ids

    def test_layer_1_ordered_by_rating_desc(
        self, customer, customer_known_tur, tenant_known,
        manicure_category,
    ):
        low = _make_specialist(
            tenant_known, suffix="0005", name="Low",
            rating=Decimal("3.5"),
        )
        high = _make_specialist(
            tenant_known, suffix="0006", name="High",
            rating=Decimal("4.9"),
        )
        _make_service(low, manicure_category, name="Маникюр")
        _make_service(high, manicure_category, name="Маникюр")

        r = _api().post(URL, {}, format="json")
        items = r.json()["data"]["layer_1_your_places"]
        # Higher-rated specialist first — stable ordering.
        assert items[0]["id"] == str(high.id)
        assert items[1]["id"] == str(low.id)


# ---------------------------------------------------------------------------
# Layer 2 — top-3 ayla picks (excluding history)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLayer2AylaPicks:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_layer_2_capped_at_3(
        self, customer, tenant_new, manicure_category,
    ):
        for i in range(5):
            sp = _make_specialist(
                tenant_new, suffix=f"010{i}", name=f"S{i}",
                rating=Decimal("4.5"),
            )
            _make_service(sp, manicure_category, name="Маникюр")

        r = _api().post(URL, {}, format="json")
        body = r.json()["data"]
        assert len(body["layer_2_ayla_picks"]) == 3

    def test_layer_2_excludes_history_tenants(
        self, customer, customer_known_tur, tenant_known, tenant_new,
        manicure_category,
    ):
        history_spec = _make_specialist(
            tenant_known, suffix="0110", name="History",
        )
        new_spec = _make_specialist(
            tenant_new, suffix="0111", name="New",
        )
        _make_service(history_spec, manicure_category, name="Маникюр")
        _make_service(new_spec, manicure_category, name="Маникюр")

        r = _api().post(URL, {}, format="json")
        body = r.json()["data"]
        l2_ids = {item["id"] for item in body["layer_2_ayla_picks"]}
        assert str(history_spec.id) not in l2_ids
        assert str(new_spec.id) in l2_ids

    def test_layer_2_ranking_higher_rating_first(
        self, customer, tenant_new, manicure_category,
    ):
        low = _make_specialist(
            tenant_new, suffix="0120", name="Low",
            rating=Decimal("3.5"),
        )
        high = _make_specialist(
            tenant_new, suffix="0121", name="High",
            rating=Decimal("4.9"),
        )
        _make_service(low, manicure_category, name="Маникюр")
        _make_service(high, manicure_category, name="Маникюр")

        r = _api().post(URL, {}, format="json")
        items = r.json()["data"]["layer_2_ayla_picks"]
        # Higher-rated specialist surfaces first.
        assert items[0]["id"] == str(high.id)

    def test_each_layer_2_item_has_reasoning_text(
        self, customer, tenant_new, manicure_category,
    ):
        sp = _make_specialist(
            tenant_new, suffix="0130", name="WithReason",
        )
        _make_service(sp, manicure_category, name="Маникюр")

        r = _api().post(URL, {}, format="json")
        items = r.json()["data"]["layer_2_ayla_picks"]
        assert items
        for it in items:
            assert "reasoning_text" in it
            assert isinstance(it["reasoning_text"], str)
            assert it["reasoning_text"] != ""


# ---------------------------------------------------------------------------
# Eligibility filter
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEligibilityFilter:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_unavailable_specialist_excluded(
        self, customer, tenant_new, manicure_category,
    ):
        sp = _make_specialist(
            tenant_new, suffix="0200", name="Unavailable",
            is_available=False,
        )
        _make_service(sp, manicure_category, name="Маникюр")

        r = _api().post(URL, {}, format="json")
        body = r.json()["data"]
        all_ids = (
            {it["id"] for it in body["layer_1_your_places"]}
            | {it["id"] for it in body["layer_2_ayla_picks"]}
        )
        assert str(sp.id) not in all_ids

    def test_booking_disabled_specialist_excluded(
        self, customer, tenant_new, manicure_category,
    ):
        sp = _make_specialist(
            tenant_new, suffix="0201", name="NoBookings",
            is_booking_enabled=False,
        )
        _make_service(sp, manicure_category, name="Маникюр")

        r = _api().post(URL, {}, format="json")
        body = r.json()["data"]
        all_ids = (
            {it["id"] for it in body["layer_1_your_places"]}
            | {it["id"] for it in body["layer_2_ayla_picks"]}
        )
        assert str(sp.id) not in all_ids

    def test_non_active_status_excluded(
        self, customer, tenant_new, manicure_category,
    ):
        sp = _make_specialist(
            tenant_new, suffix="0202", name="Pending",
            status_value="pending",
        )
        _make_service(sp, manicure_category, name="Маникюр")

        r = _api().post(URL, {}, format="json")
        body = r.json()["data"]
        all_ids = (
            {it["id"] for it in body["layer_1_your_places"]}
            | {it["id"] for it in body["layer_2_ayla_picks"]}
        )
        assert str(sp.id) not in all_ids


# ---------------------------------------------------------------------------
# Goal filter
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGoalFilter:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_goal_filters_by_service_name(
        self, customer, tenant_new, manicure_category, massage_category,
    ):
        manicurist = _make_specialist(
            tenant_new, suffix="0300", name="Manicurist",
        )
        masseur = _make_specialist(
            tenant_new, suffix="0301", name="Masseur",
        )
        _make_service(manicurist, manicure_category, name="Маникюр")
        _make_service(masseur, massage_category, name="Массаж")

        r = _api().post(URL, {"goal": "маникюр"}, format="json")
        body = r.json()["data"]
        l2_ids = {it["id"] for it in body["layer_2_ayla_picks"]}
        assert str(manicurist.id) in l2_ids
        assert str(masseur.id) not in l2_ids

    def test_goal_filters_by_category_slug(
        self, customer, tenant_new, manicure_category, massage_category,
    ):
        manicurist = _make_specialist(
            tenant_new, suffix="0310", name="Manicurist2",
        )
        masseur = _make_specialist(
            tenant_new, suffix="0311", name="Masseur2",
        )
        _make_service(
            manicurist, manicure_category, name="Шеллак",
        )
        _make_service(masseur, massage_category, name="Шиацу")

        r = _api().post(URL, {"goal": "manicure"}, format="json")
        body = r.json()["data"]
        l2_ids = {it["id"] for it in body["layer_2_ayla_picks"]}
        assert str(manicurist.id) in l2_ids
        assert str(masseur.id) not in l2_ids


# ---------------------------------------------------------------------------
# Distance + reasoning text
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDistanceAndReasoning:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_distance_computed_when_lat_lon_given(
        self, customer, tenant_new, manicure_category,
    ):
        # 55.75, 37.62 — center of Moscow. Specialist at 55.76, 37.63
        # is ~1.3 km away.
        sp = _make_specialist(
            tenant_new, suffix="0400", name="NearMoscow",
            lat=55.76, lon=37.63,
        )
        _make_service(sp, manicure_category, name="Маникюр")

        r = _api().post(
            URL, {"lat": 55.75, "lon": 37.62}, format="json",
        )
        items = r.json()["data"]["layer_2_ayla_picks"]
        item = next(it for it in items if it["id"] == str(sp.id))
        assert item["distance_km"] is not None
        assert 0.5 < item["distance_km"] < 3.0

    def test_distance_null_when_no_lat_lon(
        self, customer, tenant_new, manicure_category,
    ):
        sp = _make_specialist(
            tenant_new, suffix="0410", name="NoCoords",
        )
        _make_service(sp, manicure_category, name="Маникюр")

        r = _api().post(URL, {}, format="json")
        items = r.json()["data"]["layer_2_ayla_picks"]
        item = next(it for it in items if it["id"] == str(sp.id))
        assert item["distance_km"] is None

    def test_reasoning_mentions_distance_and_rating(
        self, customer, tenant_new, manicure_category,
    ):
        sp = _make_specialist(
            tenant_new, suffix="0420", name="WithSignals",
            lat=55.76, lon=37.63, rating=Decimal("4.9"),
        )
        _make_service(sp, manicure_category, name="Маникюр")

        r = _api().post(
            URL, {"lat": 55.75, "lon": 37.62}, format="json",
        )
        items = r.json()["data"]["layer_2_ayla_picks"]
        item = next(it for it in items if it["id"] == str(sp.id))
        assert "км" in item["reasoning_text"]
        assert "4.9" in item["reasoning_text"]

    def test_reasoning_mentions_goal_match_first(
        self, customer, tenant_new, manicure_category,
    ):
        sp = _make_specialist(
            tenant_new, suffix="0430", name="GoalMatch",
            rating=Decimal("4.9"),
        )
        _make_service(sp, manicure_category, name="Маникюр")

        r = _api().post(URL, {"goal": "маникюр"}, format="json")
        items = r.json()["data"]["layer_2_ayla_picks"]
        item = next(it for it in items if it["id"] == str(sp.id))
        text = item["reasoning_text"]
        assert "Совпадает с твоей целью" in text
        # Goal match should be the FIRST fact (priority order).
        assert text.startswith("Совпадает с твоей целью")

    def test_reasoning_fallback_when_no_signals(
        self, customer, tenant_new, manicure_category,
    ):
        # Low rating (no rating fact), no coords (no distance), no
        # goal — falls back to 'Принимает записи'.
        sp = _make_specialist(
            tenant_new, suffix="0440", name="Minimal",
            rating=Decimal("3.5"),
        )
        _make_service(sp, manicure_category, name="Маникюр")

        r = _api().post(URL, {}, format="json")
        items = r.json()["data"]["layer_2_ayla_picks"]
        item = next(it for it in items if it["id"] == str(sp.id))
        assert item["reasoning_text"] == "Принимает записи"


# ---------------------------------------------------------------------------
# Layer 3 — category aggregate
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLayer3Explore:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_layer_3_returns_category_counts(
        self, customer, tenant_new, manicure_category, massage_category,
    ):
        m1 = _make_specialist(
            tenant_new, suffix="0500", name="M1",
        )
        m2 = _make_specialist(
            tenant_new, suffix="0501", name="M2",
        )
        ms1 = _make_specialist(
            tenant_new, suffix="0502", name="MS1",
        )
        _make_service(m1, manicure_category)
        _make_service(m2, manicure_category)
        _make_service(ms1, massage_category)

        r = _api().post(URL, {}, format="json")
        cats = r.json()["data"]["layer_3_explore"]["categories"]
        by_slug = {c["slug"]: c for c in cats}
        assert "manicure" in by_slug
        assert "massage" in by_slug
        # Manicure has 2 services, massage has 1.
        assert by_slug["manicure"]["count"] == 2
        assert by_slug["massage"]["count"] == 1


# ---------------------------------------------------------------------------
# Состояние салона решает пул — DRF-1430
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSalonStateGatesThePool:
    """``_base_pool`` обязан исполнять то, что обещает его докстринг.

    Докстринг говорил «active specialist in an active tenant taking
    bookings», а фильтра по салону в коде не было вовсе:
    ``select_related("tenant")`` служит только выводу
    ``tenant_slug``/``tenant_name``. Отключённый салон попадал в «ваши
    места» наравне с живыми.

    Правило контура: рядом с каждым отрицательным утверждением стоит
    положительная стража НА ТЕХ ЖЕ ДАННЫХ — иначе «мастера не видно»
    зелено и на пустом ответе.
    """

    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    @staticmethod
    def _layer_1_ids() -> set[str]:
        r = _api().post(URL, {}, format="json")
        assert r.status_code == 200, r.content
        return {i["id"] for i in r.json()["data"]["layer_1_your_places"]}

    @staticmethod
    def _layer_2_ids() -> set[str]:
        r = _api().post(URL, {}, format="json")
        assert r.status_code == 200, r.content
        return {i["id"] for i in r.json()["data"]["layer_2_ayla_picks"]}

    def test_deactivated_salon_drops_out_of_your_places(
        self, customer, customer_known_tur, tenant_known, manicure_category,
    ):
        spec = _make_specialist(
            tenant_known, suffix="0601", name="Мастер знакомого салона",
        )
        _make_service(spec, manicure_category, name="Маникюр")

        # Положительная стража: салон включён — мастер на месте.
        # Без неё отрицание ниже прошло бы и на пустом ответе.
        assert str(spec.id) in self._layer_1_ids()

        # Меняем РОВНО одно поле — состояние салона.
        tenant_known.is_active = False
        tenant_known.save(update_fields=["is_active"])
        assert str(spec.id) not in self._layer_1_ids()

        # И обратно, чтобы исключить любую другую причину.
        tenant_known.is_active = True
        tenant_known.save(update_fields=["is_active"])
        assert str(spec.id) in self._layer_1_ids()

    @pytest.mark.no_auto_tenant
    def test_master_without_a_salon_does_not_500_the_endpoint(
        self, customer, tenant_new, manicure_category,
    ):
        """Мастер без салона больше не роняет ВЕСЬ эндпоинт в 500.

        ``_build_card`` разыменовывает ``specialist.tenant.slug`` и
        ``.name`` без проверки на ``None``, а ``SpecialistProfile.tenant``
        — ``null=True`` (бэкфилл DRF-242.4 не закрыт). До DRF-1430 такой
        профиль попадал в пул и клал ответ целиком:

            AttributeError: 'NoneType' object has no attribute 'slug'

        То есть страдал не только он сам — 500 получал каждый клиент,
        чей пул его зацепил. INNER JOIN в ``_base_pool`` превращает
        жёсткое падение в корректное отсутствие.

        Это НЕ то же решение, что в ``RecommendationEngine``: там
        мастера без салона остаются в выдаче, потому что движок тенант
        не разыменовывает и отдать такого мастера может.
        """
        orphan = _make_specialist(
            None, suffix="0602", name="Мастер без салона",
        )
        _make_service(orphan, manicure_category, name="Маникюр")

        # Стража на предусловие: профиль действительно без салона.
        orphan.refresh_from_db()
        assert orphan.tenant_id is None

        # Положительная стража НА ТЕХ ЖЕ данных: мастер с живым салоном
        # рядом — чтобы «200 и без сироты» не доказывалось пустым
        # ответом, в котором нет вообще никого.
        healthy = _make_specialist(
            tenant_new, suffix="0603", name="Мастер живого салона",
        )
        _make_service(healthy, manicure_category, name="Маникюр")

        r = _api().post(URL, {}, format="json")
        assert r.status_code == 200, r.content

        picks = {i["id"] for i in r.json()["data"]["layer_2_ayla_picks"]}
        assert str(healthy.id) in picks
        assert str(orphan.id) not in picks
