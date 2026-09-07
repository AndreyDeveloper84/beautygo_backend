"""Реестр `reason_codes` — контракт §7.2. Закрытый, версионируемый, с инвариантами."""
from __future__ import annotations

import pytest

from recommendation._reason_codes import (
    EXCLUSION_CODES,
    OWNED_PREFIXES,
    ReasonCode,
    ReasonCodeInvariantError,
    validate_candidate_codes,
)
from recommendation.api import REASON_CODE_REGISTRY_VERSION

#: Префиксы трека A (`DECISION_READINESS_ENGINE_v1.0.md` §1). Пересечение
#: означало бы, что два документа независимо версионируют одно имя, и
#: аналитика склеит два разных состояния в одно.
_TRACK_A_PREFIXES = ("STATE_", "SAFETY_", "BLOCK_", "REQ_CONTEXT_", "ASK_", "EVID_", "DELEG_", "MEASURE_", "SLOT_")


def test_every_code_belongs_to_an_owned_prefix():
    for code in ReasonCode:
        assert code.value.startswith(OWNED_PREFIXES), f"{code} вне пространства имён контракта"


def test_no_overlap_with_track_a_namespace():
    for code in ReasonCode:
        assert not code.value.startswith(_TRACK_A_PREFIXES), f"{code} пересекается с треком A"


def test_registry_has_a_version():
    assert REASON_CODE_REGISTRY_VERSION


def test_no_code_means_rating_is_a_reason_to_recommend():
    """§7.2: такого кода нет и не будет.

    Проверка буквальная — по именам. «Рекомендуем, потому что хороший
    рейтинг» это и есть утверждение, которое человек прочитал на экране
    как «Рейтинг 4.9» при нуле отзывов.
    """
    for code in ReasonCode:
        assert "RATING_HIGH" not in code.value
        assert code.value != "MATCH_RATING"


def test_candidate_needs_at_least_one_code():
    with pytest.raises(ReasonCodeInvariantError):
        validate_candidate_codes(frozenset())


def test_exclusion_code_cannot_sit_on_a_candidate_in_the_output():
    """Исключения живут в `excluded[]` (§4.4).

    Смешать списки значило бы сделать «почему его нет» невыразимым: код
    исключения на кандидате, который всё-таки показан, не значит ничего.
    """
    with pytest.raises(ReasonCodeInvariantError):
        validate_candidate_codes(frozenset({ReasonCode.MATCH_SERVICE_EXACT, ReasonCode.ELIG_EXCLUDED_BUDGET}))


def test_substantiated_rating_requires_a_match_code():
    """Рейтинг не бывает основанием сам по себе (§7.2, канон §9.1)."""
    with pytest.raises(ReasonCodeInvariantError):
        validate_candidate_codes(frozenset({ReasonCode.QUALITY_RATING_SUBSTANTIATED}))

    ok = validate_candidate_codes(
        frozenset({ReasonCode.QUALITY_RATING_SUBSTANTIATED, ReasonCode.MATCH_SERVICE_EXACT})
    )
    assert ReasonCode.QUALITY_RATING_SUBSTANTIATED in ok


def test_match_undetermined_does_not_license_rating():
    """`MATCH_UNDETERMINED` — признание, что соответствие не считали.

    Пропусти мы его как «код семейства MATCH_», и получилось бы
    «соответствие неизвестно, зато рейтинг хороший» — то самое
    ранжирование по рейтингу, только с приличным видом.
    """
    with pytest.raises(ReasonCodeInvariantError):
        validate_candidate_codes(
            frozenset({ReasonCode.QUALITY_RATING_SUBSTANTIATED, ReasonCode.MATCH_UNDETERMINED})
        )


def test_codes_come_back_lexicographically_sorted():
    codes = validate_candidate_codes(
        frozenset({ReasonCode.MATCH_SERVICE_EXACT, ReasonCode.ELIG_ACTIVE_OFFER, ReasonCode.SCOPE_WITHIN_CITY})
    )
    assert list(codes) == sorted(codes)


def test_exclusion_codes_are_scope_or_elig_only():
    """S2–S6 не исключают — они упорядочивают (§4.4)."""
    for code in EXCLUSION_CODES:
        assert code.value.startswith(("SCOPE_", "ELIG_"))
