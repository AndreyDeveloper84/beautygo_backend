"""A5 — Pin the prod.py fail-fast tuple membership for AYLA_INTERNAL_API_TOKEN.

The token gates IsBotServiceWithVerifiedClient (used by every
/api/v1/payments/internal/ + /api/v1/masters/internal/ +
/api/v1/internal/me/ endpoint). When the env var is empty, the
permission silently rejects every cross-service call as 401, which
surfaces in bot-platform as opaque "service unavailable" with no
obvious root cause.

Codex P0-3 (payment create contract) and the founder Block A handoff
both call out this exact failure mode. Adding the token to
``_REQUIRED_PROD_ENV`` means a forgotten env var fails at boot with an
actionable ``ImproperlyConfigured`` instead of paging on-call at
midnight after the first bot payment retry.

The test imports the prod settings module's ``_REQUIRED_PROD_ENV`` tuple
directly so a future drive-by edit that removes the token from the
tuple fails this assertion before it lands.
"""
from __future__ import annotations

import importlib
import os
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured


def _reload_prod_settings():
    """Reload prod settings under the current env. Must run on a copy
    of the env to avoid leaking state between tests."""
    prod_settings = importlib.import_module("djangoProject.settings.prod")
    return importlib.reload(prod_settings)


@pytest.fixture
def prod_required_env_tuple():
    # Import directly so the test does not depend on the live env state.
    from djangoProject.settings.prod import _REQUIRED_PROD_ENV
    return _REQUIRED_PROD_ENV


class TestProdRequiredEnvMembership:
    """Schema-pin for the required-env tuple."""

    def test_ayla_internal_api_token_is_gated(self, prod_required_env_tuple):
        assert "AYLA_INTERNAL_API_TOKEN" in prod_required_env_tuple, (
            "AYLA_INTERNAL_API_TOKEN must be in _REQUIRED_PROD_ENV — "
            "without it bot-platform live calls to "
            "/api/v1/payments/internal/, /api/v1/masters/internal/, "
            "and /api/v1/internal/me/ silently 401. Codex P0-3 + "
            "handoff Block A → A5."
        )

    def test_existing_gates_still_present(self, prod_required_env_tuple):
        # Sanity check that the A5 edit did not displace previous
        # entries. Each is load-bearing for a separate defence layer.
        assert "GOOGLE_CLIENT_ID" in prod_required_env_tuple
        assert "APPLE_CLIENT_ID" in prod_required_env_tuple
        assert "YOOKASSA_WEBHOOK_ALLOWED_IPS" in prod_required_env_tuple


class TestProdBootFailFast:
    """Behavioural test — strict prod boots fail when the token is missing."""

    def test_strict_prod_boot_raises_on_missing_token(self, monkeypatch):
        # Strip the token + provide every other required var so the
        # error message names AYLA_INTERNAL_API_TOKEN specifically.
        monkeypatch.setenv("DJANGO_ENV", "production")
        monkeypatch.delenv("AYLA_INTERNAL_API_TOKEN", raising=False)
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "x")
        monkeypatch.setenv("APPLE_CLIENT_ID", "x")
        monkeypatch.setenv("YOOKASSA_WEBHOOK_ALLOWED_IPS", "127.0.0.1/32")
        # Other required-at-import env (DJANGO_SECRET_KEY etc.) must be
        # present so the test catches the env_strictness raise rather
        # than a generic KeyError from settings.py earlier.
        monkeypatch.setenv("DJANGO_SECRET_KEY", "test-secret")

        with pytest.raises(ImproperlyConfigured) as exc_info:
            _reload_prod_settings()

        assert "AYLA_INTERNAL_API_TOKEN" in str(exc_info.value)

    def test_staging_warn_lets_boot_continue(self, monkeypatch):
        # Same missing-token scenario but with DJANGO_ENV=staging —
        # warn-and-boot, no raise.
        monkeypatch.setenv("DJANGO_ENV", "staging")
        monkeypatch.delenv("AYLA_INTERNAL_API_TOKEN", raising=False)
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "x")
        monkeypatch.setenv("APPLE_CLIENT_ID", "x")
        monkeypatch.setenv("YOOKASSA_WEBHOOK_ALLOWED_IPS", "127.0.0.1/32")
        monkeypatch.setenv("DJANGO_SECRET_KEY", "test-secret")

        with patch("core.env_strictness.logger") as mock_logger:
            _reload_prod_settings()

        # Warning was emitted and named the token.
        assert mock_logger.warning.called
        warn_calls = "".join(
            args[0] % args[1:] if len(args) > 1 else args[0]
            for call in mock_logger.warning.call_args_list
            for args in [call.args]
        )
        assert "AYLA_INTERNAL_API_TOKEN" in warn_calls

    @pytest.fixture(autouse=True)
    def restore_prod_settings_after_each(self, monkeypatch):
        # Reload prod settings without test env overrides at teardown
        # so subsequent tests inherit the canonical baseline.
        yield
        # Restore baseline: leave required vars set to placeholders so
        # the reload at teardown does not raise itself.
        for var in (
            "GOOGLE_CLIENT_ID", "APPLE_CLIENT_ID",
            "YOOKASSA_WEBHOOK_ALLOWED_IPS", "AYLA_INTERNAL_API_TOKEN",
            "DJANGO_SECRET_KEY",
        ):
            os.environ.setdefault(var, "test-restore")
        os.environ["DJANGO_ENV"] = "staging"  # warn, never raise
        try:
            _reload_prod_settings()
        except Exception:  # noqa: BLE001 — teardown best effort
            pass
