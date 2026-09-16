"""Телефон и код подтверждения не попадают в логи из ``users/sms.py``.

Предмет — учётные данные, а не идентификатор: на пути OTP текст сообщения
собирается как ``f"BeautyGO: {code} — ваш код подтверждения"``
(``users/sms.py``), и ветка «отправка выключена» печатала его целиком вместе
с номером.

**Почему это не закрыто значением переменной.** На пилоте ``SMS_ENABLED``
задан (замер главного окна 16.09.2026 00:15 UTC, все три контейнера), поэтому
ветка сегодня не срабатывает. Но умолчание настройки — ``false``
(``settings/base.py``), сторож у неё **один**, а у соседнего пути с
фиксированным кодом их два (``users/services.py``: ``DEBUG`` **и**
``not SMS_ENABLED``). Контейнер, где переменную забыли задать, пишет код в
stdout немедленно: ``LOG_LEVEL`` по умолчанию ``INFO``.

**Почему значение в логе не нужно даже разработчику.** Там, где ветка удобна —
локальная разработка и тесты, ``DEBUG=True`` — код не случайный, а константа
``OTP_DEBUG_CODE`` (``users/services.py``). Лог не сообщает ничего, чего не
знает разработчик. Случайный код печатается ровно в одной конфигурации:
``DEBUG=False`` и ``SMS_ENABLED=False``, то есть staging/prod с выключенной
отправкой — там, где это утечка, а не удобство.

**Ловушка перехвата.** Логгер ``users`` объявлен ``propagate=False``
(``settings/base.py``, блок ``LOGGING``), поэтому корневой обработчик
``caplog`` записей ``users.sms`` **не видит**: тест без подвески handler'а
к самому логгеру прошёл бы на пустом списке и не проверил бы ничего. Ниже —
тот же приём, что в ``appointments/tests/test_payment_required_server_decides_b61.py``.
"""

import ast
import logging
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from users.models import OTPCode
from users.services import OTPService
from users.sms import SMSService

SMS_LOGGER = "users.sms"

#: Тестовый диапазон (префикс 999) — того же вида, что в ``users/tests/conftest.py``.
PHONE = "+79990000001"


@contextmanager
def _capturing_the_sms_log(caplog):
    """Логгер ``users`` стоит ``propagate=False`` (settings/base.py LOGGING),
    поэтому корневой handler ``caplog`` записей ``users.sms`` не видит —
    подвешиваем перехват к самому логгеру."""
    sms_logger = logging.getLogger(SMS_LOGGER)
    sms_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger=SMS_LOGGER):
            yield
    finally:
        sms_logger.removeHandler(caplog.handler)


def _text(caplog) -> str:
    """Весь перехваченный текст одной строкой — и сообщение, и аргументы."""
    return "\n".join(r.getMessage() for r in caplog.records)


class TestTheDevModeBranch:
    """Отправка выключена: событие остаётся, значения уходят."""

    def test_the_message_body_is_not_logged(self, caplog, settings):
        settings.SMS_ENABLED = False
        body = "BeautyGO: 4321 — ваш код подтверждения"

        with _capturing_the_sms_log(caplog):
            SMSService().send(PHONE, body)

        captured = _text(caplog)
        assert captured, "перехват пуст — тест не проверил бы ничего"
        assert "4321" not in captured
        assert body not in captured

    def test_the_phone_is_not_logged(self, caplog, settings):
        settings.SMS_ENABLED = False

        with _capturing_the_sms_log(caplog):
            SMSService().send(PHONE, "любой текст")

        captured = _text(caplog)
        assert captured, "перехват пуст — тест не проверил бы ничего"
        assert PHONE not in captured
        assert PHONE.lstrip("+") not in captured

    def test_the_event_is_still_recorded(self, caplog, settings):
        """Положительная стража: значения убраны, наблюдаемость — нет."""
        settings.SMS_ENABLED = False

        with _capturing_the_sms_log(caplog):
            SMSService().send(PHONE, "любой текст")

        assert "sms.not_sent" in _text(caplog)

    def test_it_still_returns_true(self, settings):
        """Контракт вызывающих не трогаем в этой правке — он назван в теле PR."""
        settings.SMS_ENABLED = False

        assert SMSService().send(PHONE, "любой текст") is True


@pytest.mark.django_db
class TestTheOtpPathWithRealCode:
    """Единственная конфигурация, где код случайный: DEBUG=False и отправка выключена."""

    def test_a_real_random_code_never_reaches_the_log(self, caplog, settings):
        settings.DEBUG = False
        settings.SMS_ENABLED = False

        with _capturing_the_sms_log(caplog):
            OTPService().send_otp(PHONE)

        issued = OTPCode.objects.filter(phone=PHONE).order_by("-created_at").first()
        assert issued is not None, "код не выпущен — проверять нечего"
        assert issued.code != "0000", (
            "при DEBUG=False код обязан быть случайным, иначе тест проверяет не тот путь"
        )
        assert issued.code not in _text(caplog)
        assert PHONE not in _text(caplog)


class TestTheProviderPaths:
    """Отправка включена: номер не печатается ни на успехе, ни на отказе."""

    def test_the_sent_confirmation_does_not_log_the_phone(self, caplog, settings):
        settings.SMS_ENABLED = True
        settings.SMS_RU_API_ID = "test-api-id"  # pragma: allowlist secret

        response = type("R", (), {"json": lambda self: {"status_code": 100}})()
        with _capturing_the_sms_log(caplog), patch("users.sms.requests.get", return_value=response):
            assert SMSService().send(PHONE, "любой текст") is True

        assert PHONE not in _text(caplog)

    def test_the_provider_error_does_not_log_the_phone(self, caplog, settings):
        settings.SMS_ENABLED = True
        settings.SMS_RU_API_ID = "test-api-id"  # pragma: allowlist secret

        response = type(
            "R", (), {"json": lambda self: {"status_code": 202, "status_text": "no route"}},
        )()
        with _capturing_the_sms_log(caplog), patch("users.sms.requests.get", return_value=response):
            assert SMSService().send(PHONE, "любой текст") is False

        assert PHONE not in _text(caplog)


