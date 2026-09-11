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
# Обратное геокодирование — через адаптер §139 (DRF-1685)
# ---------------------------------------------------------------------------
#
# До DRF-1685 здесь стояли тесты транспорта Яндекса, которые сами подставляли
# ``YANDEX_GEOCODER_API_KEY`` — и потому три месяца были зелёными над мёртвой
# в бою функцией. Теперь провайдер подставной и **из реестра**, а состояние
# «не настроен» проверяется отдельно — как состояние, а не как «вернул None».

class _FakeReverse:
    name = "fake-reverse"
    refuse = None
    answer = None
    calls: list = []

    def check(self):
        from core.geocoding.contract import GeocodeResult, Outcome
        if self.refuse:
            return GeocodeResult(outcome=Outcome.MISCONFIGURED, provider=self.name, reason=self.refuse)
        return None

    def geocode(self, address, *, city=""):  # pragma: no cover — не для этого теста
        raise AssertionError("прямое геокодирование здесь не зовётся")

    def reverse(self, lat, lon):
        _FakeReverse.calls.append((lat, lon))
        return self.answer


@pytest.fixture
def fake_reverse(monkeypatch, settings):
    from core.geocoding.providers import PROVIDERS
    from services import geocoding
    _FakeReverse.refuse = None
    _FakeReverse.answer = None
    _FakeReverse.calls = []
    monkeypatch.setitem(PROVIDERS, "fake-reverse", _FakeReverse)
    settings.GEOCODING_PROVIDER = "fake-reverse"
    monkeypatch.setattr(geocoding, "_warned_once", False)
    return _FakeReverse


class TestReverseGeocodeCity:
    def test_found_returns_the_locality(self, fake_reverse):
        from core.geocoding.contract import GeocodeResult, Outcome
        from services.geocoding import reverse_geocode_city
        fake_reverse.answer = GeocodeResult(outcome=Outcome.FOUND, provider="fake-reverse", locality="Пенза")
        assert reverse_geocode_city(53.2, 45.0) == "Пенза"
        assert fake_reverse.calls == [(53.2, 45.0)]

    def test_not_configured_returns_none_and_says_so_once(self, fake_reverse):
        """Не тихий None: причина в логе, один раз на процесс.

        Логгер подменяется напрямую, а не через caplog: конфигурация
        логирования проекта не пропускает записи наверх, и caplog молчал бы
        по чужой причине.
        """
        from services import geocoding
        fake_reverse.refuse = "ключ пуст"
        with patch.object(geocoding.logger, "warning") as warn:
            assert geocoding.reverse_geocode_city(53.2, 45.0) is None
            assert geocoding.reverse_geocode_city(53.2, 45.0) is None
        assert fake_reverse.calls == []  # до провайдера не дошли
        assert warn.call_count == 1
        assert "ключ пуст" in warn.call_args.args[1]

    def test_unavailable_and_not_found_return_none(self, fake_reverse):
        from core.geocoding.contract import GeocodeResult, Outcome
        from services.geocoding import reverse_geocode_city
        fake_reverse.answer = GeocodeResult(outcome=Outcome.UNAVAILABLE, provider="fake-reverse", reason="timeout")
        assert reverse_geocode_city(53.2, 45.0) is None
        fake_reverse.answer = GeocodeResult(outcome=Outcome.NOT_FOUND, provider="fake-reverse")
        assert reverse_geocode_city(53.2, 45.0) is None

    def test_unknown_provider_name_is_a_named_readiness_failure(self, settings):
        from services.geocoding import reverse_geocoding_readiness
        settings.GEOCODING_PROVIDER = "google"
        reason = reverse_geocoding_readiness()
        assert reason is not None and "google" in reason and "dadata" in reason


class TestReverseGeocodingSystemCheck:
    """Сторож из DRF-1685: краснеет, когда ключа нет, а функция объявлена живой."""

    def _run(self):
        from core.geocoding.checks import reverse_geocoding_is_not_a_fiction
        return reverse_geocoding_is_not_a_fiction(None)

    def test_configured_provider_is_silent(self, fake_reverse):
        assert self._run() == []

    def test_unconfigured_is_a_warning_by_default(self, fake_reverse, settings):
        fake_reverse.refuse = "ключ пуст"
        settings.GEOCODING_REQUIRE_LIVE_REVERSE = False
        msgs = self._run()
        assert [m.id for m in msgs] == ["geocoding.W001"]
        assert "всегда default" in msgs[0].msg

    def test_unconfigured_but_declared_live_is_an_error(self, fake_reverse, settings):
        fake_reverse.refuse = "ключ пуст"
        settings.GEOCODING_REQUIRE_LIVE_REVERSE = True
        msgs = self._run()
        assert [m.id for m in msgs] == ["geocoding.E001"]

    def test_the_check_is_wired_into_manage_py_check(self):
        """Проверка зарегистрирована ЗАГРУЗКОЙ приложения, а не импортом из теста.

        Первая редакция этого теста импортировала ``core.geocoding.checks``
        сама — и тем самым сама её регистрировала: подмена, выкинувшая
        импорт из ``apps.py``, оставалась зелёной. Здесь ``manage.py check``
        идёт в отдельном процессе с пустым ключом: единственный путь к
        регистрации — ``ServicesConfig.ready()``.
        """
        import os
        import subprocess
        import sys
        from pathlib import Path

        env = dict(os.environ, DADATA_API_KEY="", GEOCODING_PROVIDER="dadata",
                   GEOCODING_REQUIRE_LIVE_REVERSE="false")
        root = Path(__file__).resolve().parents[2]
        out = subprocess.run(
            [sys.executable, "manage.py", "check"], cwd=root, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        combined = out.stdout + out.stderr
        assert "geocoding.W001" in combined, combined[-800:]

    def test_real_settings_in_this_environment_name_the_state(self):
        """Не подставной: с настоящими настройками этого окружения проверка
        либо молчит (ключ есть), либо называет причину. Тихого третьего нет."""
        from services.geocoding import reverse_geocoding_readiness
        reason = reverse_geocoding_readiness()
        msgs = self._run()
        assert (reason is None and msgs == []) or (reason is not None and len(msgs) == 1)
