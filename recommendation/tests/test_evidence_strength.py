"""EVIDENCE STRENGTH — контракт §8, проверки E1–E2.

Здесь проверяется не «функция считает как задумано», а то, что **ошибку
нельзя допустить**: рейтинг без числа отзывов не собирается вовсе.
Сегодняшний дефект (`RATING_REASONING_FLOOR = 4.5` смотрит на величину
оценки и не смотрит на её обоснованность) в такой форме невоспроизводим.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from recommendation.api import (
    EvidenceItem,
    EvidenceKind,
    EvidenceOrigin,
    EvidenceStrength,
    RatingValue,
    rating_evidence,
    rating_strength,
)


def test_rating_evidence_cannot_be_built_without_review_count():
    """E2: `rating` не сериализуется без `review_count` — и не собирается тоже."""
    with pytest.raises(TypeError):
        EvidenceItem(
            kind=EvidenceKind.RATING,
            value=Decimal("4.9"),  # голое число, без числа отзывов
            strength=EvidenceStrength.CONFIRMED,
            origin=EvidenceOrigin.DOMAIN_FACT,
        )


def test_rating_absent_is_unknown_not_zero():
    """Отсутствие оценки — `UNKNOWN`, а не «ноль звёзд»."""
    item = rating_evidence(None)
    assert item.strength is EvidenceStrength.UNKNOWN
    assert item.value is None


def test_rating_without_reviews_is_unsubstantiated():
    """Случай пилота дословно: 4.9 при нуле отзывов.

    Это ровно то число, которое `seed_demo_salons.py` кладёт литералом
    ради порога чужого движка, а чужая поверхность печатает человеку
    как причину выбора мастера.
    """
    assert rating_strength(RatingValue(Decimal("4.9"), 0)) is EvidenceStrength.UNSUBSTANTIATED


def test_confirmed_is_never_issued_while_threshold_unnamed():
    """Порог `N_substantiated` владельцем не назван — `CONFIRMED` не выдаётся.

    Максимум `WEAK`, и это не осторожность: `CONFIRMED` означает «оценке
    можно верить», и выдать его, не имея порога, значило бы назначить порог
    молча — тем же способом, которым в контур попал литерал рейтинга.
    """
    assert rating_strength(RatingValue(Decimal("4.9"), 100)) is EvidenceStrength.WEAK


def test_threshold_separates_weak_from_confirmed_once_named():
    """Когда владелец назовёт число, функция начинает различать — без правок."""
    assert rating_strength(RatingValue(Decimal("4.9"), 9), n_substantiated=10) is EvidenceStrength.WEAK
    assert rating_strength(RatingValue(Decimal("4.9"), 10), n_substantiated=10) is EvidenceStrength.CONFIRMED


def test_unsubstantiated_evidence_is_passed_not_hidden():
    """Свидетельство передаётся, но силой объявлено недоказанным (§8.3).

    Скрывать нельзя: потребитель вправе показать число справочно. Ему
    запрещено другое — выдать его за причину, и запрет выражен силой,
    а не отсутствием поля.
    """
    item = rating_evidence(RatingValue(Decimal("4.9"), 0))
    assert item.strength is EvidenceStrength.UNSUBSTANTIATED
    assert item.value.rating == Decimal("4.9")
    assert item.value.review_count == 0


def test_negative_review_count_is_rejected():
    with pytest.raises(ValueError):
        RatingValue(Decimal("4.0"), -1)


def test_model_inference_is_not_an_origin():
    """Инвариант EVIDENCE ORIGIN: модельное суждение не бывает свидетельством."""
    assert "MODEL_INFERENCE" not in {member.value for member in EvidenceOrigin}