class TestTheTransportFailure:
    """Отказ транспорта: в тексте исключения ``requests`` лежит URL с параметрами.

    Замер (локально, 16.09.2026, без выхода наружу — отказ в соединении на
    закрытый порт и несуществующий домен): у ``ConnectTimeout`` и
    ``ConnectionError`` текст содержит
    ``Max retries exceeded with url: /sms/send?api_id=…&to=…&msg=…`` —
    то есть **ключ API, номер и тело сообщения**. У ``ReadTimeout`` — только
    хост и порт. Форму отказа выбирает сеть, а не мы, поэтому печатать
    исключение целиком нельзя ни при каких обстоятельствах.

    Эта ветка живая ровно тогда, когда отправка **включена**, — то есть в той
    конфигурации, в которой пилот работает сегодня (замер главного окна
    16.09.2026 00:15 UTC: ``SMS_ENABLED=true`` во всех трёх контейнерах).
    """

    #: Форма взята из настоящего замера, а не выдумана.
    LEAKY_TEXT = (
        "HTTPSConnectionPool(host='sms.ru', port=443): Max retries exceeded with url: "
        "/sms/send?api_id=SECRET-API-ID&to=79990000001&msg=BeautyGO%3A+4321&json=1 "
        "(Caused by ConnectTimeoutError(...))"
    )

    def _failing_transport(self):
        import requests

        return patch("users.sms.requests.get", side_effect=requests.ConnectionError(self.LEAKY_TEXT))

    def test_the_api_key_never_reaches_the_log(self, caplog, settings):
        settings.SMS_ENABLED = True
        settings.SMS_RU_API_ID = "SECRET-API-ID"  # pragma: allowlist secret

        with _capturing_the_sms_log(caplog), self._failing_transport():
            assert SMSService().send(PHONE, "BeautyGO: 4321 — ваш код подтверждения") is False

        captured = _text(caplog)
        assert captured, "перехват пуст — тест не проверил бы ничего"
        assert "SECRET-API-ID" not in captured

    def test_the_phone_and_the_body_never_reach_the_log(self, caplog, settings):
        settings.SMS_ENABLED = True
        settings.SMS_RU_API_ID = "SECRET-API-ID"  # pragma: allowlist secret

        with _capturing_the_sms_log(caplog), self._failing_transport():
            assert SMSService().send(PHONE, "BeautyGO: 4321 — ваш код подтверждения") is False

        captured = _text(caplog)
        assert captured, "перехват пуст — тест не проверил бы ничего"
        assert PHONE.lstrip("+") not in captured
        assert "4321" not in captured

    def test_the_failure_class_is_still_logged(self, caplog, settings):
        """Положительная стража: отказ без неё неотличим от «перестали логировать».

        Класс отказа — и есть диагностика: ``ConnectTimeout`` («не дошли»)
        против ``ReadTimeout`` («не ответили») — разные причины и разные
        действия. Сегодня в лог уходит только ``%s`` от исключения, то есть
        его текст; имени класса там нет.
        """
        settings.SMS_ENABLED = True
        settings.SMS_RU_API_ID = "SECRET-API-ID"  # pragma: allowlist secret

        with _capturing_the_sms_log(caplog), self._failing_transport():
            SMSService().send(PHONE, "BeautyGO: 4321 — ваш код подтверждения")

        assert "ConnectionError" in _text(caplog)


class TestTheCensus:
    """Перепись: в аргументах ``logger.*`` этого модуля нет ни номера, ни текста.

    Считается по AST, а не построчно: вызов, открывающий скобку в конце
    строки, для построчного шаблона невидим, и пустой результат читался бы
    как чистота.
    """

    #: ``status_text`` сюда НЕ входит намеренно: это описание отказа от SMS.RU
    #: («no route»), оно операционное, а не личное, и снимать его — потерять
    #: диагностику без выигрыша в приватности.
    FORBIDDEN = {"phone", "message", "code", "msg", "exc", "e"}

    def test_no_personal_identifier_in_logger_arguments(self):
        source = Path(SMSService.__module__.replace(".", "/") + ".py")
        if not source.exists():  # запуск из другого каталога
            import users.sms as sms_module
            source = Path(sms_module.__file__)
        tree = ast.parse(source.read_text(encoding="utf-8"))

        seen_calls = 0
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)):
                continue
            if fn.value.id != "logger":
                continue
            seen_calls += 1
            # Запрещено ПЕРЕДАВАТЬ имя аргументом, а не упоминать его внутри
            # выражения: `type(e).__name__` называет класс отказа и ничего не
            # раскрывает, а `e` отдаёт текст исключения целиком — вместе с URL
            # и строкой запроса. Проверка по узлу самого аргумента, не по
            # `ast.walk` под ним, иначе честная правка осталась бы красной.
            bad = set()
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if isinstance(arg, ast.Name) and arg.id in self.FORBIDDEN:
                    bad.add(arg.id)
                elif isinstance(arg, ast.Attribute) and arg.attr in self.FORBIDDEN:
                    bad.add(arg.attr)
            if bad:
                offenders.append(f"{source.name}:{node.lineno} -> {sorted(bad)}")

        assert seen_calls >= 4, (
            f"нашлось {seen_calls} вызовов logger.* — перепись обязана видеть их все, "
            "иначе пустой скан читается как чистота"
        )
        assert not offenders, "личность в аргументах логирования: " + "; ".join(offenders)
