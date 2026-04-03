"""SMS service — sends messages via SMS.RU API."""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

SMS_RU_SEND_URL = "https://sms.ru/sms/send"


class SMSError(Exception):
    """SMS delivery failed."""
    pass


class SMSService:
    """
    Sends SMS via SMS.RU API.

    Usage:
        sms = SMSService()
        sms.send("+79991234567", "Your code: 123456")

    If SMS_ENABLED=false, logs the message instead of sending.
    If SMS.RU is unreachable, logs error but does NOT raise (best-effort).
    """

    def send(self, phone: str, message: str) -> bool:
        """
        Send SMS to phone number.

        Returns True if sent (or logged in dev mode), False on failure.
        """
        if not getattr(settings, 'SMS_ENABLED', False):
            logger.info("SMS [dev mode] to=%s: %s", phone, message)
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
            logger.error("SMS.RU request failed: %s", e)
            return False
        except ValueError:
            logger.error("SMS.RU returned non-JSON response")
            return False

        # SMS.RU response: status_code=100 means success
        status_code = data.get("status_code")
        if status_code == 100:
            logger.info("SMS sent to=%s status=ok", phone)
            return True

        # Log the error with SMS.RU status code
        status_text = data.get("status_text", "Unknown error")
        logger.error(
            "SMS.RU error to=%s code=%s text=%s",
            phone, status_code, status_text,
        )
        return False

    def send_otp(self, phone: str, code: str) -> bool:
        """Send OTP code via SMS."""
        message = f"BeautyGO: {code} — ваш код подтверждения"
        return self.send(phone, message)
