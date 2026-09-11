"""Форма на проводе — контракт §9.4, без HTTP и без базы.

Разделение намеренное: здесь проверяется **сама форма**, а в
`test_resolve_endpoint` — обвязка (аутентификация, коды ответа, привязка
источника). Первое ломается от правки типов и обязано ловиться мгновенно;
второе требует базы и живёт в CI.
"""
from __future__ import annotations

from recommendation._serializers import (
    FORBIDDEN_RESPONSE_FIELDS,
    ResolveRequestSerializer,
    ResolveResponseSerializer,
    decision_to_payload,
)
from recommendation.api import ConstraintKind, RESOLVER_SPEC_VERSION, resolve

from .conftest import StaticSource, make_facts, make_request


def _payload():
    decision = resolve(
        make_request(),
        source=StaticSource([make_facts(rating=("4.9", 0)), make_facts()]),
    )
    return decision_to_payload(decision)


def test_payload_passes_its_own_schema():
    """Источник проверяет СВОЙ выход, а не надеется на потребителя.

    У формы ответа теперь есть владелец (§2.1 C3). Владелец, узнающий
    о своём нарушении от потребителя, — это ровно то, чем была старая
    ручка: «Mini App owns the rendering contract».
    """
    serializer = ResolveResponseSerializer(data=_payload())
    assert serializer.is_valid(), serializer.errors


def test_no_display_string_and_no_score_anywhere_in_the_payload():
    """W1 рекурсивно: запрещённое поле на любой глубине красит тест."""
    assert _forbidden_keys(_payload()) == set()


def test_rating_evidence_travels_as_a_pair():
    """E2 на проводе: величина оценки без её обоснованности не сериализуется."""
    ratings = [
        item
        for candidate in _payload()["ordered"]
        for item in candidate["evidence"]
        if item["kind"] == "RATING"
    ]
    assert ratings
    for item in ratings:
        assert set(item["value"]) == {"rating", "review_count"}


def test_version_travels_in_the_body():
    """§9.4: потребитель с неизвестной мажорной версией обязан отказать.

    Отказать он сможет, только если версия приехала. Поэтому она в теле,
    а не в заголовке, который транзит вправе не пробросить.
    """
    payload = _payload()
    assert payload["resolver_spec_version"] == RESOLVER_SPEC_VERSION
    assert payload["policy_versions"]["reason_code_registry_version"]


def test_tiers_and_codes_are_on_the_wire():
    """Ярусы уезжают наружу: поверхность обязана уметь показать равноправных."""
    payload = _payload()
    assert all("tier" in c and c["reason_codes"] for c in payload["ordered"])


def test_request_schema_keeps_constraints_three_valued():
    """`null` не схлопывает UNKNOWN и FLEXIBLE — и на проводе тоже.

    Ограничение едет объектом с явным `kind`. Голое значение с `null`
    означало бы сразу «не спросили» и «человеку всё равно», а это разные
    ответы, из которых следуют разные выдачи (канон §16.2).
    """
    serializer = ResolveRequestSerializer(data={
        "request_id": "req-1",
        "surface": "BOT_CHAT",
        "scope": {"mode": "MARKETPLACE"},
        "need": {"origin": "USER_EXPLICIT", "raw_text": "массаж"},
        "safety_state": "NORMAL",
        "price_max": {"kind": "FLEXIBLE"},
    })
    assert serializer.is_valid(), serializer.errors
    constraints = serializer.build_constraints()
    assert constraints.price_max.kind is ConstraintKind.FLEXIBLE
    assert constraints.time_window.kind is ConstraintKind.UNKNOWN


def test_request_schema_rejects_unknown_surface():
    serializer = ResolveRequestSerializer(data={
        "request_id": "req-1",
        "surface": "TELEPATHY",
        "scope": {"mode": "MARKETPLACE"},
        "need": {"origin": "USER_EXPLICIT"},
        "safety_state": "NORMAL",
    })
    assert not serializer.is_valid()
    assert "surface" in serializer.errors


def test_safety_state_is_required_not_defaulted():
    """Забытое поле — 400, а не пустая выдача.

    `UNKNOWN` по §14 fail-closed: множество пусто, стадии не выполняются.
    Умолчание превращало бы забывчивость вызывающего в ответ «подходящих
    нет» — честный по форме, лживый по смыслу. Так и вышло в CI, пока
    поле имело умолчание; поэтому теперь оно обязательное.
    """
    serializer = ResolveRequestSerializer(data={
        "request_id": "req-1",
        "surface": "BOT_CHAT",
        "scope": {"mode": "MARKETPLACE"},
        "need": {"origin": "USER_EXPLICIT", "raw_text": "массаж"},
    })
    assert not serializer.is_valid()
    assert "safety_state" in serializer.errors


def test_request_schema_has_no_subject_field():
    """Кого спрашивают — говорит аутентификация, а не тело (см. views.py)."""
    assert "subject_ref" not in ResolveRequestSerializer().fields


def _forbidden_keys(node) -> set:
    found: set = set()
    if isinstance(node, dict):
        found |= FORBIDDEN_RESPONSE_FIELDS & set(node)
        for value in node.values():
            found |= _forbidden_keys(value)
    elif isinstance(node, list):
        for value in node:
            found |= _forbidden_keys(value)
    return found
