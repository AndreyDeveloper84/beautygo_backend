"""Происхождение координат салона (DRF-1662, §139 — решение владельца 11.09.2026).

Что закрепляется:

* ``tenants.Tenant`` несёт всё, что §139 велит сохранять при
  геокодировании: исходный и нормализованный адрес, широту и долготу,
  провайдера, точность и статус результата, время геокодирования;
* статус — набор исходов, а не булево. §139 называет два исхода,
  «которые не становятся координатами молча»: «Неоднозначный адрес
  подтверждает человек» и «при недоступности сервиса адрес сохраняется
  со статусом pending, но в расчёте расстояния НЕ УЧАСТВУЕТ». Значит,
  ``pending`` и ``ambiguous`` — полноправные исходы, отличные и от
  успеха, и от «не вышло»;
* «геокодировано» ≠ «есть числа». Единственное правило —
  ``Tenant.is_geocoded``: строка со статусом, отличным от успеха,
  геокодированной НЕ считается, даже если координаты заполнены. Второй
  тест ниже — предмет всей задачи;
* никаких умолчаний-числом: свежая строка несёт ``null`` в широте,
  долготе и времени, а пара ``(0, 0)`` (Гвинейский залив) читается как
  отсутствие координат — решение названо в тесте, а не спрятано в коде;
* адаптера геокодера здесь нет — только место под запись. Ни один тест
  не ходит ни к какому сервису.

Целевое доказательство (снято вручную, см. тело PR): подмена
``is_geocoded`` на «обе координаты не None» красит ровно четыре теста
(pending/ambiguous/failed с координатами и ноль-ноль); подмена на
«всегда False» красит ровно три (ok, confirmed и тест ноль-ноль — в нём
есть положительная половина про меридиан). Остальные при этом остаются
зелёными — они стерегут другую сторону правила.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from tenants.models import GEOCODED_STATUSES, GeocodeStatus, Tenant


pytestmark = pytest.mark.django_db

PENZA_LAT = Decimal("53.195042")
PENZA_LNG = Decimal("45.018316")


def _tenant(*, status: str = "", lat=None, lng=None, **extra) -> Tenant:
    """Создать салон и ПЕРЕЧИТАТЬ его из базы.

    Перечитывание обязательно: свойство должно работать на строке в том
    виде, в каком её вернёт база (Decimal, а не то, что положили), иначе
    тест стерёг бы объект в памяти, а не запись.
    """
    row = Tenant.objects.create(
        slug=f"salon-{Tenant.all_objects.count() + 1}",
        name="Салон",
        city="Пенза",
        address="Пенза, ул. Московская, 1",
        geocode_status=status,
        latitude=lat,
        longitude=lng,
        **extra,
    )
    return Tenant.objects.get(pk=row.pk)


# ---------------------------------------------------------------------------
# Обязательный набор из брифа (1–5)
# ---------------------------------------------------------------------------


class TestIsGeocodedRule:
    def test_1_ok_with_both_coordinates_counts(self):
        """Положительная стража: без неё «всегда False» зеленело бы везде."""
        row = _tenant(
            status=GeocodeStatus.OK,
            lat=PENZA_LAT,
            lng=PENZA_LNG,
            geocode_provider="yandex",
            geocode_precision="exact",
            geocode_source_address="Пенза, ул. Московская, 1",
            geocode_normalized_address="Россия, Пенза, Московская улица, 1",
            geocoded_at=timezone.now(),
        )
        assert row.is_geocoded is True

    def test_2_pending_with_both_coordinates_does_not_count(self):
        """ГЛАВНЫЙ. §139: «сохраняется со статусом pending, но в расчёте
        расстояния НЕ УЧАСТВУЕТ». Координаты в строке есть (например,
        остались от прошлого ответа или записаны как догадка) — и всё
        равно строка не геокодирована. Без этого теста «геокодировано»
        выводилось бы из наличия чисел, и pending переставал быть исходом.
        """
        row = _tenant(status=GeocodeStatus.PENDING, lat=PENZA_LAT, lng=PENZA_LNG)
        assert row.latitude is not None and row.longitude is not None
        assert row.is_geocoded is False

    def test_3_ok_with_one_coordinate_does_not_count(self):
        """Одна широта без долготы местом не является."""
        only_lat = _tenant(status=GeocodeStatus.OK, lat=PENZA_LAT, lng=None)
        only_lng = _tenant(status=GeocodeStatus.OK, lat=None, lng=PENZA_LNG)
        assert only_lat.is_geocoded is False
        assert only_lng.is_geocoded is False

    def test_4_ok_with_zero_zero_does_not_count(self):
        """РЕШЕНИЕ: пара (0, 0) читается как отсутствие координат.

        Ноль-ноль — точка в Гвинейском заливе. Для салона в Пензе это не
        результат геокодера, а воскресшее умолчание (§137 снял
        искусственные числа для неизвестного; «снятая константа
        возвращается вычисленной» уже случалось). Поэтому даже при
        статусе ok такая строка геокодированной НЕ считается.

        Ноль в ОДНОЙ координате при ненулевой второй — законная точка
        (экватор или нулевой меридиан) и здесь не отбрасывается; для
        Пензы это невозможно, но правило стережёт умолчание, а не
        географию.
        """
        row = _tenant(status=GeocodeStatus.OK, lat=Decimal("0"), lng=Decimal("0"))
        assert row.latitude == 0 and row.longitude == 0
        assert row.is_geocoded is False

        on_meridian = _tenant(status=GeocodeStatus.OK, lat=PENZA_LAT, lng=Decimal("0"))
        assert on_meridian.is_geocoded is True

    def test_5_empty_row_does_not_count(self):
        """Свежий салон: ничего не геокодировали — ничего и не считается."""
        row = _tenant()
        assert row.is_geocoded is False


# ---------------------------------------------------------------------------
# Остальные исходы §139 — каждый с координатами, чтобы стеречь именно
# статус, а не наличие чисел
# ---------------------------------------------------------------------------


class TestOtherOutcomes:
    def test_confirmed_by_human_counts(self):
        """§139: «Неоднозначный адрес подтверждает человек» — исход
        отдельный от ok, чтобы выбор человека не был неотличим от ответа
        провайдера; но координаты после подтверждения — координаты места.
        """
        row = _tenant(
            status=GeocodeStatus.CONFIRMED,
            lat=PENZA_LAT,
            lng=PENZA_LNG,
            geocode_provider="yandex",
        )
        assert row.is_geocoded is True

    def test_ambiguous_with_coordinates_does_not_count(self):
        """§139: «Геокодер не выбирает за него». Координаты кандидата —
        не координаты места, пока человек не подтвердил."""
        row = _tenant(status=GeocodeStatus.AMBIGUOUS, lat=PENZA_LAT, lng=PENZA_LNG)
        assert row.is_geocoded is False

    def test_failed_with_coordinates_does_not_count(self):
        """«Не вышло» ≠ «неоднозначно» ≠ «сервис недоступен» — но все
        трое одинаково не притворяются геокодированными."""
        row = _tenant(status=GeocodeStatus.FAILED, lat=PENZA_LAT, lng=PENZA_LNG)
        assert row.is_geocoded is False


# ---------------------------------------------------------------------------
# Форма набора исходов и умолчания — сторожа на решение, а не на код
# ---------------------------------------------------------------------------


class TestOutcomeSet:
    def test_at_least_four_outcomes_including_pending_and_ambiguous(self):
        """Бриф: минимум четыре исхода, среди них pending (§139:
        «сохраняется со статусом pending») и «неоднозначно» (§139:
        «подтверждает человек»). Бинарная постановка спрятала бы третий
        и четвёртый исход."""
        values = set(GeocodeStatus.values)
        assert len(values) >= 4
        assert "pending" in values
        assert GeocodeStatus.AMBIGUOUS in values
        assert GeocodeStatus.FAILED in values

    def test_success_set_is_narrower_than_outcome_set(self):
        """Если бы GEOCODED_STATUSES совпал с полным набором, «статус ≠
        успех» перестал бы существовать как условие."""
        assert GEOCODED_STATUSES < set(GeocodeStatus.values)
        assert GeocodeStatus.PENDING not in GEOCODED_STATUSES
        assert GeocodeStatus.AMBIGUOUS not in GEOCODED_STATUSES
        assert GeocodeStatus.FAILED not in GEOCODED_STATUSES
        assert "" not in GEOCODED_STATUSES

    def test_pending_is_a_status_value_not_the_default(self):
        """pending — исход («сервис был недоступен»), а не «ещё не
        пробовали». Умолчание поля — пустая строка."""
        field = Tenant._meta.get_field("geocode_status")
        assert field.default == ""
        assert GeocodeStatus.PENDING in dict(field.choices)


class TestNoNumericDefaults:
    def test_fresh_row_carries_null_not_zero(self):
        """Миграция не подставляет чисел: широта, долгота и время — null,
        строки — пусто. Проверяется на строке из базы, а не на объекте
        в памяти."""
        row = _tenant()
        assert row.latitude is None
        assert row.longitude is None
        assert row.geocoded_at is None
        assert row.geocode_status == ""
        assert row.geocode_provider == ""
        assert row.geocode_precision == ""
        assert row.geocode_source_address == ""
        assert row.geocode_normalized_address == ""

    def test_coordinate_fields_are_nullable_without_numeric_default(self):
        for name in ("latitude", "longitude"):
            field = Tenant._meta.get_field(name)
            assert field.null is True, name
            assert not field.has_default(), name

    def test_provenance_fields_exist(self):
        """§139: «исходный и нормализованный адрес; широта и долгота;
        провайдер; точность и статус результата; время геокодирования»
        — по полю на каждую строку решения."""
        names = {f.name for f in Tenant._meta.get_fields()}
        assert {
            "geocode_source_address",
            "geocode_normalized_address",
            "latitude",
            "longitude",
            "geocode_provider",
            "geocode_precision",
            "geocode_status",
            "geocoded_at",
        } <= names
