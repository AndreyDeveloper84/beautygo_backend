"""Firebase Cloud Messaging client.

Production sends through firebase-admin; dev / CI / fresh deploys
without creds run in stub mode (logs the would-be payload, returns
success). Mirrors the Sentry SDK pattern — empty config doesn't crash
boot, just downgrades behaviour.

Credential resolution order (first non-empty wins):

1. ``settings.FIREBASE_CREDENTIALS_JSON`` — JSON content as a string.
   Used when GitHub Secrets injects the whole service-account file via
   a single env var (CI workflow path).
2. ``settings.FIREBASE_CREDENTIALS_PATH`` — filesystem path to the
   service-account JSON file. Useful for local dev where holding the
   blob in-shell is ugly.
3. Empty → stub mode.

Initialization is lazy — the firebase-admin SDK is heavy and we don't
want to pay its cost (or fail at boot) for environments that never push.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


# Module-level handle so we initialize ``firebase_admin`` exactly once.
# None means "haven't tried yet"; False means "tried, no creds — stub mode".
_app_state: Any = None


def _ensure_app() -> Any:
    """Return the initialized ``firebase_admin`` app, or ``False`` if no
    credentials were provided. Idempotent."""
    global _app_state
    if _app_state is not None:
        return _app_state

    json_blob = getattr(settings, "FIREBASE_CREDENTIALS_JSON", "")
    path = getattr(settings, "FIREBASE_CREDENTIALS_PATH", "")

    if not json_blob and not path:
        logger.warning(
            "firebase.no_credentials — PushService will run in stub mode "
            "(messages will be logged, not delivered).",
        )
        _app_state = False
        return _app_state

    try:
        import firebase_admin
        from firebase_admin import credentials
    except ImportError:
        logger.error("firebase.import_failed — install firebase-admin")
        _app_state = False
        return _app_state

    try:
        if json_blob:
            cred = credentials.Certificate(json.loads(json_blob))
        else:
            cred = credentials.Certificate(path)
        _app_state = firebase_admin.initialize_app(cred)
        logger.info("firebase.initialized")
    except Exception as exc:  # noqa: BLE001
        logger.exception("firebase.init_failed: %s", exc)
        _app_state = False

    return _app_state


def reset_app_state_for_tests() -> None:
    """Tests call this between cases to force re-init against patched
    settings. Production code never invokes it."""
    global _app_state
    _app_state = None


class PushService:
    """Send a single FCM message to one device token.

    The ``send`` method is intentionally narrow — caller is responsible
    for resolving the token, building the payload, and updating the
    ``Notification`` row. Keeping this layer thin makes mocking trivial
    in tests.
    """

    def send(
        self,
        token: str,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> bool:
        """Deliver one push. Returns True on success, False on any
        failure (logs the cause). Empty / stub credentials always
        return True after logging the payload."""
        if not token:
            logger.warning("push.missing_token")
            return False

        app = _ensure_app()
        if app is False:
            logger.info(
                "push.stub token=%s title=%r body=%r data=%s",
                token[:12] + "…", title, body, data or {},
            )
            return True

        try:
            from firebase_admin import messaging
        except ImportError:
            logger.error("push.import_failed — firebase-admin not installed")
            return False

        # firebase-admin requires every data value to be a string.
        # Coerce here so callers can pass ints / UUIDs without thinking
        # about it.
        data_str = {k: str(v) for k, v in (data or {}).items()}

        message = messaging.Message(
            token=token,
            notification=messaging.Notification(title=title, body=body),
            data=data_str,
        )
        try:
            message_id = messaging.send(message)
        except Exception as exc:  # noqa: BLE001 — surface every failure mode
            logger.exception("push.send_failed: %s", exc)
            return False

        logger.info("push.sent id=%s", message_id)
        return True
