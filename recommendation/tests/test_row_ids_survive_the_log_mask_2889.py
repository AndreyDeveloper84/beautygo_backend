"""Адрес закрытой строки доезжает до лога целым (DRF-2889, находка на CI 08.10).

Маскировщик ПДн заменяет на ``[CARD]`` группы цифр, проходящие проверку Луна.
У UUID с дефисами это изредка случается (0,014% по замеру в самом фильтре), и
тогда ERROR «строка закрыта: не определено» терял адрес строки — а узел,
сверявший лог с идентификатором, падал раз в несколько тысяч прогонов.
"""
from __future__ import annotations

import logging
import uuid

from core.pii_log_filter import PIIRedactingFilter
from users.recommendation_source import _log_id

#: Идентификаторы, у которых маска срабатывает на записи с дефисами: первый —
#: из комментария в самом фильтре, второй — с упавшего шарда CI.
KNOWN_VICTIMS = (
    uuid.UUID("320ba56a-8c75-4765-9175-452210217959"),
    uuid.UUID("15835949-2147-4d47-bb62-2539e07b3fec"),
)


def _through_the_mask(text: str) -> str:
    record = logging.LogRecord("users.recommendation_source", logging.ERROR, __file__, 1, text, None, None)
    PIIRedactingFilter().filter(record)
    return record.getMessage()


def test_positive_control_the_dashed_form_is_what_the_mask_eats():
    """Контроль: хотя бы один из известных идентификаторов маска действительно режет."""
    assert any("[CARD]" in _through_the_mask(f"closed={victim}") for victim in KNOWN_VICTIMS)


def test_the_form_written_to_the_log_survives_the_mask():
    for victim in KNOWN_VICTIMS:
        line = f"recommendation.source legal_gates_undetermined rows=1 closed={_log_id(victim)}:{_log_id(victim)}"

        assert _through_the_mask(line) == line


def test_random_ids_survive_in_bulk():
    for _ in range(20000):
        pk = uuid.uuid4()
        line = f"closed={_log_id(pk)}:{_log_id(pk)}"

        assert _through_the_mask(line) == line


def test_a_non_uuid_key_is_printed_as_it_is():
    assert _log_id(42) == "42"
