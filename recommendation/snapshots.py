"""Сторож содержимого снимка контекста (DRF-1906) — вторая стена после сборщика бота.

Сборщик бота (``apps.orchestrator.context_snapshot``, turn-context-v1) уже
отвергает текст. Каталог на это не полагается и проверяет сам, до записи:

* **схема закрыта** по версии: верх — ровно ``snapshot_version``,
  ``decision_readiness {state_revision, readiness_state}``,
  ``said [{key, value, origin, said_on}]``, ``answered_question {question_id} | null``;
  лишний или недостающий ключ на любом уровне — отказ по имени;
* **значение ``said[].value`` — из закрытого словаря своего ключа** (решение
  главного окна 15.09): ``city`` — хранимое написание города каталога
  (``Tenant.city``), ``visit_context`` — коды сборщика. Неизвестный ключ или
  значение вне словаря — отказ. Длина и число слов — вторая стена, независимая
  от словаря;
* **класс здоровья**: ключ ``said[].key`` или ``answered_question.question_id``,
  любой сегмент которого начинается с ``health_`` / ``screening`` / ``safety_`` /
  ``wellness_`` (без учёта регистра) — отказ по имени. По классу, не по списку
  имён: новый вопрос скрининга не проскочит, потому что его забыли дописать.

Регистр значений не нормализуется (решение главного окна 15.09): контракт —
то, что пишет сборщик, digest считается по байтам. Отказ называет поле и
никогда не повторяет значение: отвергнутое значение и есть то, чему не место
ни в базе, ни в ответе, ни в логе.

Digest согласован с ботом побайтно (эталон — первый тест
``test_context_snapshot.py``).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Any

SNAPSHOT_VERSION_TURN_CONTEXT_V1 = "turn-context-v1"
KNOWN_SNAPSHOT_VERSIONS = frozenset({SNAPSHOT_VERSION_TURN_CONTEXT_V1})

#: Класс вопросов и ключей, чьё имя само по себе — смысл здоровья (главное окно 15.09).
HEALTH_PREFIXES = ("health_", "screening", "safety_", "wellness_")

#: ``said[].origin`` — только сказанное в разговоре (``said_memory.ORIGIN_CONVERSATION``).
SAID_ORIGIN = "conversation"
#: ``visit_context`` — закрытый словарь сборщика (``said_memory._VISIT_RULES``).
VISIT_CONTEXT_CODES = frozenset({"after_work", "weekend", "evening"})
#: Состояния движка DecisionReadiness бота так, как их пишет сборщик — строчные
#: (``decision_readiness.engine.ReadinessState``). Тип поля набора — верхний регистр;
#: перевод — обязанность бота при сборке тела (6.4).
SNAPSHOT_READINESS_STATES = frozenset({
    "ready", "needs_discrimination", "needs_required_context", "insufficient_evidence", "blocked",
})
SAID_KEYS = ("city", "visit_context")

#: Вторая стена: значение словаря короткое — «Нижний Новгород» проходит, реплика нет.
VALUE_MAX_LENGTH = 48
VALUE_MAX_WORDS = 2

_QUESTION_ID_RE = re.compile(r"^[A-Za-z0-9_.:/\-]{1,64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SEGMENT_RE = re.compile(r"[.:/\-]")

_TOP_KEYS = frozenset({"snapshot_version", "decision_readiness", "said", "answered_question"})
_READINESS_KEYS = frozenset({"state_revision", "readiness_state"})
_SAID_ITEM_KEYS = frozenset({"key", "value", "origin", "said_on"})
_ANSWERED_KEYS = frozenset({"question_id"})


class SnapshotInvalid(ValueError):
    """Содержимое снимка не соответствует схеме версии или несёт то, чему там не место."""


def content_digest(content: dict[str, Any]) -> str:
    """sha256 канонического JSON — формула, сверенная с ботом побайтно."""
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def known_cities() -> frozenset[str]:
    """Закрытый словарь ``city``: хранимые написания городов каталога.

    Все тенанты, а не только с продаваемыми мастерами: сторож — стена приватности
    («значение словаря, а не текст»), а не проверка «здесь есть мастера» — город,
    потерявший последнего мастера между «сказано» и «снимок», не должен ронять запись.
    """
    from tenants.models import Tenant

    # order_by() — упорядочивание из Meta попало бы в SELECT DISTINCT и вернуло повторы
    # (замер главного окна на пилоте); frozenset всё равно сводит их.
    return frozenset(Tenant.all_objects.exclude(city="").order_by().values_list("city", flat=True).distinct())


def _name(key: Any) -> str:
    """Имя ключа для отказа — только если оно само похоже на код."""
    return key if isinstance(key, str) and _NAME_RE.match(key) else "<не код>"


def _exact_keys(obj: Any, allowed: frozenset[str], path: str) -> None:
    if not isinstance(obj, dict):
        raise SnapshotInvalid(f"{path}: ожидается объект")
    extra = sorted(_name(k) for k in obj if k not in allowed)
    if extra:
        raise SnapshotInvalid(f"{path}: лишние ключи {extra} — схема снимка закрыта")
    missing = sorted(allowed - set(obj))
    if missing:
        raise SnapshotInvalid(f"{path}: нет ключей {missing}")


def health_prefix(code: str) -> str | None:
    """Префикс класса здоровья в любом сегменте кода (``said.health_x`` — тоже здоровье)."""
    for segment in _SEGMENT_RE.split(code):
        folded = segment.casefold()
        for prefix in HEALTH_PREFIXES:
            if folded.startswith(prefix):
                return prefix
    return None


def _check_readiness(dr: Any) -> None:
    path = "content.decision_readiness"
    _exact_keys(dr, _READINESS_KEYS, path)
    revision = dr["state_revision"]
    if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int) or revision < 0):
        raise SnapshotInvalid(f"{path}.state_revision: целое ≥ 0 или null")
    state = dr["readiness_state"]
    if state is not None and state not in SNAPSHOT_READINESS_STATES:
        raise SnapshotInvalid(
            f"{path}.readiness_state: не из {sorted(SNAPSHOT_READINESS_STATES)} (как пишет сборщик, без приведения)"
        )


def _check_said(said: Any, cities: frozenset[str] | None) -> None:
    if not isinstance(said, list):
        raise SnapshotInvalid("content.said: ожидается список")
    for i, item in enumerate(said):
        path = f"content.said[{i}]"
        _exact_keys(item, _SAID_ITEM_KEYS, path)
        key = item["key"]
        if not isinstance(key, str):
            raise SnapshotInvalid(f"{path}.key: ожидается строка")
        prefix = health_prefix(key)
        if prefix is not None:
            raise SnapshotInvalid(f"{path}.key: класс здоровья ({prefix}) — в снимке не хранится")
        if key not in SAID_KEYS:
            raise SnapshotInvalid(f"{path}.key: неизвестный ключ {_name(key)!r}; известные {list(SAID_KEYS)}")
        if item["origin"] != SAID_ORIGIN:
            raise SnapshotInvalid(f"{path}.origin: только {SAID_ORIGIN!r}")
        said_on = item["said_on"]
        if said_on is not None and not _is_date(said_on):
            raise SnapshotInvalid(f"{path}.said_on: дата YYYY-MM-DD или null")
        value = item["value"]
        if not isinstance(value, str) or not value:
            raise SnapshotInvalid(f"{path}.value: непустая строка")
        # Вторая стена — до словаря и независимо от него.
        if len(value) > VALUE_MAX_LENGTH or len(value.split()) > VALUE_MAX_WORDS:
            raise SnapshotInvalid(
                f"{path}.value: длиннее {VALUE_MAX_LENGTH} знаков или {VALUE_MAX_WORDS} слов — не значение словаря"
            )
        if key == "city":
            if cities is None:
                cities = known_cities()
            allowed = cities
        else:
            allowed = VISIT_CONTEXT_CODES
        if value not in allowed:
            raise SnapshotInvalid(f"{path}.value: вне закрытого словаря ключа {key!r}")


def _check_answered(answered: Any) -> None:
    if answered is None:
        return
    path = "content.answered_question"
    _exact_keys(answered, _ANSWERED_KEYS, path)
    qid = answered["question_id"]
    if not isinstance(qid, str) or not _QUESTION_ID_RE.match(qid):
        raise SnapshotInvalid(f"{path}.question_id: код [A-Za-z0-9_.:/-], не длиннее 64")
    prefix = health_prefix(qid)
    if prefix is not None:
        raise SnapshotInvalid(f"{path}.question_id: класс здоровья ({prefix}) — в снимке не хранится")


def _is_date(value: Any) -> bool:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def check_content(content: Any, *, snapshot_version: str, cities: frozenset[str] | None = None) -> None:
    """Отказ ``SnapshotInvalid`` с именем поля, если содержимое не по схеме версии.

    ``cities`` — словарь ``city``; ``None`` — прочитать из каталога, и только если
    в снимке есть город.
    """
    if snapshot_version not in KNOWN_SNAPSHOT_VERSIONS:
        raise SnapshotInvalid(
            f"snapshot_version {_name(snapshot_version)!r} неизвестна; известные {sorted(KNOWN_SNAPSHOT_VERSIONS)}"
        )
    _exact_keys(content, _TOP_KEYS, "content")
    if content["snapshot_version"] != snapshot_version:
        raise SnapshotInvalid("content.snapshot_version: не совпадает с snapshot_version снимка")
    _check_readiness(content["decision_readiness"])
    _check_said(content["said"], cities)
    _check_answered(content["answered_question"])
