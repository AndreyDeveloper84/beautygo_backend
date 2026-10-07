"""EVIDENCE STRENGTH — контракт §8. Нормативно и проверяемо.

Правило, ради которого написан модуль:

> **При `review_count = 0` рейтинг не является обоснованием. Потребитель
> не вправе превращать неподтверждённое число в WHY. `UNKNOWN ≠ false ≠ true`.**

Здесь оно сделано **структурно, а не дисциплиной**: собрать свидетельство
о рейтинге без числа отзывов невозможно — конструктор требует пару
(:class:`RatingValue`). Сегодняшний дефект (`RATING_REASONING_FLOOR = 4.5`
проверяет величину оценки и не проверяет её обоснованность) в этой форме
не воспроизводим: величина и обоснованность приходят вместе или не приходят.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class EvidenceKind(StrEnum):
    RATING = "RATING"
    REVIEW_COUNT = "REVIEW_COUNT"
    PRIOR_VISIT = "PRIOR_VISIT"
    SCHEDULE = "SCHEDULE"
    PRICE = "PRICE"
    CAPABILITY_MAPPING = "CAPABILITY_MAPPING"
    SERVICE_MATCH = "SERVICE_MATCH"


class EvidenceStrength(StrEnum):
    """Сила свидетельства. Четыре значения, и `UNKNOWN` — не `false`."""

    CONFIRMED = "CONFIRMED"
    WEAK = "WEAK"
    UNSUBSTANTIATED = "UNSUBSTANTIATED"
    UNKNOWN = "UNKNOWN"


class EvidenceOrigin(StrEnum):
    """Происхождение свидетельства.

    Значения `MODEL_INFERENCE` здесь **не существует** и не появится:
    модельное суждение не становится свидетельством от того, что легло
    в историю (инвариант EVIDENCE ORIGIN, контракт §8.2).
    """

    DOMAIN_FACT = "DOMAIN_FACT"
    USER_EXPLICIT = "USER_EXPLICIT"
    USER_CLICK = "USER_CLICK"
    CURATED_KNOWLEDGE = "CURATED_KNOWLEDGE"


@dataclass(frozen=True)
class RatingValue:
    """Оценка вместе с числом отзывов. Порознь эти два числа не ходят.

    Тип существует ровно для того, чтобы `rating` физически нельзя было
    сериализовать без `review_count` (проверка E2 контракта §8.4). Это тот
    самый разрыв, который сегодня стоит между движком (обнуляет вклад при
    нуле отзывов) и эндпоинтом (берёт сырое число и печатает человеку).
    """

    rating: Decimal
    review_count: int

    def __post_init__(self) -> None:
        if self.review_count < 0:
            raise ValueError(f"review_count не может быть отрицательным: {self.review_count}")


@dataclass(frozen=True)
class EvidenceItem:
    """Одно свидетельство. Неизменяемо, с происхождением и временем наблюдения."""

    kind: EvidenceKind
    value: Any
    strength: EvidenceStrength
    origin: EvidenceOrigin
    observed_at: datetime | None = None
    source_ref: str | None = None

    def __post_init__(self) -> None:
        if self.kind is not EvidenceKind.RATING:
            return
        # `value is None` допустимо ровно в одном случае — когда сила
        # объявлена UNKNOWN, то есть свидетельство говорит «оценки нет».
        # Любое другое сочетание означало бы «оценка есть, а сколько за ней
        # отзывов — неизвестно», а это и есть сегодняшний дефект.
        if self.value is None:
            if self.strength is not EvidenceStrength.UNKNOWN:
                raise ValueError(
                    f"рейтинг без значения обязан иметь strength=UNKNOWN, получено {self.strength} (§8.3)"
                )
            return
        if not isinstance(self.value, RatingValue):
            raise TypeError(
                "свидетельство о рейтинге собирается только из RatingValue(rating, review_count): "
                "оценка без числа отзывов не является свидетельством (§8.4 E2)"
            )


#: Порог, с которого оценка считается подтверждённой: число отзывов.
#: `CONTROLLED_POLICY`, **решение владельца 07.10.2026 (лист решений, п.27,
#: DRF-2884): минимум 5 подтверждённых отзывов.** Первая версия сравнивает
#: обычную среднюю оценку после порога, без коэффициента числа отзывов;
#: насыщение `reviews/(reviews + 10)` из старого движка (DRF-1433) владелец
#: отложил до проверки на примерах — здесь его нет и переносить его сюда
#: без нового решения нельзя.
#:
#: Импортированные оценки в ранжирование не идут (то же решение, DRF-1527).
#: Сегодня это держится на том, что у них ноль отзывов Ayla: поля
#: «происхождение оценки» у профиля нет.
N_SUBSTANTIATED: int | None = 5


def rating_strength(value: RatingValue | None, *, n_substantiated: int | None = None) -> EvidenceStrength:
    """Детерминированная функция силы для рейтинга (контракт §8.3).

    ::

        UNKNOWN          рейтинга нет
        UNSUBSTANTIATED  review_count == 0            <- случай пилота
        WEAK             0 < review_count < N
        CONFIRMED        review_count >= N

    `N` — :data:`N_SUBSTANTIATED` (решение владельца: 5), если политика не
    назвала свой. `WEAK` безвреден: он допускается только как тай-брейк
    внутри яруса и никогда не различает ярусы (§8.3).
    """
    if value is None:
        return EvidenceStrength.UNKNOWN
    if value.review_count == 0:
        return EvidenceStrength.UNSUBSTANTIATED
    threshold = N_SUBSTANTIATED if n_substantiated is None else n_substantiated
    if threshold is None:
        return EvidenceStrength.WEAK
    return EvidenceStrength.CONFIRMED if value.review_count >= threshold else EvidenceStrength.WEAK


def rating_evidence(
    value: RatingValue | None,
    *,
    observed_at: datetime | None = None,
    source_ref: str | None = None,
    n_substantiated: int | None = None,
) -> EvidenceItem:
    """Собрать свидетельство о рейтинге с уже вычисленной силой.

    Свидетельство с `UNSUBSTANTIATED` **передаётся** потребителю, а не
    скрывается (§8.3): потребитель вправе показать число справочно и
    не вправе выдать его за причину. Разделение «показать» и «обосновать»
    проводится здесь, а не на поверхности — иначе каждая поверхность
    проведёт его по-своему, что уже однажды и случилось.
    """
    return EvidenceItem(
        kind=EvidenceKind.RATING,
        value=value,
        strength=rating_strength(value, n_substantiated=n_substantiated),
        origin=EvidenceOrigin.DOMAIN_FACT,
        observed_at=observed_at,
        source_ref=source_ref,
    )
