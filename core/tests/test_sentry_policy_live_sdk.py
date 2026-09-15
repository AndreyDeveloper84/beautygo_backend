"""Политика Sentry (R3) на настоящем SDK: ``init`` с опциями каталога и падающая ручка.

Зачем отдельно от чистых тестов: на пилоте ``SENTRY_DSN`` задан, и ``sentry_sdk.init``
каталога действительно вызывается. Неверное имя опции или сигнатура хука под
закреплённой версией SDK уронили бы каталог на старте — чистые тесты этого не видят.

Здесь ``sentry_sdk.init(**init_options(...))`` — те же опции, что собирают настройки
(``test_settings_build_sentry_init_from_init_options``). DSN фиктивный, сеть не нужна:
свой транспорт складывает конверты в список.

Запрос идёт в ``djangoProject.wsgi.application`` — то, что запускает gunicorn на пилоте,
а не через тестовый клиент Django. Тестовый клиент минует ``WSGIHandler``, а именно его
``DjangoIntegration`` оборачивает в ``SentryWsgiMiddleware``, который кладёт в событие
адрес, метод, строку запроса, заголовки и окружение. Через тестовый клиент ``request``
в событии пуст, и «секретов нет» проходило бы на пустом месте.

Ручка из тестового URL-модуля падает на запросе с телом, строкой запроса,
``Authorization``, ``Cookie``, адресом клиента и ``X-Request-ID``. Событие обязано пройти
через хук: без тела, cookies, окружения и секретных заголовков, но с маршрутом, методом,
классом ошибки и correlation id. В конце — прежний клиент Sentry.
"""
from __future__ import annotations

import io
import json
import sys

import pytest
import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.transport import Transport

from core.sentry_policy import init_options

# Без пробелов: в строке запроса значение не перекодируется, и поиск по тексту события не пуст.
HOME = "Pushkina_45_kv_12"
PHONE = "79990001234"
TOKEN = "Bearer live-secret-token"  # noqa: S105
SESSION = "sessionid=live-secret-session"
CLIENT_IP = "203.0.113.77"
REQUEST_ID = "req-live-sentry-1"
PATH = "/api/v1/sentry-probe/7/boom/"


class _CaptureTransport(Transport):
    """Конверты — в список; в сеть ничего не уходит."""

    def __init__(self, options=None):
        super().__init__(options)
        self.envelopes: list = []

    def capture_envelope(self, envelope):
        self.envelopes.append(envelope)

    def flush(self, timeout=None, callback=None):
        return None

    def kill(self):
        return None


@pytest.fixture
def live_sentry():
    previous = sentry_sdk.get_client()
    transport = _CaptureTransport()
    sentry_sdk.init(
        **init_options(
            dsn="http://public@127.0.0.1/1",
            environment="test",
            release=None,
            traces_sampler=lambda context: 1.0,
        ),
        integrations=[DjangoIntegration()],
        default_integrations=False,
        auto_enabling_integrations=False,
        transport=transport,
    )
    try:
        yield transport
    finally:
        sentry_sdk.get_global_scope().set_client(previous)


def _call_wsgi(app, *, method, path, query, body, headers):
    environ = {
        "REQUEST_METHOD": method,
        "SCRIPT_NAME": "",
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_NAME": "testserver",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "REMOTE_ADDR": CLIENT_IP,
        "CONTENT_TYPE": "application/json",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        **headers,
    }
    status: list = []
    response = app(environ, lambda s, h, exc_info=None: status.append(s))
    try:
        b"".join(response)
    finally:
        getattr(response, "close", lambda: None)()
    return status[0]


@pytest.mark.urls("core.tests.sentry_live_probe_urls")
def test_a_real_sdk_error_event_leaves_through_the_policy(live_sentry):
    from djangoProject.wsgi import application

    status = _call_wsgi(
        application,
        method="POST",
        path=PATH,
        query=f"q={HOME}&page=2",
        body=json.dumps({"q": HOME, "phone": PHONE}).encode(),
        headers={
            "HTTP_AUTHORIZATION": TOKEN,
            "HTTP_COOKIE": SESSION,
            "HTTP_X_REQUEST_ID": REQUEST_ID,
            "HTTP_X_APP_TYPE": "client",
        },
    )

    assert status.startswith("500")
    events = [env.get_event() for env in live_sentry.envelopes if env.get_event() is not None]
    assert len(events) == 1, [env.items for env in live_sentry.envelopes]
    event = events[0]

    # Что обязано остаться — иначе «секретов нет» прошло бы на пустом событии.
    assert "sentry-probe" in (event.get("transaction") or "")
    assert event["exception"]["values"][-1]["type"] == "RuntimeError"
    assert event["tags"]["request_id"] == REQUEST_ID
    request = event["request"]
    assert request["url"] == f"http://testserver{PATH}"
    assert request["method"] == "POST"
    assert request["query_string"] == "q=%5BFiltered%5D&page=2"
    assert {key.lower() for key in request["headers"]} >= {"content-type", "x-app-type", "x-request-id"}

    # Чего быть не может.
    assert set(request) == {"url", "method", "query_string", "headers"}
    assert {key.lower() for key in request["headers"]}.isdisjoint({"authorization", "cookie"})
    text = str(event)
    for secret in (HOME, PHONE, "live-secret-token", "live-secret-session", CLIENT_IP):
        assert secret not in text, secret

    # Событие производительности, если SDK его отправил, тоже прошло чистку.
    for env in live_sentry.envelopes:
        transaction = env.get_transaction_event()
        if transaction is not None:
            assert set(transaction.get("request") or {}) <= {"url", "method", "query_string", "headers"}
            assert HOME not in str(transaction) and "live-secret-token" not in str(transaction)
