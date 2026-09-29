"""DRF-2643 — выключенная отправка SMS — «не отправлено», а не «успех».

До листа ``SMSService.send`` при ``SMS_ENABLED=false`` возвращал ``True``, и
вызывающий не мог отличить «отправлено» от «не отправлено, потому что
выключено»: уведомление по SMS становилось ``SENT`` с ``sent_at``. На пилоте
``SMS_ENABLED=true`` — дефект спящий.

Узлы — пары, которые обязаны различаться: выключено → не «отправлено»;
провайдер принял → «отправлено». «send отработал» прошло бы при дефекте.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from notifications.models import Notification
from notifications.services.dispatcher import NotificationService
from users.models import User
from users.sms import SMSService


def _provider_accepts():
    response = MagicMock()
    response.json.return_value = {"status_code": 100}
    return patch("users.sms.requests.get", return_value=response)


class TestTheServiceSaysWhetherItSent:
    def test_disabled_is_not_sent(self, settings):
        settings.SMS_ENABLED = False
        with _provider_accepts() as get:
            assert SMSService().send("+79990000001", "текст") is False
        get.assert_not_called()

    def test_accepted_by_the_provider_is_sent(self, settings):
        settings.SMS_ENABLED = True
        settings.SMS_RU_API_ID = "test-api-id"  # pragma: allowlist secret
        with _provider_accepts() as get:
            assert SMSService().send("+79990000001", "текст") is True
        get.assert_called_once()


@pytest.mark.django_db
class TestAnSmsNotificationIsNotMarkedSentWhenNothingWasSent:
    @staticmethod
    def _notification() -> Notification:
        user = User.objects.create_user(
            username="sms2643", password="x", role="client", phone="+79990002643",
        )
        return Notification.objects.create(
            user=user,
            template_id="appointment_reminder_1h",
            channel=Notification.Channel.SMS,
            title="t",
            body="b",
            data={
                "specialist_name": "Елена",
                "service_name": "Маникюр",
                "date_time": "14:00 26.04",
                "address": "Пушкина 10",
                "appointment_id": "a1",
            },
            status=Notification.Status.PENDING,
        )

    def test_disabled_sending_is_failed_with_its_reason(self, settings):
        settings.SMS_ENABLED = False
        n = self._notification()
        with _provider_accepts() as get:
            NotificationService().deliver(n)
        n.refresh_from_db()
        assert n.status == Notification.Status.FAILED
        assert n.error == "sms sending disabled"
        assert n.sent_at is None
        get.assert_not_called()

    def test_sending_accepted_by_the_provider_is_sent(self, settings):
        settings.SMS_ENABLED = True
        settings.SMS_RU_API_ID = "test-api-id"  # pragma: allowlist secret
        n = self._notification()
        with _provider_accepts():
            NotificationService().deliver(n)
        n.refresh_from_db()
        assert n.status == Notification.Status.SENT
        assert n.sent_at is not None
