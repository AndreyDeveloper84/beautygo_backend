"""SMS service — sends messages via SMS.RU API."""

import logging
import re

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

SMS_RU_SEND_URL = "https://sms.ru/sms/send"

#: Описание отказа пишет провайдер, а не мы: это единственное поле в наших
#: логах с неконтролируемым содержимым. Длинная цифровая последовательность
#: в нём — почти наверняка номер, который мы же и передали.
_LONG_DIGIT_RUN = re.compile(r"\d{7,}")


def _without_long_digit_runs(text: str) -> str:
    """Снять из чужого текста то, что похоже на номер, оставив слова."""
    return _LONG_DIGIT_RUN.sub("…", text)


class SMSError(Exception):
    """SMS delivery failed."""
    pass


class SMSService:
    """
    Sends SMS via SMS.RU API.

    Usage:
        sms = SMSService()
        sms.send("+79991234567", "Your code: 123456")

    If SMS_ENABLED=false, records the event and does NOT send.
    If SMS.RU is unreachable, logs the failure class but does NOT raise
    (best-effort).

    Ни номер, ни текст сообщения в логи не попадают: на пути OTP текст —
    это код подтверждения, а строка запроса к провайдеру несёт ещё и
    ключ API. Подробности и замеры — в тестах
    ``users/tests/test_sms_credentials_never_in_logs_2020.py``.
    """

    def send(self, phone: str, message: str) -> bool:
        """
        Send SMS to phone number.

        Returns True if sent (or logged in dev mode), False on failure.
        """
        if not getattr(settings, 'SMS_ENABLED', False):
            logger.info("sms.not_sent reason=sending_disabled")
            return True

        api_id = getattr(settings, 'SMS_RU_API_ID', '')
        if not api_id:
            logger.error("SMS_RU_API_ID not configured, cannot send SMS")
            return False

        return self._send_via_sms_ru(phone, message, api_id)

    def _send_via_sms_ru(
        self, phone: str, message: str, api_id: str,
    ) -> bool:
        """Send via SMS.RU HTTP API."""
        params = {
            "api_id": api_id,
            "to": phone.lstrip("+"),
            "msg": message,
            "json": 1,
        }
        sender = getattr(settings, 'SMS_RU_SENDER', '')
        if sender:
            params["from"] = sender

        try:
            timeout = getattr(settings, 'SMS_RU_TIMEOUT', 10)
            response = requests.get(
                SMS_RU_SEND_URL,
                params=params,
                timeout=timeout,
            )
            data = response.json()
        except requests.RequestException as e:
            logger.error("sms.transport_failed error=%s", type(e).__name__)
            return False
        except ValueError:
            logger.error("sms.provider_non_json")
            return False

        # SMS.RU response: status_code=100 means success
        status_code = data.get("status_code")
        if status_code == 100:
            logger.info("sms.sent status=ok")
            return True

        # Log the error with SMS.RU status code
        status_text = data.get("status_text", "Unknown error")
        logger.error(
            "sms.provider_error code=%s text=%s",
            status_code, _without_long_digit_runs(status_text),
        )
        return False

    def send_otp(self, phone: str, code: str) -> bool:
        """Send OTP code via SMS."""
        message = f"BeautyGO: {code} — ваш код подтверждения"
        return self.send(phone, message)
