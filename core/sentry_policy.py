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


#: Сколько позиций ``sys.argv`` остаётся: путь скрипта и имя подкоманды.
#: Отрезание ПО ПОЗИЦИИ, а не по образцу: роль «значение опции» по написанию не
#: восстанавливается, а позиции 0 и 1 — структура вызова, не пользовательские
#: данные. Предел назван: всё старше первой позиции отрезается независимо от
#: содержимого, и если точка входа когда-нибудь начнёт нести смысл в ``argv[2]``,
#: он будет потерян — намеренно.
ARGV_KEPT = 2

#: Замена тексту записи лога: у записи нет класса, который стоило бы сохранить.
REDACTED_LOG = "<redacted: log message>"


def redacted_message(type_name: str) -> str:
    """Чем заменяется текст исключения: класс остаётся, текст — нет.

    Класс здесь не для красоты: он единственное, что остаётся в САМОМ поле
    сообщения, и по нему видно, что случилось, без ``type``.
    """

    return f"<redacted: {type_name or 'Exception'}>"


def _scrub_exception_messages(event: dict) -> None:
    """Текст КАЖДОГО исключения цепочки, а не последнего.

    ``raise X(str(exc)) from exc`` переносит текст в сообщение другого типа
    (``ai/application/services/chat_service.py:120-121``), поэтому опознавать
    «опасные» классы нельзя — заменяется всё.
    """

    values = event.get("exception")
    values = values.get("values") if isinstance(values, dict) else values
    for value in values if isinstance(values, list) else []:
        if isinstance(value, dict):
            value["value"] = redacted_message(str(value.get("type") or ""))


def _scrub_logentry(event: dict) -> None:
    """Три поля одного канала: чистка одного создаёт видимость закрытия.

    ``message`` — неформатированный ``record.msg``; ``params`` — ``record.args``,
    где значение и лежит при ``logger.error("... %s", value)``; ``formatted`` —
    ``record.getMessage()``, то есть подставленный текст целиком
    (``sentry_sdk/integrations/logging.py:329-333``, пин 2.68.1).
    """

    logentry = event.get("logentry")
    if not isinstance(logentry, dict):
        return
    if "message" in logentry:
        logentry["message"] = REDACTED_LOG
    if "formatted" in logentry:
        logentry["formatted"] = REDACTED_LOG
    if "params" in logentry:
        logentry["params"] = ()


def _scrub_extra(event: dict) -> None:
    """Из ``extra`` остаётся только начало ``sys.argv``.

    Пишет его не наш код, а сам SDK: ``ArgvIntegration`` входит в
    ``_DEFAULT_INTEGRATIONS`` и её глобальный процессор кладёт ``sys.argv`` в
    КАЖДОЕ событие (``sentry_sdk/integrations/argv.py:19-27``). В argv каталога
    есть личность: ``provision_salon_admin`` требует ``--phone``. Прочие ключи
    ``extra`` снимаются целиком — их роль нам неизвестна.
    """

    extra = event.get("extra")
    if not isinstance(extra, dict):
        return
    argv = extra.get("sys.argv")
    clean = {}
    if isinstance(argv, (list, tuple)):
        clean["sys.argv"] = list(argv)[:ARGV_KEPT]
    if clean:
        event["extra"] = clean
    else:
        event.pop("extra", None)


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
        if not isinstance(crumb, dict):
            continue
        # Крошка несёт УЖЕ форматированный текст записи
        # (``sentry_sdk/integrations/logging.py:376``), а её ``data`` — то же,
        # что попадает в ``extra``. Остаётся адрес без строки запроса.
        if "message" in crumb:
            crumb["message"] = REDACTED_LOG
        data = crumb.get("data")
        if isinstance(data, dict):
            url = data.get("url")
            crumb["data"] = {"url": _strip_query(url)} if isinstance(url, str) else {}


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


def scrub_event(event: Any, hint: Any = None) -> Any:
    """Событие Sentry по политике R3; не словарь — как есть."""
    if not isinstance(event, dict):
        return event
    request = event.get("request")
    if isinstance(request, dict):
        event["request"] = _clean_request(request)
    event.pop("user", None)
    _drop_frame_vars(event)
    _scrub_exception_messages(event)
    _scrub_logentry(event)
    _scrub_extra(event)
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
        "include_local_variables": LOCAL_VARIABLES,
        "traces_sampler": traces_sampler,
        "before_send": scrub_event,
        "before_send_transaction": scrub_event,
    }
