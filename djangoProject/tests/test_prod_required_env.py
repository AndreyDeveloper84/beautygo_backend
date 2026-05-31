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

The tests use the same reload pattern as ``test_social_auth.py`` —
``importlib.import_module`` after env injection — because the tuple
lives in prod.py whose top-level ``enforce_required_env`` call would
otherwise raise during a plain import inside the strict test env.
"""
from __future__ import annotations

import importlib
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured


def _reload_prod(monkeypatch, **env_overrides):
    """Reload djangoProject.settings.prod with env overrides applied.

    Same shape as users.tests.test_social_auth.TestProdOAuthFailFast —
    necessary because prod.py runs ``enforce_required_env`` at import
    time. Without explicit env injection the import raises before the
    test body runs.
    """
    for key, value in env_overrides.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    sys.modules.pop("djangoProject.settings.prod", None)
    sys.modules.pop("djangoProject.settings.base", None)
    return importlib.import_module("djangoProject.settings.prod")


# Baseline env where every required var IS set — each test can flip
# one entry to None and assert the specific raise / behaviour.
_FULL_ENV = dict(
    DJANGO_SECRET_KEY="test-secret",
    GOOGLE_CLIENT_ID="google-id",
    APPLE_CLIENT_ID="apple-id",
    YOOKASSA_WEBHOOK_ALLOWED_IPS="185.71.76.0/27",
    AYLA_INTERNAL_API_TOKEN="bearer-token",
)


class TestProdRequiredEnvMembership:
    """Schema-pin for the required-env tuple."""

    def test_ayla_internal_api_token_is_gated(self, monkeypatch):
        module = _reload_prod(monkeypatch, **_FULL_ENV)
        assert "AYLA_INTERNAL_API_TOKEN" in module._REQUIRED_PROD_ENV, (
            "AYLA_INTERNAL_API_TOKEN must be in _REQUIRED_PROD_ENV — "
            "without it bot-platform live calls to "
            "/api/v1/payments/internal/, /api/v1/masters/internal/, "
            "and /api/v1/internal/me/ silently 401. Codex P0-3 + "
            "handoff Block A → A5."
        )

    def test_existing_gates_still_present(self, monkeypatch):
        module = _reload_prod(monkeypatch, **_FULL_ENV)
        # Sanity check that the A5 edit did not displace previous
        # entries. Each is load-bearing for a separate defence layer.
        assert "GOOGLE_CLIENT_ID" in module._REQUIRED_PROD_ENV
        assert "APPLE_CLIENT_ID" in module._REQUIRED_PROD_ENV
        assert "YOOKASSA_WEBHOOK_ALLOWED_IPS" in module._REQUIRED_PROD_ENV


class TestProdBootFailFast:
    """Behavioural test — strict prod boot fails when the token is missing."""

    def test_strict_prod_boot_raises_on_missing_token(self, monkeypatch):
        # Strip the token + provide every other required var so the
        # error message names AYLA_INTERNAL_API_TOKEN specifically.
        monkeypatch.setenv("DJANGO_ENV", "production")
        with pytest.raises(ImproperlyConfigured) as exc_info:
            _reload_prod(
                monkeypatch,
                **{**_FULL_ENV, "AYLA_INTERNAL_API_TOKEN": None},
            )
        assert "AYLA_INTERNAL_API_TOKEN" in str(exc_info.value)

    def test_staging_warn_lets_boot_continue(self, monkeypatch):
        # Same missing-token scenario but with DJANGO_ENV=staging —
        # warn-and-boot, no raise.
        monkeypatch.setenv("DJANGO_ENV", "staging")
        module = _reload_prod(
            monkeypatch,
            **{**_FULL_ENV, "AYLA_INTERNAL_API_TOKEN": None},
        )
        # Reload succeeded — the boot did not abort.
        assert hasattr(module, "_REQUIRED_PROD_ENV")

    def test_imports_when_all_required_set(self, monkeypatch):
        # Belt-and-suspenders: a fully configured env reaches the end
        # of prod.py without raising.
        monkeypatch.setenv("DJANGO_ENV", "production")
        module = _reload_prod(monkeypatch, **_FULL_ENV)
        assert module.AYLA_INTERNAL_API_TOKEN == "bearer-token"
