"""Внешняя личность не уезжает в Sentry — ни одним носителем (DRF-2020 C).

Замер, с которого начался лист: идентификатор попадает в ТЕКСТ ИСКЛЮЧЕНИЯ, а
чистка политики (`core.sentry_policy.scrub_event`) текст исключения не трогает.
Это трансграничная передача персональных данных, а не шумный лог.

Перепись, сделанная прогоном (маркер в каждое поле → настоящая чистка):

* носителей с маркером до чистки — 23, **после — 13**. То есть «починить
  `exception[].value`» оставило бы двенадцать: `transaction`, `message`,
  `logentry.params`/`formatted`, `breadcrumbs[].message`,
  `breadcrumbs[].data.*` (чистился ТОЛЬКО ключ `url`), `extra.*`,
  `contexts.*.data.*`, `tags.*`, `spans[].description` (события
  производительности идут через ту же чистку), `fingerprint[]`, `server_name`;
* мест `raise` в каталоге — 644, подставляют значение — 304, названы как
  личность — 37, из них несут ВНЕШНИЙ идентификатор — 13;
* достижимость: `InvalidExternalUserIDError` перехвачен у четырёх
  вызывающих файлов и НЕ перехвачен у четырёх, то есть путь до
  необработанного 500 и до отправки существует.

Отдельная находка того же замера: к событию Sentry в каталоге **вообще не
применялась** редактура персональных данных. `core.pii_log_filter.redact_pii`
(телефон, почта, карта) стояла на логах и на операторской строке алертов, но не
на событии — значит телефон в тексте исключения уходил наружу так же, как
идентификатор.

Починка в два слоя, и здесь проверяются оба:

1. **не класть значение в текст** — в тех 13 местах, где идентификатор внешний,
   печатается ФОРМА (источник и длина), а не значение;
2. **чистить событие** — текстовые листья события проходят через `redact_pii`,
   одно определение на логи, алерты и Sentry. Вторая линия нужна потому, что
   первую нарушит следующий разработчик, и молча.

Узлы идут ЧЕРЕЗ НАСТОЯЩИЙ SDK и WSGI-вход каталога (как
`test_sentry_policy_live_sdk`), а не зовут `scrub_event` напрямую: у нас уже
было, что двадцать один зелёный узел был правдой про функцию и неправдой про
продукт — поле не переносилось через шов, и узлы этого не видели.

И рядом с «идентификатора нет» стоит «событие дошло и несёт то, что должно»:
первое утверждение верно и о пустом событии, то есть зелёный сторож над
сломанной отправкой выглядел бы так же.
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

#: Внешняя личность той же формы, что ходит в `X-External-User-ID`
#: (`<source>:<id>`). Значение синтетическое, но форма настоящая — именно её
#: подставляли в текст исключения.
IDENTITY = "max:729481"

#: Телефон в тестовом диапазоне (сторож PII разрешает 900/999).
PHONE = "+79991234567"

PATH = "/api/v1/sentry-probe/identity/boom/"
REQUEST_ID = "req-live-identity-2020c"


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
        transport=transport,
    )
    try:
        yield transport
    finally:
        sentry_sdk.get_global_scope().set_client(previous)


def _call_wsgi(app, *, path, query="", body=b"", headers=None):
    environ = {
        "REQUEST_METHOD": "POST",
        "SCRIPT_NAME": "",
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_NAME": "testserver",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "REMOTE_ADDR": "203.0.113.77",
        "CONTENT_TYPE": "application/json",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        **(headers or {}),
    }
    status: list = []
    response = app(environ, lambda s, h, exc_info=None: status.append(s))
    try:
        b"".join(response)
    finally:
        getattr(response, "close", lambda: None)()
    return status[0]


def _error_events(transport) -> list[dict]:
    events = []
    for envelope in transport.envelopes:
        for item in envelope.items:
            if item.headers.get("type") == "event":
                events.append(json.loads(bytes(item.payload.get_bytes())))
    return events


def _carriers_with(payload, needle: str, path: str = "") -> list[str]:
    """Пути всех носителей, где встречается строка. Путь, а не факт: сообщение
    сторожа должно НАЗЫВАТЬ поле, иначе разбор начнётся с нуля."""
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            found += _carriers_with(value, needle, f"{path}.{key}" if path else str(key))
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            found += _carriers_with(value, needle, f"{path}[{index}]")
    elif isinstance(payload, str) and needle in payload:
        found.append(path or "<корень>")
    return found


@pytest.mark.django_db
@pytest.mark.urls("core.tests.sentry_live_probe_urls")
class TestTheIdentityDoesNotLeaveInAnyCarrier:
    def _run(self, live_sentry) -> dict:
        from djangoProject.wsgi import application

        _call_wsgi(
            application,
            path=PATH,
            query="page=2",
            body=json.dumps({"note": "ok"}).encode(),
            headers={
                "HTTP_X_REQUEST_ID": REQUEST_ID,
                "HTTP_X_APP_TYPE": "client",
                "HTTP_X_EXTERNAL_USER_ID": IDENTITY,
            },
        )
        events = _error_events(live_sentry)
        assert events, "SDK не отправил события — проверять нечего"
        return events[0]

    def test_the_identity_is_in_no_carrier_of_the_real_event(self, live_sentry):
        """Отсутствие И место замены — в одном узле.

        «Личности нет» выполнимо и пустым событием, и событием, где вырезано не
        то. Поэтому рядом стоит утверждение, что след замены лежит ИМЕННО в том
        носителе, из которого личность убрали — в тексте исключения. Критерий
        успеха подмены не должен быть выполним ничем, кроме проверяемого
        утверждения.
        """
        event = self._run(live_sentry)

        carriers = _carriers_with(event, IDENTITY)

        assert carriers == [], (
            "внешняя личность уехала бы в Sentry; носители: " + ", ".join(carriers)
        )
        exception_text = (event["exception"]["values"][-1] or {}).get("value") or ""
        assert "[IDENTITY]" in exception_text, (
            "в тексте исключения нет следа замены — значит вырезали не там, "
            f"а личности нет по другой причине: {exception_text[:80]!r}"
        )

    def test_a_phone_in_the_same_text_is_also_gone(self, live_sentry):
        """Тот же замер показал, что редактура ПДн к событию не применялась
        вовсе: телефон в тексте исключения уходил наружу наравне с
        идентификатором."""
        event = self._run(live_sentry)

        carriers = _carriers_with(event, PHONE)

        assert carriers == [], "телефон уехал бы в Sentry; носители: " + ", ".join(carriers)

    def test_the_event_still_carries_what_it_must(self, live_sentry):
        """Положительная сторона: «личности нет» правда и о пустом событии.

        Поэтому рядом стоит утверждение, что событие ДОШЛО и несёт диагностику:
        класс ошибки, маршрут и correlation id. Без этого зелёный сторож над
        сломанной отправкой выглядел бы точно так же.
        """
        event = self._run(live_sentry)

        values = (event.get("exception") or {}).get("values") or []
        assert values, f"в событии нет исключения: {sorted(event)}"
        assert values[-1]["type"] == "RuntimeError"
        assert "sentry-probe" in str(event.get("transaction") or ""), event.get("transaction")
        assert (event.get("tags") or {}).get("request_id") == REQUEST_ID
        # Диагностика самого падения: место в коде осталось на месте, то есть
        # редактура не съела то, по чему инцидент ищут.
        frames = (values[-1].get("stacktrace") or {}).get("frames") or []
        assert frames, "у исключения нет кадров стека — искать инцидент нечем"
        assert any("sentry_live_probe_urls" in str(f.get("filename") or "") for f in frames)


class TestThePrefilterDoesNotSwallowTheIdentity:
    """Дешёвый префильтр стоит ПЕРЕД редактурой и решает, звать ли её вообще.

    Значит он — не оптимизация, а часть охраны: текст, который он отсёк,
    редактуру не проходит, и личность уходит наружу молча. Отказ был бы
    невидимым — ни исключения, ни записи в журнале, просто чистая строка,
    которую никто не чистил.
    """

    def test_the_prefilter_lets_through_everything_the_patterns_catch(self):
        from core.pii_log_filter import (
            _HAS_PII_CANDIDATE,
            _IDENTITY_RE,
            _IDENTITY_SOURCES,
            redact_pii,
        )

        sources = _IDENTITY_SOURCES.split("|")
        assert sources, "список источников пуст — сторожить нечего"

        missed = []
        for source in sources:
            # БЕЗ цифр и без «@» — намеренно. С `a1b2c3` узел был зелёным и на
            # подмене: префильтр срабатывал на цифрах образца, а не на списке
            # источников, то есть проверялось что угодно, кроме проверяемого
            # утверждения (поймано подменой, а не чтением).
            sample = f"upstream said {source}:abcdef is unknown"
            # Премиссу утверждаем первой: если шаблон личности сам не видит
            # образец, узел проверял бы префильтр на том, что чистить не надо.
            assert _IDENTITY_RE.search(sample), source
            if not _HAS_PII_CANDIDATE.search(sample):
                missed.append(source)
            elif "[IDENTITY]" not in redact_pii(sample):
                missed.append(f"{source} (префильтр пустил, редактура не сработала)")

        assert missed == [], (
            "префильтр отсекает текст, который шаблон личности обязан почистить — "
            f"наружу уйдёт молча: {missed}"
        )
