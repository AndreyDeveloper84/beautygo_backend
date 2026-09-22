"""DRF-2272 — каждая строка, уходящая в постоянный журнал, проходит фильтр ПДн.

Логи контейнеров каталога переезжают в journald хоста и живут дольше
выкладки (≥7 дней). До этого листа в ``LOGGING`` каталога фильтра ПДн не было
вовсе (только ``request_id``), а мимо Django-конфига шли ещё два пути:

* gunicorn пишет access- и error-логи своими обработчиками
  (``--access-logfile -``): строка «метод путь?query статус» — как есть;
* Celery 5 по умолчанию захватывает root (``worker_hijack_root_logger``) и
  ставит свой обработчик — логгеры, идущие в root, теряли бы фильтр в воркере.

Фильтр — порт ``PIIRedactingFilter`` бота (DRF-859). Узлы: телефон, e-mail и
карта в сообщении и в access-строке замаскированы; положительная пара —
строка на месте.
"""

from __future__ import annotations

import copy
import io
import logging
import logging.config
from collections.abc import Iterator
from typing import Any

import pytest
from django.conf import settings

PHONE = "+7 905 123 4567"
EMAIL = "client@example.com"
CARD = "4111 1111 1111 1111"


def _gunicorn_like_loggers() -> tuple[logging.Logger, logging.Logger]:
    """То, что делает ``gunicorn.glogging.Logger.setup`` с ``accesslog=-`` и
    ``errorlog=-``: свои обработчики и ``propagate=False``. Настоящий gunicorn
    на Windows не импортируется (``grp``) — узел с ним ниже идёт в CI."""
    access = logging.getLogger("gunicorn.access")
    error = logging.getLogger("gunicorn.error")
    for lg in (access, error):
        lg.handlers[:] = [logging.StreamHandler(io.StringIO())]
        lg.propagate = False
        lg.setLevel(logging.INFO)
    return access, error


def _apply_django_logging() -> io.StringIO:
    stream = io.StringIO()
    config: dict[str, Any] = copy.deepcopy(dict(settings.LOGGING))
    config["handlers"]["console"]["stream"] = stream
    config["handlers"]["console"]["formatter"] = "human"
    logging.config.dictConfig(config)
    return stream


@pytest.fixture
def sink() -> Iterator[tuple[io.StringIO, logging.Logger, logging.Logger]]:
    """Порядок как в бою: сначала свои обработчики ставит gunicorn, затем при
    загрузке приложения Django применяет ``LOGGING`` — с консолью в буфер."""
    access, error = _gunicorn_like_loggers()
    stream = _apply_django_logging()
    try:
        yield stream, access, error
    finally:
        logging.config.dictConfig(settings.LOGGING)


def _assert_masked(text: str) -> None:
    assert "[PHONE]" in text and "[EMAIL]" in text and "[CARD]" in text
    for raw in (PHONE, EMAIL, CARD, "905 123 4567", "4111 1111"):
        assert raw not in text, raw


class TestGunicornGoesThroughTheFilter:
    def test_the_access_line(self, sink) -> None:
        stream, access, _ = sink
        # Тот же вызов, что делает gunicorn: формат из cfg и словарь атомов.
        access.info(
            '%(h)s "%(r)s" %(s)s',
            {
                "h": "10.0.0.1",
                "r": f"GET /api/v1/x?phone={PHONE}&email={EMAIL}&card={CARD} HTTP/1.1",
                "s": "200",
            },
        )
        out = stream.getvalue()
        assert '"GET /api/v1/x?phone=' in out  # положительно: строка записана
        _assert_masked(out)

    def test_the_error_line(self, sink) -> None:
        stream, _, error = sink
        error.error("Error handling request for %s %s %s", PHONE, EMAIL, CARD)
        out = stream.getvalue()
        assert "Error handling request" in out
        _assert_masked(out)


class TestRealGunicorn:
    def test_the_real_logger_setup_is_overridden(self) -> None:
        """В CI (Linux) — настоящий ``gunicorn.glogging.Logger``."""
        pytest.importorskip("grp")
        from gunicorn.config import Config
        from gunicorn.glogging import Logger

        cfg = Config()
        cfg.set("accesslog", "-")
        cfg.set("errorlog", "-")
        glog = Logger(cfg)
        stream = _apply_django_logging()
        try:
            glog.access_log.info('"%(r)s"', {"r": f"GET /x?phone={PHONE} HTTP/1.1"})
            out = stream.getvalue()
            assert '"GET /x?phone=' in out
            assert PHONE not in out
        finally:
            logging.config.dictConfig(settings.LOGGING)


class TestAppLinesAreFiltered:
    def test_a_named_app_logger(self, sink) -> None:
        stream, _, _ = sink
        logging.getLogger("users").info("client %s %s %s", PHONE, EMAIL, CARD)
        out = stream.getvalue()
        assert "client" in out
        _assert_masked(out)

    def test_a_logger_that_goes_to_root(self, sink) -> None:
        stream, _, _ = sink
        logging.getLogger("nutrition.somewhere").warning("client %s %s %s", PHONE, EMAIL, CARD)
        out = stream.getvalue()
        assert "client" in out
        _assert_masked(out)


class TestCeleryKeepsTheRootHandler:
    def test_the_worker_does_not_hijack_root(self) -> None:
        from djangoProject.celery import app

        assert settings.CELERY_WORKER_HIJACK_ROOT_LOGGER is False
        assert app.conf.worker_hijack_root_logger is False
