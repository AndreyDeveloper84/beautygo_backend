"""Tests for DRF-197: RegionalPricing + get_region_key."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.utils import IntegrityError

from services.models import RegionalPricing, ServiceCategory, ServiceTemplate
from services.pricing import get_region_key


@pytest.fixture
def template(db) -> ServiceTemplate:
    category = ServiceCategory.objects.create(name='Тест-категория')
    return ServiceTemplate.objects.create(
        category=category,
        name='Тест-шаблон',
        name_short='Тест',
        duration_default=60,
        duration_min=30,
        duration_max=90,
    )


# ---------------------------------------------------------------------------
# Модель RegionalPricing
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestRegionalPricingModel:
    def test_create(self, template):
        rp = RegionalPricing.objects.create(
            template=template, region_key='penza', region_name='Пенза',
            price_min=Decimal('500'), price_max=Decimal('1000'),
        )
        assert 'Пенза' in str(rp)

    def test_unique_per_template_region(self, template):
        RegionalPricing.objects.create(
            template=template, region_key='penza', region_name='Пенза',
            price_min=Decimal('500'), price_max=Decimal('1000'),
        )
        with pytest.raises(IntegrityError):
            RegionalPricing.objects.create(
                template=template, region_key='penza', region_name='Пенза 2',
                price_min=Decimal('600'), price_max=Decimal('1200'),
            )

    def test_min_must_be_le_max(self, template):
        rp = RegionalPricing(
            template=template, region_key='default', region_name='Default',
            price_min=Decimal('2000'), price_max=Decimal('1000'),
        )
        with pytest.raises(ValidationError):
            rp.full_clean()


# ---------------------------------------------------------------------------
# Команда seed_regional_pricing
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSeedRegionalPricingCommand:
    @pytest.fixture(autouse=True)
    def run_seed(self):
        call_command('seed_service_templates', verbosity=0)
        call_command('seed_regional_pricing', verbosity=0)

    def test_all_templates_have_penza_and_default(self):
        templates = ServiceTemplate.objects.all()
        for tpl in templates:
            keys = set(
                tpl.regional_prices.values_list('region_key', flat=True)
            )
            assert {'penza', 'default'}.issubset(keys), (
                f"{tpl.name}: нет цен для {keys}"
            )

    def test_penza_cheaper_than_default(self):
        """Инвариант: цены в Пензе ≤ default для одного и того же шаблона."""
        for tpl in ServiceTemplate.objects.all():
            penza = tpl.regional_prices.get(region_key='penza')
            default = tpl.regional_prices.get(region_key='default')
            assert penza.price_max <= default.price_max, (
                f"{tpl.name}: Пенза {penza.price_max} > default {default.price_max}"
            )

    def test_idempotent(self):
        count_before = RegionalPricing.objects.count()
        call_command('seed_regional_pricing', verbosity=0)
        assert RegionalPricing.objects.count() == count_before


# ---------------------------------------------------------------------------
# Функция get_region_key
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestGetRegionKey:
    def test_city_input_wins_over_coordinates(self):
        """Явный выбор города — высший приоритет, геокодирование не вызывается."""
        with patch('services.pricing.reverse_geocode_city') as mock:
            region = get_region_key(lat=55.75, lon=37.62, city_input='Пенза')
            assert region == 'penza'
            mock.assert_not_called()

    def test_city_input_case_insensitive(self):
        assert get_region_key(city_input='ПЕНЗА') == 'penza'
        assert get_region_key(city_input=' penza ') == 'penza'

    def test_unknown_city_input_falls_back_to_default(self):
        assert get_region_key(city_input='Владивосток') == 'default'

    def test_reverse_geocode_hit_maps_to_region(self):
        with patch(
            'services.pricing.reverse_geocode_city', return_value='Пенза',
        ) as mock:
            assert get_region_key(lat=53.2, lon=45.0) == 'penza'
            mock.assert_called_once_with(53.2, 45.0)

    def test_reverse_geocode_unknown_city_falls_back_to_default(self):
        with patch(
            'services.pricing.reverse_geocode_city', return_value='Казань',
        ):
            assert get_region_key(lat=55.8, lon=49.1) == 'default'

    def test_reverse_geocode_none_falls_back_to_default(self):
        """Сбой геокодирования (нет ключа, таймаут) → default."""
        with patch(
            'services.pricing.reverse_geocode_city', return_value=None,
        ):
            assert get_region_key(lat=53.2, lon=45.0) == 'default'

    def test_no_inputs_returns_default(self):
        assert get_region_key() == 'default'

    def test_invalid_coordinates_dont_raise(self):
        """Мусор в координатах не должен падать с исключением."""
        with patch('services.pricing.reverse_geocode_city') as mock:
            assert get_region_key(lat='invalid', lon='nope') == 'default'  # type: ignore[arg-type]
            mock.assert_not_called()


# ---------------------------------------------------------------------------
# Yandex geocoder — транспорт
# ---------------------------------------------------------------------------

class TestReverseGeocodeCity:
    def test_returns_none_without_api_key(self, settings):
        settings.YANDEX_GEOCODER_API_KEY = ''
        from services.geocoding import reverse_geocode_city
        assert reverse_geocode_city(53.2, 45.0) is None

    def test_parses_city_from_response(self, settings):
        settings.YANDEX_GEOCODER_API_KEY = 'test-key'
        from services import geocoding

        fake_json = {
            'response': {
                'GeoObjectCollection': {
                    'featureMember': [
                        {'GeoObject': {'name': 'Пенза'}},
                    ],
                },
            },
        }

        class _Response:
            def raise_for_status(self): pass
            def json(self): return fake_json

        with patch.object(geocoding.requests, 'get', return_value=_Response()):
            assert geocoding.reverse_geocode_city(53.2, 45.0) == 'Пенза'

    def test_network_error_returns_none(self, settings):
        settings.YANDEX_GEOCODER_API_KEY = 'test-key'
        from services import geocoding

        with patch.object(
            geocoding.requests, 'get',
            side_effect=geocoding.requests.Timeout(),
        ):
            assert geocoding.reverse_geocode_city(53.2, 45.0) is None

    def test_empty_features_returns_none(self, settings):
        settings.YANDEX_GEOCODER_API_KEY = 'test-key'
        from services import geocoding

        class _Response:
            def raise_for_status(self): pass

            def json(self):
                return {'response': {'GeoObjectCollection': {'featureMember': []}}}

        with patch.object(geocoding.requests, 'get', return_value=_Response()):
            assert geocoding.reverse_geocode_city(53.2, 45.0) is None
