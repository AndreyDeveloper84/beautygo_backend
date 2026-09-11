"""Контракт исхода: ответ провайдера → статус §139.

Отображение — единственное, что здесь важно, и оно **не** булево.
Провайдер отвечает одним из пяти способов, человек — ещё одним, и в модели
для этого шесть статусов. Сжать это до «получилось / нет» значило бы
повторять вечно то, что не изменится (``FAILED`` в ``PENDING``), или звать
человека выбирать из пустого списка (``FAILED`` в ``AMBIGUOUS``).

Две строки, которые держат таблицу
----------------------------------

**Найдено — ещё не значит найдено там.** Проба публичного Nominatim на пяти
пензенских адресах (11.09.2026): «Кирова 20» уехала в **Кузнецк** — с домовой
точностью, уверенно, в другой город. Значит одного ``FOUND`` мало: результат
вне города места — ``AMBIGUOUS``, а не ``OK``. Человек подтверждает.

**Точность города — не координата.** Та же проба: «Ладожская 130» — только
улица. Улица в городе ошибается на сотни метров при пороге 25 км, это
приемлемо. Населённый пункт целиком ставит все места в одну точку — центр
Пензы, — и это уже не «координаты есть у улицы», а «координат нет, есть
число». Порог ``MIN_PRECISION_FOR_OK`` назван и стоит один.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from tenants.models import GeocodeStatus


class Outcome(StrEnum):
    """Что ответил провайдер. Пять исходов, и они про провайдера, не про нас."""

    FOUND = "found"                  #: один кандидат
    MULTIPLE = "multiple"            #: несколько кандидатов, провайдер не выбрал
    NOT_FOUND = "not_found"          #: ответил, адреса нет
    UNAVAILABLE = "unavailable"      #: сеть, 5xx, таймаут, лимит — повторить позже
    MISCONFIGURED = "misconfigured"  #: ключа/хоста нет — это про НАС, не про сервис


class Precision(StrEnum):
    """Точность, приведённая к общему виду. Исходное слово провайдера тоже хранится."""

    HOUSE = "house"
    STREET = "street"
    LOCALITY = "locality"
    UNKNOWN = "unknown"


#: Ниже этого результат не считается координатами места. Улица — да: при
#: пороге 25 км ошибка в сотни метров не переставляет соседей. Населённый
#: пункт — нет: он кладёт все места города в одну точку, и «расстояние»
#: между ними становится нулём не потому, что они рядом.
MIN_PRECISION_FOR_OK: frozenset[Precision] = frozenset({Precision.HOUSE, Precision.STREET})


@dataclass(frozen=True)
class GeocodeResult:
    """Ответ провайдера в одном виде для всех провайдеров.

    ``provider_precision`` — слово провайдера как есть (у DaData ``qc_geo``,
    у Яндекса ``exact/number/near/...``): §139 требует хранить точность, и
    приведённая ``precision`` без исходного слова потеряла бы ответ.
    ``reason`` — человеку, не машине: почему недоступен, чего не хватает.
    """

    outcome: Outcome
    provider: str
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    normalized_address: str = ""
    precision: Precision = Precision.UNKNOWN
    provider_precision: str = ""
    locality: str = ""     #: населённый пункт из ответа — для проверки «там ли»
    reason: str = ""


def _same_locality(expected_city: str, locality: str) -> bool:
    """Сравнение мягкое — по вхождению, без регистра.

    «Пенза» против «г Пенза» и «Пенза, Пензенская обл.» — одно место.
    Пустой ``locality`` в ответе — НЕ совпадение: провайдер не сказал,
    где нашёл, и уверенность здесь взять неоткуда.
    """
    want, got = expected_city.strip().lower(), locality.strip().lower()
    return bool(want) and bool(got) and (want in got or got in want)


def status_for(result: GeocodeResult, *, expected_city: str) -> GeocodeStatus:
    """Единственное место, где исход провайдера становится статусом §139.

    ``CONFIRMED`` отсюда не выходит никогда — это исход человека.
    ``NOT_ATTEMPTED`` тоже: он означает, что сюда ещё не приходили.
    ``MISCONFIGURED`` статуса не имеет вовсе — вызывающий обязан
    остановиться до записи (см. ``apply``), иначе одиннадцать строк получат
    ``pending`` из-за пустой переменной окружения и будут выглядеть как
    сервис, который лежал.
    """
    match result.outcome:
        case Outcome.UNAVAILABLE:
            return GeocodeStatus.PENDING
        case Outcome.NOT_FOUND:
            return GeocodeStatus.FAILED
        case Outcome.MULTIPLE:
            return GeocodeStatus.AMBIGUOUS
        case Outcome.FOUND:
            if result.latitude is None or result.longitude is None:
                # Провайдер сказал «нашёл» и не дал координат. Это не отказ
                # сервиса и не пустой список — это ответ, который человеку
                # надо посмотреть.
                return GeocodeStatus.AMBIGUOUS
            if result.precision not in MIN_PRECISION_FOR_OK:
                return GeocodeStatus.AMBIGUOUS
            if not _same_locality(expected_city, result.locality):
                return GeocodeStatus.AMBIGUOUS
            return GeocodeStatus.OK
        case Outcome.MISCONFIGURED:
            raise ValueError(
                "MISCONFIGURED не отображается в статус: это ошибка настройки, "
                "а не исход геокодирования. Остановить прогон до записи."
            )
    raise AssertionError(f"неизвестный исход: {result.outcome!r}")  # pragma: no cover
