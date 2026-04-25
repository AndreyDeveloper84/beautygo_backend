"""Tests for DRF-198: Service Templates API."""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.core.management import call_command
from rest_framework.test import APIClient

from services.models import RegionalPricing
from users.models import User

TEMPLATES_URL = '/api/v1/service-templates/'
REGIONS_URL = '/api/v1/service-templates/regions/'


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def seeded(db):
    call_command('seed_service_templates', verbosity=0)
    call_command('seed_regional_pricing', verbosity=0)


@pytest.fixture
def pro_user(seeded) -> User:
    return User.objects.create_user(
        username='pro_tpl', role='specialist',
        phone='+79990500001', is_verified=True,
    )


@pytest.fixture
def client_user(seeded) -> User:
    return User.objects.create_user(
        username='client_tpl', role='client',
        phone='+79990500002', is_verified=True,
    )


def _pro(pro_user) -> APIClient:
    c = APIClient()
    c.defaults['HTTP_X_APP_TYPE'] = 'pro'
    c.force_authenticate(user=pro_user)
    return c


def _client_app(client_user) -> APIClient:
    c = APIClient()
    c.defaults['HTTP_X_APP_TYPE'] = 'client'
    c.force_authenticate(user=client_user)
    return c


def _manicure_category_id() -> str:
    from services.models import ServiceCategory
    return str(ServiceCategory.objects.get(slug='manicure').pk)


# ---------------------------------------------------------------------------
# GET /api/v1/service-templates/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestServiceTemplatesList:
    def test_unauthenticated_returns_401(self, seeded):
        resp = APIClient(HTTP_X_APP_TYPE='pro').get(TEMPLATES_URL)
        assert resp.status_code in (401, 403)

    def test_client_app_is_forbidden(self, client_user):
        resp = _client_app(client_user).get(
            TEMPLATES_URL, {'category_id': _manicure_category_id()},
        )
        assert resp.status_code == 403

    def test_missing_category_id_returns_400(self, pro_user):
        resp = _pro(pro_user).get(TEMPLATES_URL)
        assert resp.status_code == 400
        assert resp.json()['error']['code'] == 'VALIDATION_ERROR'

    def test_invalid_category_id_returns_400(self, pro_user):
        resp = _pro(pro_user).get(TEMPLATES_URL, {'category_id': 'not-a-uuid'})
        assert resp.status_code == 400

    def test_unknown_category_id_returns_404(self, pro_user):
        resp = _pro(pro_user).get(
            TEMPLATES_URL, {'category_id': str(uuid.uuid4())},
        )
        assert resp.status_code == 404

    def test_explicit_region_overrides_detection(self, pro_user):
        """`region=penza` используется даже при наличии lat/lon."""
        with patch('services.templates_views.get_region_key') as mock:
            resp = _pro(pro_user).get(TEMPLATES_URL, {
                'category_id': _manicure_category_id(),
                'region': 'penza',
                'lat': '55.75', 'lon': '37.62',  # Москва, но должен выиграть penza
            })
            mock.assert_not_called()
        assert resp.status_code == 200
        data = resp.json()['data']
        assert data['region'] == 'penza'
        assert data['region_name'] == 'Пенза'

    def test_returns_templates_with_pricing(self, pro_user):
        resp = _pro(pro_user).get(TEMPLATES_URL, {
            'category_id': _manicure_category_id(),
            'region': 'penza',
        })
        data = resp.json()['data']
        assert data['region'] == 'penza'
        assert len(data['templates']) == 10  # Маникюр: 10 шаблонов
        first = data['templates'][0]
        assert set(first.keys()) == {
            'id', 'name', 'name_short', 'duration_default',
            'duration_min', 'duration_max', 'is_popular',
            'recommended_price_min', 'recommended_price_max',
        }
        # Конкретика: «Классический маникюр» в Пензе = 600-1000
        classic = next(
            t for t in data['templates']
            if t['name'] == 'Классический маникюр'
        )
        assert classic['recommended_price_min'] == 600
        assert classic['recommended_price_max'] == 1000

    def test_popular_templates_first(self, pro_user):
        resp = _pro(pro_user).get(TEMPLATES_URL, {
            'category_id': _manicure_category_id(),
            'region': 'penza',
        })
        templates = resp.json()['data']['templates']
        popular = [t['is_popular'] for t in templates]
        # Популярные все в начале — переход False не должен встречаться дважды
        transitions = sum(
            1 for i in range(1, len(popular)) if popular[i] and not popular[i - 1]
        )
        assert transitions == 0
        assert popular[:3] == [True, True, True]

    def test_lat_lon_trigger_geocoding(self, pro_user):
        """Если region не указан, а lat/lon есть — геокодируем."""
        with patch(
            'services.pricing.reverse_geocode_city', return_value='Пенза',
        ) as mock:
            resp = _pro(pro_user).get(TEMPLATES_URL, {
                'category_id': _manicure_category_id(),
                'lat': '53.2', 'lon': '45.0',
            })
            mock.assert_called_once_with(53.2, 45.0)
        assert resp.json()['data']['region'] == 'penza'

    def test_no_coords_no_region_fallback_default(self, pro_user):
        resp = _pro(pro_user).get(TEMPLATES_URL, {
            'category_id': _manicure_category_id(),
        })
        data = resp.json()['data']
        assert data['region'] == 'default'
        classic = next(
            t for t in data['templates']
            if t['name'] == 'Классический маникюр'
        )
        assert classic['recommended_price_min'] == 1000
        assert classic['recommended_price_max'] == 2000

    def test_unknown_region_falls_back_to_default_prices(self, pro_user):
        """Регион без цен → шаблоны возвращаются с default-ценами."""
        resp = _pro(pro_user).get(TEMPLATES_URL, {
            'category_id': _manicure_category_id(),
            'region': 'vladivostok',
        })
        data = resp.json()['data']
        assert data['region'] == 'vladivostok'
        classic = next(
            t for t in data['templates']
            if t['name'] == 'Классический маникюр'
        )
        assert classic['recommended_price_min'] == 1000  # fallback на default

    def test_response_is_cached(self, pro_user):
        """Второй запрос с теми же параметрами не дергает БД для шаблонов."""
        params = {
            'category_id': _manicure_category_id(),
            'region': 'penza',
        }
        first = _pro(pro_user).get(TEMPLATES_URL, params)
        assert first.status_code == 200

        with patch(
            'services.templates_views.ServiceTemplatesListView._build_payload'
        ) as build:
            second = _pro(pro_user).get(TEMPLATES_URL, params)
            assert second.status_code == 200
            build.assert_not_called()
        assert first.json()['data'] == second.json()['data']


# ---------------------------------------------------------------------------
# GET /api/v1/service-templates/regions/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSupportedRegions:
    def test_requires_pro_app(self, client_user):
        resp = _client_app(client_user).get(REGIONS_URL)
        assert resp.status_code == 403

    def test_returns_penza_and_default(self, pro_user):
        resp = _pro(pro_user).get(REGIONS_URL)
        assert resp.status_code == 200
        data = resp.json()['data']
        keys = {r['key'] for r in data}
        assert {'penza', 'default'}.issubset(keys)
        for row in data:
            assert set(row.keys()) == {'key', 'name'}
            assert row['name']

    def test_default_present_even_without_pricing(
        self, pro_user, seeded,
    ):
        """Если в БД только экзотические регионы — default всё равно есть."""
        RegionalPricing.objects.all().delete()
        resp = _pro(pro_user).get(REGIONS_URL)
        data = resp.json()['data']
        assert any(r['key'] == 'default' for r in data)
