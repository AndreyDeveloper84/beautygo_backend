"""Tests for core.env_strictness — DJANGO_ENV gate on prod.py fail-fast."""
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured

from core.env_strictness import enforce_required_env, is_strict_production


class TestIsStrictProduction:
    @pytest.mark.parametrize("value,expected", [
        ("production", True),
        ("PRODUCTION", True),  # case-insensitive
        ("  production  ", True),  # tolerant of whitespace
        ("staging", False),
        ("dev", False),
        ("", False),  # empty falls through to non-strict
    ])
    def test_label_resolution(self, monkeypatch, value, expected):
        monkeypatch.setenv("DJANGO_ENV", value)
        assert is_strict_production() is expected

    def test_unset_defaults_to_strict(self, monkeypatch):
        # Default = production = fail-closed when ops forgets the var
        monkeypatch.delenv("DJANGO_ENV", raising=False)
        assert is_strict_production() is True


class TestEnforceRequiredEnv:
    def test_no_missing_is_a_no_op(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "production")
        # Should not raise even in strict mode
        enforce_required_env([], "any purpose")

    def test_strict_prod_raises_on_missing(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "production")
        with pytest.raises(ImproperlyConfigured) as exc_info:
            enforce_required_env(["GOOGLE_CLIENT_ID"], "OAuth audience check")
        assert "GOOGLE_CLIENT_ID" in str(exc_info.value)
        assert "OAuth audience check" in str(exc_info.value)

    def test_staging_warns_instead_of_raising(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "staging")
        # Patch the module-level logger directly — settings.LOGGING may
        # filter/redirect named loggers, making caplog unreliable here.
        with patch("core.env_strictness.logger") as mock_logger:
            # Must not raise
            enforce_required_env(
                ["GOOGLE_CLIENT_ID", "APPLE_CLIENT_ID"],
                "OAuth audience check",
            )
        assert mock_logger.warning.called
        # First positional arg is the format string; remaining are %-args.
        args, _ = mock_logger.warning.call_args
        rendered = args[0] % args[1:]
        assert "GOOGLE_CLIENT_ID" in rendered
        assert "APPLE_CLIENT_ID" in rendered
        assert "DJANGO_ENV=staging" in rendered

    def test_unset_env_is_strict(self, monkeypatch):
        monkeypatch.delenv("DJANGO_ENV", raising=False)
        with pytest.raises(ImproperlyConfigured):
            enforce_required_env(["GOOGLE_CLIENT_ID"], "OAuth audience check")
