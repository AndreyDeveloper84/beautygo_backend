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
* **Остаются:** маршрут (``transaction``), уровень, класс и стек ошибки, теги,
  контексты (там статус ответа) и correlation id — тег ``request_id`` из
  ``RequestIDMiddleware`` (``core.log_filters``). Раньше в отчётах его не было.

Одна и та же чистка стоит на ``before_send`` и ``before_send_transaction``:
событие производительности тоже несёт ``request``.

Функции чистые и импортируемые; ``settings`` собирает ``init`` из
:func:`init_options`. ``sentry_sdk.init`` в тестах не вызывается (DSN пуст),
поэтому то, что настройки действительно берут эти опции, проверяется текстом
настроек — это названный предел проверки.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.log_filters import get_request_id

FILTERED = "[Filtered]"

#: Сколько тела запроса отдаёт SDK. Держит политику вместе с чисткой ниже.
BODY_SIZE = "never"

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


def scrub_event(event: Any, hint: Any = None) -> Any:
    """Событие Sentry по политике R3; не словарь — как есть."""
    if not isinstance(event, dict):
        return event
    request = event.get("request")
    if isinstance(request, dict):
        event["request"] = _clean_request(request)
    event.pop("user", None)
    _tag_request_id(event)
    _clean_breadcrumbs(event)
    return event


def init_options(*, dsn: str, environment: str, release: str | None, traces_sampler) -> dict:
    """Все опции ``sentry_sdk.init`` каталога, кроме интеграций."""
    return {
        "dsn": dsn,
        "environment": environment,
        "release": release,
        "send_default_pii": False,
        "max_request_body_size": BODY_SIZE,
        "traces_sampler": traces_sampler,
        "before_send": scrub_event,
        "before_send_transaction": scrub_event,
    }
