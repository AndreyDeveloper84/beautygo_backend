"""Политика отчётов Sentry каталога (решение владельца R3, 15.09).

Стерегутся обе стороны: чего в отчёте быть не может (тело, чувствительная
строка запроса, auth/cookie-заголовки, адрес клиента, пользователь) — и что в
нём обязано остаться (маршрут, уровень, класс и стек ошибки, статус ответа,
теги, correlation id). Пустой отчёт прошёл бы первую половину, поэтому вторая
обязательна.

Тесты чистые: базы и сети не нужно, ``sentry_sdk.init`` здесь не вызывается (DSN
пуст). То, что настройки берут опции из ``init_options``, проверяется текстом
``settings/base.py``. Сами опции на настоящем SDK и WSGI-входе каталога —
в ``test_sentry_policy_live_sdk.py``.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

from core.log_filters import clear_request_id, set_request_id
from core.sentry_policy import FILTERED, init_options, scrub_event


def _redacted(type_name: str) -> str:
    """Ожидаемая замена: класс остаётся, текст — нет.

    Литерал здесь намеренно: узел обязан краснеть оттого, что значение ещё
    в событии, а не оттого, что в `core.sentry_policy` пока нет имени.
    """
    return f"<redacted: {type_name}>"


HOME = "Пушкина 45 кв 12"
PHONE = "+79990001234"


def _event(url: str) -> dict:
    return {
        "event_id": "e1",
        "level": "error",
        "transaction": "/api/v1/internal/specialists/{specialist_id}/service-locations/",
        "exception": {
            "values": [{"type": "ValueError", "value": "boom", "stacktrace": {"frames": [{"function": "post"}]}}],
        },
        "contexts": {"response": {"status_code": 500}},
        "tags": {"app": "catalog"},
        "user": {"id": "u1", "ip_address": "10.0.0.1", "email": "a@b.c"},
        "request": {
            "url": f"{url}?q={HOME}&page=2",
            "method": "POST",
            "query_string": f"q={HOME}&phone={PHONE}&page=2",
            "data": {"q": HOME, "phone": PHONE},
            "cookies": {"sessionid": "secret-session"},
            "env": {"REMOTE_ADDR": "10.0.0.1", "SERVER_NAME": "web-1"},
            "headers": {
                "Authorization": "Bearer secret-token",
                "Cookie": "sessionid=secret-session",
                "X-Forwarded-For": "10.0.0.1",
                "Content-Type": "application/json",
                "X-Request-ID": "req-from-gateway",
            },
        },
    }


def _real_event_from_a_chain(secret: str):
    """Событие, собранное ТЕМ ЖЕ путём, что собирает SDK.

    Не словарь руками: `event_from_exception` — вход, которым пользуется сам
    SDK (`sentry_sdk/utils.py`), и только он кладёт текст в
    `exception.values[].value` через `get_error_message`.
    """
    from sentry_sdk.utils import event_from_exception

    try:
        try:
            raise ValueError(f"external identity {secret!r} is unknown")
        except ValueError as inner:
            raise RuntimeError(f"while binding {secret!r}") from inner
    except RuntimeError:
        event, _hint = event_from_exception(sys.exc_info())
    return event


def test_every_exception_in_a_chain_loses_its_message():
    """DRF-2020 — заменяется КАЖДЫЙ элемент `exception.values`, не последний.

    Цепочка `raise ... from ...` — не редкость: `ai/application/services/
    chat_service.py:120-121` переносит текст исходного исключения в сообщение
    нового типа (`raise AIUnavailable(str(exc)) from exc`, находка ayla-9d).
    Скруббер, узнающий «опасные» типы, такой случай пропустил бы; замена по
    всем элементам — нет.
    """
    event = _real_event_from_a_chain(PHONE)

    scrubbed = scrub_event(event)

    values = scrubbed["exception"]["values"]
    assert len(values) == 2, values
    assert [value["value"] for value in values] == [
        _redacted("ValueError"),
        _redacted("RuntimeError"),
    ]
    assert PHONE not in str(scrubbed)


def test_the_type_module_and_frames_survive_the_message_scrub():
    """Положительная стража: «текста нет» прошло бы и на пустом событии."""
    event = _real_event_from_a_chain(PHONE)

    scrubbed = scrub_event(event)

    outer = scrubbed["exception"]["values"][-1]
    assert outer["type"] == "RuntimeError"
    # ``module`` у встроенных классов SDK не заполняет (``get_type_module``
    # опускает builtins), поэтому стережём то, что он действительно кладёт.
    assert outer["value"] == _redacted("RuntimeError")
    frames = outer["stacktrace"]["frames"]
    assert frames, outer
    assert frames[-1]["function"] == "_real_event_from_a_chain"
    assert frames[-1]["lineno"] > 0


def test_init_options_do_not_enable_the_logs_sink():
    """Дормантный канал остаётся выключенным — и сообщение говорит, что делать.

    `SentryLogsHandler` шлёт запись через `_capture_log`
    (`sentry_sdk/integrations/logging.py:484-493`), МИМО `before_send`; у SDK
    для него отдельный хук `before_send_log` (`sentry_sdk/consts.py:81`).
    """
    options = init_options(
        dsn="http://public@127.0.0.1/1", environment="test", release=None,
        traces_sampler=lambda context: 1.0,
    )

    assert not options.get("enable_logs", False), (
        "канал логов включён, но `before_send_log` не задан: текст записи уйдёт в "
        "Sentry мимо `scrub_event`. Включать логи можно только вместе со "
        "скрабированием в `before_send_log`."
    )


@pytest.mark.parametrize("url", [
    "https://api.example/api/v1/internal/specialists/1/geocoding/suggest/",
    "https://api.example/api/v1/internal/users/2/personal-context/",
    "https://api.example/api/v1/appointments/",
])
def test_the_request_body_is_never_sent_on_any_url(url):
    got = scrub_event(_event(url))

    assert got["request"]["method"] == "POST"
    assert "data" not in got["request"]
    assert "cookies" not in got["request"] and "env" not in got["request"]
    assert HOME not in str(got["request"].get("data", "")) and PHONE not in str(got["request"].get("data", ""))


def test_query_values_are_filtered_except_technical_ones_and_the_url_loses_its_query():
    got = scrub_event(_event("https://api.example/api/v1/appointments/"))

    assert got["request"]["url"] == "https://api.example/api/v1/appointments/"
    query = got["request"]["query_string"]
    assert "page=2" in query
    assert HOME not in query and PHONE not in query
    assert FILTERED.replace("[", "%5B").replace("]", "%5D") in query


def test_only_technical_headers_are_sent():
    got = scrub_event(_event("https://api.example/api/v1/appointments/"))

    headers = got["request"]["headers"]
    assert headers == {"Content-Type": "application/json", "X-Request-ID": "req-from-gateway"}


def test_the_user_is_not_sent():
    got = scrub_event(_event("https://api.example/api/v1/appointments/"))

    assert got["level"] == "error"
    assert "user" not in got


def test_route_status_error_class_stack_and_tags_stay():
    """DRF-2020: правило сменилось решением главного окна — текст заменяется.

    Раньше этот сторож сравнивал ``exception`` целиком, то есть закреплял и
    СООБЩЕНИЕ (``'boom'``). Теперь сообщение заменяется у каждого исключения, а
    предмет сторожа — маршрут, уровень, класс, стек, статус и теги — остаётся
    тем же и проверяется поимённо. Это смена правила, а не ослабление стража:
    утечку текста стерегут узлы DRF-2020, а здесь стоит то, что обязано пережить
    чистку.
    """
    event = _event("https://api.example/api/v1/appointments/")
    before = copy.deepcopy(event)

    got = scrub_event(event)

    assert got["transaction"] == before["transaction"]
    assert got["level"] == "error"
    outer = got["exception"]["values"][-1]
    assert outer["type"] == before["exception"]["values"][-1]["type"]
    assert outer["stacktrace"] == before["exception"]["values"][-1]["stacktrace"]
    assert outer["value"] == _redacted("ValueError")
    assert got["contexts"]["response"]["status_code"] == 500
    assert got["tags"]["app"] == "catalog"


def test_the_correlation_id_is_added_as_a_tag():
    set_request_id("req-abc-123")
    try:
        got = scrub_event(_event("https://api.example/api/v1/appointments/"))
    finally:
        clear_request_id()

    assert got["tags"]["request_id"] == "req-abc-123"
    assert got["tags"]["app"] == "catalog"


def test_breadcrumb_urls_lose_their_query():
    event = {"breadcrumbs": {"values": [{"category": "httplib", "data": {"url": f"https://x.example/s?q={HOME}"}}]}}

    got = scrub_event(event)

    assert got["breadcrumbs"]["values"][0]["data"]["url"] == "https://x.example/s"


def test_stack_frames_lose_local_variables_but_keep_where_it_failed():
    def frame(function):
        return {"function": function, "lineno": 7, "vars": {"request": f"<WSGIRequest: POST '/s/?q={HOME}'>"}}

    event = {
        "exception": {"values": [{"type": "ValueError", "stacktrace": {"frames": [frame("post"), frame("boom")]}}]},
        "threads": {"values": [{"id": 1, "stacktrace": {"frames": [frame("run")]}}]},
        "stacktrace": {"frames": [frame("capture")]},
    }

    got = scrub_event(event)

    assert HOME not in str(got)
    assert [f["function"] for f in got["exception"]["values"][0]["stacktrace"]["frames"]] == ["post", "boom"]
    assert got["exception"]["values"][0]["stacktrace"]["frames"][0]["lineno"] == 7
    assert got["threads"]["values"][0]["stacktrace"]["frames"][0]["function"] == "run"
    assert got["stacktrace"]["frames"][0]["function"] == "capture"


def test_odd_shapes_are_safe():
    assert scrub_event("not-an-event") == "not-an-event"
    assert scrub_event({"message": "boot"})["message"] == "boot"
    assert scrub_event({"request": "not-a-dict"})["request"] == "not-a-dict"


def test_init_options_never_send_the_body_and_scrub_both_event_kinds():
    def sampler(context):
        return 0.1

    options = init_options(dsn="https://k@o.example/1", environment="dev", release=None, traces_sampler=sampler)

    assert options["max_request_body_size"] == "never"
    assert options["include_local_variables"] is False
    assert options["send_default_pii"] is False
    assert options["before_send"] is scrub_event
    assert options["before_send_transaction"] is scrub_event
    assert options["traces_sampler"] is sampler


def test_settings_build_sentry_init_from_init_options():
    """Названный предел: init в тестах не вызывается, связь проверяется текстом настроек."""
    text = (Path(__file__).resolve().parents[2] / "djangoProject" / "settings" / "base.py").read_text(encoding="utf-8")
    assert "from core.sentry_policy import init_options" in text
    assert "**init_options(" in text
    assert "before_send=_sentry_before_send" not in text
