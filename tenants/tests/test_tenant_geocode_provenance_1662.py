"""Происхождение координат салона (DRF-1662, решение владельца §139 от 11.09.2026).

Предмет тикета — НЕ геокодер. Адаптера здесь нет. Здесь место, куда
адаптер будет писать, и **одно** правило, отличающее «геокодировано» от
«в строке есть числа».

Что закрепляется:

* восемь полей §139 («Что сохраняется») существуют на ``Tenant``;
* координаты — ``null``, а не ``0.0``: у ``latitude``/``longitude`` нет
  умолчания-числа (§137 отменил искусственные ``0.5``; ноль в широте и
  долготе — точка в Гвинейском заливе, и она прошла бы любую проверку
  «заполнено»);
* статус имеет **шесть** исходов, и каждый обоснован строкой §139
  (см. докстринг ``GeocodeStatus``), а не два;
* геокодированной строку делает ``Tenant.is_geocoded`` — **единственное**
  место, где сходятся статус и наличие чисел. ``pending`` с двумя
  координатами геокодированным НЕ считается: §139 дословно — «при
  недоступности сервиса адрес сохраняется со статусом pending, но в
  расчёте расстояния НЕ УЧАСТВУЕТ»;
* миграция только добавляет колонки и не трогает данные.

Почему главный тест — именно ``pending`` с двумя координатами: без него
«геокодировано» выводится из наличия чисел, и ``pending`` перестаёт быть
исходом. Рядом стоит положительная стража (``ok`` и ``confirmed``): без
неё весь набор зеленел бы и на свойстве, возвращающем ``False`` всегда.

Почти все проверки — в памяти, без базы: ``is_geocoded`` и ``clean()``
чистые. Один тест (``test_row_round_trips_through_the_database``) ходит в
базу намеренно — он доказывает, что колонки существуют, то есть что
миграция применилась.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db.models import NOT_PROVIDED

from tenants.models import GeocodeStatus, Tenant


# Настоящее место из замера 11.09.2026: все одиннадцать тенантов в Пензе.
PENZA_LAT = Decimal("53.195878")
PENZA_LNG = Decimal("45.018316")

# §139 «Что сохраняется» — построчно, в том же порядке, что в решении.
FIELDS_FROM_139 = {
    "geocode_source_address": "исходный адрес",
    "geocode_normalized_address": "нормализованный адрес",
    "latitude": "широта",
    "longitude": "долгота",
    "geocode_provider": "провайдер",
    "geocode_precision": "точность результата",
    "geocode_status": "статус результата",
    "geocoded_at": "время геокодирования",
}


def _tenant(**overrides) -> Tenant:
    """Несохранённый тенант. В базу не ходим — правило чистое."""
    kwargs = {
        "slug": "salon-1662",
        "name": "Салон 1662",
        "address": "Пенза, ул. Московская, 1",
        "city": "Пенза",
    }
    kwargs.update(overrides)
    return Tenant(**kwargs)


def _geocoded(status: str, **overrides) -> Tenant:
    """Строка с ПОЛНЫМ набором §139 и заданным исходом.

    Координаты заполнены во всех случаях намеренно: предмет проверки —
    что исход решает статус, а не наличие чисел.
    """
    kwargs = {
        "geocode_source_address": "Пенза, ул. Московская, 1",
        "geocode_normalized_address": "Россия, Пенза, Московская улица, 1",
        "latitude": PENZA_LAT,
        "longitude": PENZA_LNG,
        "geocode_provider": "yandex",
        "geocode_precision": "exact",
        "geocode_status": status,
    }
    kwargs.update(overrides)
    return _tenant(**kwargs)


# ───────────────────────── правило is_geocoded ─────────────────────────


def test_ok_with_both_coordinates_is_geocoded():
    """Положительная стража. Без неё свойство, возвращающее False всегда,
    прошло бы весь остальной набор."""
    assert _geocoded(GeocodeStatus.OK).is_geocoded is True


def test_confirmed_by_human_is_geocoded():
    """Вторая положительная стража.

    §139: «Неоднозначный адрес подтверждает ЧЕЛОВЕК. Геокодер не выбирает
    за него.» Подтверждённая человеком строка пригодна так же, как ``ok``,
    но остаётся отличимой от неё — иначе выбор геокодера и выбор человека
    легли бы неразличимо.
    """
    row = _geocoded(GeocodeStatus.CONFIRMED, geocode_provider="manual")
    assert row.is_geocoded is True


def test_pending_with_both_coordinates_is_not_geocoded():
    """ГЛАВНЫЙ тест тикета.

    §139 дословно: «При недоступности сервиса адрес сохраняется со
    статусом ``pending``, но в расчёте расстояния НЕ УЧАСТВУЕТ.»

    Координаты в строке заполнены (скажем, остались от прошлого успешного
    прогона по прежнему адресу). Если бы «геокодировано» выводилось из
    наличия чисел, ``pending`` перестал бы быть исходом вовсе.
    """
    row = _geocoded(GeocodeStatus.PENDING)
    assert row.latitude is not None and row.longitude is not None
    assert row.is_geocoded is False


def test_ambiguous_with_both_coordinates_is_not_geocoded():
    """§139: «Неоднозначный адрес подтверждает человек. Геокодер не
    выбирает за него.»

    Лучший кандидат провайдера может лежать в координатах как справка, но
    выбор ещё не сделан — значит строка не геокодирована.
    """
    assert _geocoded(GeocodeStatus.AMBIGUOUS).is_geocoded is False


def test_failed_with_both_coordinates_is_not_geocoded():
    """«Не вышло» ≠ «неоднозначно» ≠ «сервис лежал».

    §139 называет особыми ровно два исхода; обычный отказ провайдера —
    третий, и он тоже не делает строку пригодной.
    """
    assert _geocoded(GeocodeStatus.FAILED).is_geocoded is False


def test_not_attempted_with_both_coordinates_is_not_geocoded():
    """Координаты без прогона происхождения не имеют.

    §139 требует сохранять провайдера, точность и время именно затем,
    чтобы числа неизвестного происхождения не считались результатом.
    """
    assert _geocoded(GeocodeStatus.NOT_ATTEMPTED).is_geocoded is False


def test_ok_with_only_latitude_is_not_geocoded():
    """Одна широта точки не задаёт."""
    assert _geocoded(GeocodeStatus.OK, longitude=None).is_geocoded is False


def test_ok_with_only_longitude_is_not_geocoded():
    """Одна долгота точки не задаёт."""
    assert _geocoded(GeocodeStatus.OK, latitude=None).is_geocoded is False


def test_ok_with_null_island_is_not_geocoded():
    """РЕШЕНИЕ, а не самоочевидность: ``0.0 / 0.0`` считается отсутствием.

    Названо здесь дословно, потому что вариант «считать координатами»
    защитим: 0°/0° — валидная точка, и свойство могло бы не лезть в
    правдоподобие значений.

    Выбрано обратное. ``0.0 / 0.0`` — точка в Гвинейском заливе, салона
    там нет ни у кого, а «ноль как умолчание» встречается постоянно: §137
    уже снимал искусственные ``0.5`` для неизвестного расстояния, и
    снятая константа возвращается вычисленной. Пара нулей прошла бы и
    «заполнено», и проверку диапазона, и молча попала бы в расчёт
    расстояния первым же местом.

    Цена решения названа честно: строку салона, стоящего ровно в точке
    0°/0°, система отвергнет. Такого салона не существует.
    """
    row = _geocoded(GeocodeStatus.OK, latitude=Decimal("0.0"), longitude=Decimal("0.0"))
    assert row.is_geocoded is False


def test_empty_row_is_not_geocoded():
    """Салон без адреса (у пилота такой есть — formula-tela, адрес пуст
    намеренно и ждёт владельца)."""
    assert _tenant(address="", city="").is_geocoded is False


def test_every_status_has_a_verdict_and_exactly_two_are_geocoded():
    """Счётчик, а не набор построчных «не считается».

    Сумма отдельных отрицаний не отвечает на вопрос «сколько исходов
    вообще пригодны». Здесь — агрегат тем же правилом: перебор ВСЕХ
    объявленных исходов с полным набором координат, и ровно два из них
    геокодированы. Новый исход, добавленный без решения о пригодности,
    покрасит этот тест.
    """
    verdicts = {
        status.value: _geocoded(status.value).is_geocoded
        for status in GeocodeStatus
    }
    assert set(verdicts) == {s.value for s in GeocodeStatus}
    assert len(verdicts) == 6, verdicts
    geocoded = {name for name, ok in verdicts.items() if ok}
    assert geocoded == {"ok", "confirmed"}, verdicts
    assert geocoded == set(Tenant.GEOCODED_STATUSES)


# ──────────────────── никаких умолчаний-чисел ─────────────────────────


def test_coordinates_default_to_null_never_to_zero():
    """§137/§139: неизвестное не получает числа.

    Проверяется объявление поля, а не поведение конкретной строки:
    умолчание-число вернулось бы во ВСЕ новые строки разом.
    """
    fresh = Tenant()
    assert fresh.latitude is None
    assert fresh.longitude is None
    assert fresh.geocoded_at is None
    assert fresh.geocode_status == GeocodeStatus.NOT_ATTEMPTED
    assert fresh.is_geocoded is False

    for name in ("latitude", "longitude", "geocoded_at"):
        field = Tenant._meta.get_field(name)
        assert field.null is True, name
        assert field.default is NOT_PROVIDED, (name, field.default)


def test_all_eight_fields_from_139_exist():
    """§139 «Что сохраняется» — построчно."""
    names = {f.name for f in Tenant._meta.get_fields()}
    missing = sorted(set(FIELDS_FROM_139) - names)
    assert not missing, f"нет полей §139: {missing}"


def test_provider_is_recorded_so_a_swap_is_observable():
    """§139: «Провайдер в записи ... делает замену провайдера
    НАБЛЮДАЕМОЙ. Без него координаты от двух разных сервисов лежали бы
    неразличимо.»"""
    yandex = _geocoded(GeocodeStatus.OK, geocode_provider="yandex")
    other = _geocoded(GeocodeStatus.OK, geocode_provider="dadata")
    assert yandex.is_geocoded is other.is_geocoded is True
    assert yandex.geocode_provider != other.geocode_provider


# ──────────────────── контракт отказа на записи ───────────────────────


def test_clean_accepts_a_row_without_coordinates():
    """Положительная стража для ``clean()``.

    Без неё три следующих теста прошли бы и на ``clean()``, который
    отказывает всегда — а тогда ни один существующий салон нельзя было бы
    сохранить.
    """
    _tenant().clean()  # не бросает


def test_clean_refuses_a_lone_coordinate():
    with pytest.raises(ValidationError) as exc:
        _tenant(latitude=PENZA_LAT).clean()
    assert "latitude" in exc.value.message_dict


def test_clean_refuses_null_island():
    """Фикция снимается контрактом отказа на границе записи, а не только
    проверкой на чтении."""
    with pytest.raises(ValidationError) as exc:
        _tenant(latitude=Decimal("0"), longitude=Decimal("0")).clean()
    assert "latitude" in exc.value.message_dict


def test_clean_refuses_success_status_without_coordinates():
    """``ok`` без координат — противоречие: неудача называется pending /
    ambiguous / failed."""
    with pytest.raises(ValidationError) as exc:
        _tenant(geocode_status=GeocodeStatus.OK).clean()
    assert "geocode_status" in exc.value.message_dict


def test_clean_accepts_pending_without_coordinates():
    """§139: при недоступности сервиса адрес СОХРАНЯЕТСЯ со статусом
    pending. Значит отказ записи здесь был бы прямым нарушением."""
    _tenant(geocode_status=GeocodeStatus.PENDING).clean()  # не бросает


# ──────────────────────── миграция и база ─────────────────────────────


def test_migration_only_adds_columns_and_touches_no_data():
    """Слияние есть выкладка: миграция, меняющая данные живых людей,
    сработала бы в момент слияния, а не когда решит оператор."""
    import importlib

    from django.db import migrations as dj_migrations

    module = importlib.import_module(
        "tenants.migrations.0005_tenant_geocode_provenance",
    )
    operations = module.Migration.operations
    assert len(operations) == 8, [type(op).__name__ for op in operations]
    assert all(isinstance(op, dj_migrations.AddField) for op in operations), [
        type(op).__name__ for op in operations
    ]
    assert {op.name for op in operations} == set(FIELDS_FROM_139)
    for op in operations:
        if op.name in ("latitude", "longitude", "geocoded_at"):
            assert op.field.default is NOT_PROVIDED, op.name
            assert op.field.null is True, op.name


@pytest.mark.django_db
def test_row_round_trips_through_the_database():
    """Колонки существуют — то есть миграция применилась.

    Две строки: успешная и pending с теми же координатами. После
    перечитывания из базы вердикт остаётся разным — значит различие несёт
    сама колонка статуса, а не объект в памяти.
    """
    ok_row = _geocoded(GeocodeStatus.OK, slug="salon-ok-1662", name="Салон ОК")
    ok_row.save()
    pending_row = _geocoded(
        GeocodeStatus.PENDING, slug="salon-pending-1662", name="Салон PENDING",
    )
    pending_row.save()

    ok_row.refresh_from_db()
    pending_row.refresh_from_db()

    assert ok_row.latitude == pending_row.latitude == PENZA_LAT
    assert ok_row.longitude == pending_row.longitude == PENZA_LNG
    assert ok_row.is_geocoded is True
    assert pending_row.is_geocoded is False

    blank = Tenant.objects.create(slug="salon-blank-1662", name="Салон без адреса")
    blank.refresh_from_db()
    assert blank.latitude is None
    assert blank.longitude is None
    assert blank.geocode_status == GeocodeStatus.NOT_ATTEMPTED
    assert blank.is_geocoded is False
