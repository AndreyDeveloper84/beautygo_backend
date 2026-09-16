"""Токен провайдера не попадает в логи из ``users/social_auth.py`` (DRF-2025).

Предмет — **учётные данные**: `access_token` ВК и `id_token` Google уезжают в
строке запроса (``users/social_auth.py:92-99`` и ``:125-128``), а при сетевом
отказе текст исключения ``requests`` несёт URL целиком, и ``logger.warning(...,
%s, e)`` печатает его. Прочитавший лог может войти как этот пользователь, пока
токен жив.

**Почему текст несёт строку запроса.** Замерено на пинованной версии
(``requirements.txt:20`` — ``requests==2.34.2``): ``adapters.request_url()``
отдаёт в urllib3 ``request.path_url``, то есть путь **со строкой запроса**, а
сообщение собирает urllib3: ``f"Max retries exceeded with url: {url} (Caused by
{reason!r})"``. Поэтому исключение здесь строится настоящими классами, а не
литералом: литерал проверял бы мою выдумку, а не поведение связки.
**Предел:** ``urllib3`` в ``requirements.txt`` не пинован вовсе — формат
принадлежит версии, которую разрешило окружение.

**Ловушка перехвата** (названа ayla-9d в #488): логгер ``users`` объявлен
``propagate=False`` (``djangoProject/settings/base.py:1337``), поэтому корневой
обработчик ``caplog`` записей ``users.social_auth`` не видит — узел без
подвески handler'а к самому логгеру прошёл бы на пустом списке. Отсюда
``assert captured`` первым в каждом узле.

**Чего эти узлы НЕ доказывают:** токен остаётся в строке запроса и после
правки — он по-прежнему доступен трассе исключения, логам прокси и любому
будущему ``logger.exception`` на этом пути. Исток закрывается отдельным листом.
"""

import logging
from contextlib import contextmanager
from unittest.mock import patch

import pytest
import requests
from urllib3.exceptions import MaxRetryError

from users.social_auth import (
    SocialAuthTokenError,
    verify_google_token,
    verify_vk_token,
)

SOCIAL_LOGGER = "users.social_auth"

#: Токены тестовые, но по форме те же, что приходят от провайдеров.
VK_TOKEN = "vk1.a.ZZZ-test-access-token-0123456789"  # pragma: allowlist secret
GOOGLE_TOKEN = "eyJhbGciOiJSUzI1NiIsImtpZCI6InRlc3QifQ.test-id-token"  # pragma: allowlist secret


@contextmanager
def _capturing_the_social_log(caplog):
    """``users`` стоит ``propagate=False``: подвешиваем перехват к логгеру.

    Идиома **скопирована** из #488 (``users/tests/test_sms_credentials_never_in_logs_2020.py``,
    лист DRF-2020, автор ayla-9d): предмет общий — логгер ``users`` с
    ``propagate=False`` в ``djangoProject/settings/base.py:1337``. Импортировать
    нечего: тот PR не слит. **При слиянии обоих свести в одно место**, иначе
    починка перехвата в одном файле не дойдёт до другого.
    """
    social_logger = logging.getLogger(SOCIAL_LOGGER)
    social_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger=SOCIAL_LOGGER):
            yield
    finally:
        social_logger.removeHandler(caplog.handler)


def _text(caplog) -> str:
    """Весь перехваченный текст одной строкой — и сообщение, и аргументы."""
    return "\n".join(record.getMessage() for record in caplog.records)


def _connection_error_for(url_with_query: str) -> requests.ConnectionError:
    """Настоящий отказ: текст собирают urllib3 и requests, не тест.

    ``MaxRetryError`` получает тот самый ``path_url``, который requests отдаёт
    в urllib3 — путь вместе со строкой запроса.
    """
    reason = OSError("[Errno 111] Connection refused")
    inner = MaxRetryError(pool=None, url=url_with_query, reason=reason)
    return requests.ConnectionError(inner)


class TestVkTokenNeverReachesTheLog:
    URL = f"/method/users.get?access_token={VK_TOKEN}&fields=first_name&v=5.199"

    def test_a_vk_transport_failure_does_not_log_the_access_token(self, caplog):
        error = _connection_error_for(self.URL)

        with _capturing_the_social_log(caplog), patch(
            "users.social_auth.requests.get", side_effect=error
        ):
            with pytest.raises(SocialAuthTokenError):
                verify_vk_token(VK_TOKEN)

        captured = _text(caplog)
        assert captured, "перехват пуст — узел не проверил бы ничего"
        assert VK_TOKEN not in captured
        assert "access_token" not in captured

    def test_the_vk_failure_is_still_recorded_by_class(self, caplog):
        """Положительная стража: значение убрано, наблюдаемость — нет."""
        error = _connection_error_for(self.URL)

        with _capturing_the_social_log(caplog), patch(
            "users.social_auth.requests.get", side_effect=error
        ):
            with pytest.raises(SocialAuthTokenError):
                verify_vk_token(VK_TOKEN)

        captured = _text(caplog)
        assert captured, "перехват пуст — узел не проверил бы ничего"
        assert "ConnectionError" in captured, captured
        assert VK_TOKEN not in captured


class TestGoogleTokenNeverReachesTheLog:
    URL = f"/tokeninfo?id_token={GOOGLE_TOKEN}"

    def test_a_google_transport_failure_does_not_log_the_id_token(self, caplog):
        error = _connection_error_for(self.URL)

        with _capturing_the_social_log(caplog), patch(
            "users.social_auth.requests.get", side_effect=error
        ):
            with pytest.raises(SocialAuthTokenError):
                verify_google_token(GOOGLE_TOKEN)

        captured = _text(caplog)
        assert captured, "перехват пуст — узел не проверил бы ничего"
        assert GOOGLE_TOKEN not in captured
        assert "id_token" not in captured

    def test_the_google_failure_is_still_recorded_by_class(self, caplog):
        """Положительная стража для второго провайдера."""
        error = _connection_error_for(self.URL)

        with _capturing_the_social_log(caplog), patch(
            "users.social_auth.requests.get", side_effect=error
        ):
            with pytest.raises(SocialAuthTokenError):
                verify_google_token(GOOGLE_TOKEN)

        captured = _text(caplog)
        assert captured, "перехват пуст — узел не проверил бы ничего"
        assert "ConnectionError" in captured, captured
        assert GOOGLE_TOKEN not in captured
