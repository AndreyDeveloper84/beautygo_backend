"""Tests for NotificationService.send + .deliver.

Push delivery is mocked so tests don't require firebase-admin. SMS
delivery goes through users.sms.SMSService which is already mocked
(SMS_ENABLED=False in test settings).
"""
from unittest.mock import patch

import pytest

from notifications.models import Notification
from notifications.services.dispatcher import NotificationService
from users.models import DeviceToken, User


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="ndclient",
        password="x",
        role="client",
        phone="+79991110000",
    )


@pytest.fixture
def specialist_user(db):
    user = User.objects.create_user(
        username="ndspec",
        password="x",
        role="specialist",
        phone="+79992220000",
    )
    profile = user.specialist_profile
    profile.display_name = "Test Master"
    profile.save(update_fields=["display_name"])
    return user


@pytest.mark.django_db
class TestSendQueuesNotification:
    def test_send_persists_pending_row_and_queues_task(self, client_user):
        with patch(
            "notifications.tasks.deliver_notification.delay",
        ) as mock_delay:
            n = NotificationService().send(
                user=client_user,
                template_id="appointment_created_client",
                context={
                    "service_name": "Маникюр",
                    "specialist_name": "Елена",
                    "client_name": "Анна",
                    "date_time": "14:00 26.04",
                    "appointment_id": "a1",
                },
            )
        assert n.status == Notification.Status.PENDING
        assert n.title == "Запись подтверждена"
        assert "Маникюр" in n.body
        assert n.deep_link == "beautygo-client://appointment/a1"
        mock_delay.assert_called_once_with(str(n.id))


@pytest.mark.django_db
class TestDeliverPushChannel:
    def test_deliver_push_with_active_token_succeeds(self, client_user):
        DeviceToken.objects.create(
            user=client_user,
            token="real-token",
            app_type="client",
            platform="ios",
            is_active=True,
        )
        n = Notification.objects.create(
            user=client_user,
            template_id="appointment_created_client",
            channel=Notification.Channel.PUSH,
            title="t",
            body="b",
            data={"appointment_id": "a1"},
            deep_link="beautygo-client://appointment/a1",
            status=Notification.Status.PENDING,
        )
        with patch(
            "notifications.services.push.PushService",
        ) as MockPush:
            MockPush.return_value.send.return_value = True
            NotificationService().deliver(n)
        n.refresh_from_db()
        assert n.status == Notification.Status.SENT
        assert n.sent_at is not None

    def test_deliver_push_no_tokens_marks_failed(self, client_user):
        n = Notification.objects.create(
            user=client_user,
            template_id="appointment_created_client",
            channel=Notification.Channel.PUSH,
            title="t",
            body="b",
            data={},
            status=Notification.Status.PENDING,
        )
        NotificationService().deliver(n)
        n.refresh_from_db()
        assert n.status == Notification.Status.FAILED
        assert "no tokens" in n.error.lower() or "failed" in n.error.lower()

    def test_deliver_filters_tokens_by_template_app_type(self, specialist_user):
        # Specialist user has a `client` token that should NOT receive a
        # specialist-side push.
        DeviceToken.objects.create(
            user=specialist_user,
            token="wrong-app-token",
            app_type="client",
            platform="android",
            is_active=True,
        )
        n = Notification.objects.create(
            user=specialist_user,
            template_id="appointment_created_specialist",
            channel=Notification.Channel.PUSH,
            title="t",
            body="b",
            data={},
            status=Notification.Status.PENDING,
        )
        with patch(
            "notifications.services.push.PushService",
        ) as MockPush:
            NotificationService().deliver(n)
        # Push never called — token's app_type=client doesn't match
        # template's app_type=pro
        assert not MockPush.return_value.send.called


@pytest.mark.django_db
class TestDeliverBothChannelFallback:
    def test_push_success_skips_sms(self, client_user):
        DeviceToken.objects.create(
            user=client_user, token="t", app_type="client",
            platform="ios", is_active=True,
        )
        n = Notification.objects.create(
            user=client_user,
            template_id="appointment_reminder_1h",
            channel=Notification.Channel.BOTH,
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
        with patch(
            "notifications.services.push.PushService",
        ) as MockPush, patch(
            "users.sms.SMSService.send",
        ) as mock_sms:
            MockPush.return_value.send.return_value = True
            NotificationService().deliver(n)
        n.refresh_from_db()
        assert n.status == Notification.Status.SENT
        mock_sms.assert_not_called()

    def test_push_failure_falls_back_to_sms(self, client_user):
        DeviceToken.objects.create(
            user=client_user, token="t", app_type="client",
            platform="ios", is_active=True,
        )
        n = Notification.objects.create(
            user=client_user,
            template_id="appointment_reminder_1h",
            channel=Notification.Channel.BOTH,
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
        with patch(
            "notifications.services.push.PushService",
        ) as MockPush, patch(
            "users.sms.SMSService.send",
        ) as mock_sms:
            MockPush.return_value.send.return_value = False
            mock_sms.return_value = True
            NotificationService().deliver(n)
        n.refresh_from_db()
        assert n.status == Notification.Status.SENT
        mock_sms.assert_called_once()
        # SMS got the rendered text from the template
        args, _ = mock_sms.call_args
        assert client_user.phone in args
        assert "Елена" in args[1]


@pytest.mark.django_db
class TestDeliverIdempotency:
    def test_deliver_skips_already_sent(self, client_user):
        n = Notification.objects.create(
            user=client_user,
            template_id="appointment_created_client",
            channel=Notification.Channel.PUSH,
            title="t",
            body="b",
            data={},
            status=Notification.Status.SENT,
        )
        with patch(
            "notifications.services.push.PushService",
        ) as MockPush:
            NotificationService().deliver(n)
        # No push attempted — the row was already terminal.
        assert not MockPush.return_value.send.called
