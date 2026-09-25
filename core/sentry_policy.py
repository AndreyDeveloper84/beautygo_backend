"""Политика отчётов Sentry каталога — решение владельца R3 (15.09, OWNER_QUESTIONS §S).

В отчёт уходит только явно разрешённое.

* **Тело запроса — никогда.** Держат двое: ``max_request_body_size="never"`` в
  ``sentry_sdk.init`` и пересборка ``request`` здесь из названных ключей. Ни один
  не держит политику в одиночку: снятая настройка или снятая чистка ещё не
  открывают тело.
* **Заголовки** — только технические из :data:`ALLOWED_HEADERS`. ``Authorization``,
  ``Cookie`` и всё, что не названо, не уходят.
* **Строка запроса** — значения только у технических параметров из
  :data:`ALLOWED_QUERY_PARAMS`, у остальных — ``[Filtered]``. Из ``url`` строка
  запроса вырезана, в HTTP-крошках — тоже.
* **cookies, env** (адрес клиента, окружение сервера), **user** — не уходят.
* **Локальные переменные кадров стека — никогда.** Держат двое:
  ``include_local_variables=False`` и снятие ``vars`` у кадров здесь. Найдено тестом
  на настоящем SDK: без этого строка запроса с адресом уходила в ``vars.request``
  каждого кадра, хотя ``request`` события был чист.
* **Остаются:** маршрут (``transaction``), уровень, класс и стек ошибки, теги,
  контексты (там статус ответа) и correlation id — тег ``request_id`` из
  ``RequestIDMiddleware`` (``core.log_filters``). Раньше в отчётах его не было.

Одна и та же чистка стоит на ``before_send`` и ``before_send_transaction``:
событие производительности тоже несёт ``request``.

Функции чистые и импортируемые; ``settings`` собирает ``init`` из
:func:`init_options`. В настройках тестов DSN пуст, поэтому то, что настройки берут
эти опции, проверяется текстом настроек. Сами опции проходят через настоящий
``sentry_sdk.init`` и WSGI-вход каталога в ``core/tests/test_sentry_policy_live_sdk.py``.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.log_filters import get_request_id
from core.pii_log_filter import redact_pii

FILTERED = "[Filtered]"

#: Сколько тела запроса отдаёт SDK. Держит политику вместе с чисткой ниже.
BODY_SIZE = "never"

#: Снимок локальных переменных кадров стека. В них ``request`` с полной строкой запроса,
#: разобранное тело, данные сериализатора — всё, что политика не отправляет.
LOCAL_VARIABLES = False

#: Технические заголовки, которые можно отправить. Всё остальное — нет.
ALLOWED_HEADERS: frozenset[str] = frozenset({
    "accept", "content-length", "content-type", "x-app-type", "x-request-id",
})

#: Технические параметры строки запроса, чьи значения можно отправить.
ALLOWED_QUERY_PARAMS: frozenset[str] = frozenset({
    "format", "limit", "offset", "ordering", "page", "page_size",
})

#: Значение, которое ``core.log_filters`` отдаёт вне запроса.
_NO_REQUEST_ID = frozenset({"", "-"})


def _strip_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _filter_query(query: Any) -> str:
    if isinstance(query, (list, tuple)):
        pairs = [(str(key), str(value)) for key, value in query]
    else:
        pairs = parse_qsl(str(query or ""), keep_blank_values=True)
    return urlencode([(key, value if key in ALLOWED_QUERY_PARAMS else FILTERED) for key, value in pairs])


def _filter_headers(headers: Any) -> dict:
    if isinstance(headers, dict):
        items = headers.items()
    elif isinstance(headers, (list, tuple)):
        items = headers
    else:
        return {}
    return {str(key): value for key, value in items if str(key).lower() in ALLOWED_HEADERS}


def _clean_request(request: dict) -> dict:
    """Новый ``request`` только из названного — не «старый минус опасное»."""
    clean: dict = {}
    if "url" in request:
        clean["url"] = _strip_query(str(request.get("url") or ""))
    if "method" in request:
        clean["method"] = request["method"]
    if request.get("query_string"):
        clean["query_string"] = _filter_query(request["query_string"])
    if "headers" in request:
        clean["headers"] = _filter_headers(request["headers"])
    return clean


def _tag_request_id(event: dict) -> None:
    request_id = get_request_id()
    if request_id in _NO_REQUEST_ID:
        return
    tags = event.get("tags")
    if tags is None:
        event["tags"] = {"request_id": request_id}
    elif isinstance(tags, dict):
        tags.setdefault("request_id", request_id)
    elif isinstance(tags, list):
        tags.append(["request_id", request_id])


def _clean_breadcrumbs(event: dict) -> None:
    crumbs = event.get("breadcrumbs")
    values = crumbs.get("values") if isinstance(crumbs, dict) else crumbs if isinstance(crumbs, list) else None
    for crumb in values or []:
        data = crumb.get("data") if isinstance(crumb, dict) else None
        if isinstance(data, dict) and isinstance(data.get("url"), str):
            data["url"] = _strip_query(data["url"])


def _frame_lists(event: dict):
    stacktrace = event.get("stacktrace")
    if isinstance(stacktrace, dict):
        yield stacktrace.get("frames")
    for kind in ("exception", "threads"):
        container = event.get(kind)
        values = container.get("values") if isinstance(container, dict) else container
        for value in values if isinstance(values, list) else []:
            stacktrace = value.get("stacktrace") if isinstance(value, dict) else None
            if isinstance(stacktrace, dict):
                yield stacktrace.get("frames")


def _drop_frame_vars(event: dict) -> None:
    for frames in _frame_lists(event):
        for frame in frames if isinstance(frames, list) else []:
            if isinstance(frame, dict):
                frame.pop("vars", None)


#: Ключи, чьи значения редактура НЕ трогает: это опорные идентификаторы
#: наблюдаемости, и вырезать их значило бы сломать поиск инцидента, ничего не
#: защитив (они не указывают на человека). Тот же принцип, что у
#: ``_OPAQUE_ID_KEYS`` в ``core.pii_log_filter``.
_UNREDACTED_KEYS: frozenset[str] = frozenset({
    "event_id", "trace_id", "span_id", "parent_span_id", "request_id",
    "release", "environment", "platform", "logger", "level", "type",
    "module", "abs_path", "filename", "function", "lineno", "url", "method",
})


def _redact_text_leaves(value: Any, key: str | None = None) -> Any:
    """Каждый строковый лист события — через одно определение редактуры.

    Вторая линия (DRF-2020 C). Первая — не класть значение в текст, и её
    нарушит следующий разработчик, причём молча: носителей у события
    тринадцать (замер маркером), и «починить `exception[].value`» оставило бы
    двенадцать. Поэтому чистится не поле, а ВСЕ текстовые листья.

    Не рекурсия ради рекурсии: ``message``, ``exception[].value``,
    ``logentry.formatted``/``params``, ``breadcrumbs[].message`` и ``.data.*``,
    ``extra.*``, ``contexts.*``, ``tags.*``, ``spans[].description``,
    ``fingerprint[]`` — разные ветки одного дерева, и перечислять их по именам
    значило бы завести четырнадцатую копию списка, который меняет SDK, а не мы.
    """
    if isinstance(value, str):
        if key in _UNREDACTED_KEYS:
            return value
        return redact_pii(value)
    if isinstance(value, dict):
        return {k: _redact_text_leaves(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        redacted = [_redact_text_leaves(v, key) for v in value]
        return type(value)(redacted) if isinstance(value, tuple) else redacted
    return value


def scrub_event(event: Any, hint: Any = None) -> Any:
    """Событие Sentry по политике R3; не словарь — как есть."""
    if not isinstance(event, dict):
        return event
    request = event.get("request")
    if isinstance(request, dict):
        event["request"] = _clean_request(request)
    event.pop("user", None)
    _drop_frame_vars(event)
    _tag_request_id(event)
    _clean_breadcrumbs(event)
    # Редактура — ПОСЛЕДНЕЙ: она работает по тексту, а всё выше меняет
    # структуру. Тег `request_id` уже проставлен и в исключениях списка
    # неприкасаемых, поэтому редактура его не тронет.
    return _redact_text_leaves(event)


def init_options(*, dsn: str, environment: str, release: str | None, traces_sampler) -> dict:
    """Все опции ``sentry_sdk.init`` каталога, кроме интеграций."""
    return {
        "dsn": dsn,
        "environment": environment,
        "release": release,
        "send_default_pii": False,
        "max_request_body_size": BODY_SIZE,
        "include_local_variables": LOCAL_VARIABLES,
        "traces_sampler": traces_sampler,
        "before_send": scrub_event,
        "before_send_transaction": scrub_event,
    }
