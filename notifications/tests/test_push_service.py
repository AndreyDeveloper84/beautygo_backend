"""Tests for PushService — stub mode + send wiring.

We don't import firebase_admin in tests; the service initialises lazily
and we mock it at the module level.
"""
from unittest.mock import MagicMock, patch

from django.test import override_settings

from notifications.services import push as push_module
from notifications.services.push import PushService, reset_app_state_for_tests


def _reset():
    reset_app_state_for_tests()


@override_settings(FIREBASE_CREDENTIALS_JSON="", FIREBASE_CREDENTIALS_PATH="")
def test_stub_mode_returns_true_and_logs():
    _reset()
    service = PushService()
    ok = service.send(token="t-123", title="Hi", body="Body")
    # Stub mode is "success" so callers don't loop on retry. The behaviour
    # is intentional — there's nothing real to fix and no message in flight.
    assert ok is True


@override_settings(FIREBASE_CREDENTIALS_JSON="", FIREBASE_CREDENTIALS_PATH="")
def test_send_with_empty_token_returns_false():
    _reset()
    ok = PushService().send(token="", title="Hi", body="Body")
    assert ok is False


def test_send_real_path_calls_firebase_messaging():
    _reset()
    fake_messaging = MagicMock()
    fake_messaging.send.return_value = "mock-message-id"
    fake_messaging.Message = MagicMock()
    fake_messaging.Notification = MagicMock()

    # Force _ensure_app() into "ready" state without actually initialising
    # firebase_admin (no creds, no network).
    with override_settings(
        FIREBASE_CREDENTIALS_JSON="{}",  # non-empty
        FIREBASE_CREDENTIALS_PATH="",
    ), patch.object(push_module, "_app_state", MagicMock()), patch.dict(
        "sys.modules", {"firebase_admin.messaging": fake_messaging},
    ):
        with patch.dict(
            "sys.modules", {"firebase_admin": MagicMock(messaging=fake_messaging)},
        ):
            ok = PushService().send(
                token="real-tok", title="t", body="b", data={"x": 1},
            )

    assert ok is True
    fake_messaging.send.assert_called_once()


def test_send_real_path_handles_send_failure():
    _reset()
    fake_messaging = MagicMock()
    fake_messaging.send.side_effect = RuntimeError("FCM down")
    fake_messaging.Message = MagicMock()
    fake_messaging.Notification = MagicMock()

    with override_settings(
        FIREBASE_CREDENTIALS_JSON="{}",
        FIREBASE_CREDENTIALS_PATH="",
    ), patch.object(push_module, "_app_state", MagicMock()), patch.dict(
        "sys.modules", {"firebase_admin": MagicMock(messaging=fake_messaging)},
    ), patch.dict(
        "sys.modules", {"firebase_admin.messaging": fake_messaging},
    ):
        ok = PushService().send(token="t", title="t", body="b")

    assert ok is False
