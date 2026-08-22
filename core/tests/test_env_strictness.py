"""Tests for core.env_strictness — DJANGO_ENV gate on prod.py fail-fast."""
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured

from core.env_strictness import (
    describe_url_defect,
    enforce_required_env,
    enforce_url_env,
    is_strict_production,
    is_structurally_valid_http_url,
)


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


class TestIsStructurallyValidHttpUrl:
    """DRF-1244 — presence is not fitness.

    The positive cases are the shapes this deployment actually uses:
    a public TLS host, an internal cluster host with a port and no dot
    in the hostname (``http://minio:9000``), a loopback address, an IPv6
    literal, and a base URL that carries a path.
    """

    @pytest.mark.parametrize("value", [
        "https://dev.gobeauty.site",
        "https://api-dev.gobeauty.site/",
        "http://minio:9000",            # docker-compose service name, no dot
        "http://ayla-api.internal:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
        "https://api.yclients.com/api/v1",   # base URL with a path
        "https://gateway.example/openai/v1",
    ])
    def test_usable_urls_pass(self, value):
        assert is_structurally_valid_http_url(value) is True
        assert describe_url_defect(value) is None

    @pytest.mark.parametrize("value,expected_fragment", [
        ("", "empty"),                          # empty string
        ("   ", "empty"),                       # whitespace only
        ("да", "no scheme"),                    # a human's "yes"
        ("TODO", "no scheme"),                  # a placeholder that shipped
        ("dev.gobeauty.site", "no scheme"),     # bare host
        ("//dev.gobeauty.site", "no scheme"),   # protocol-relative
        ("/api/v1", "no scheme"),               # a path, not a URL
        ("https://", "no host"),                # truncated paste
        ("http:///api/v1", "no host"),          # scheme, path, no host
        ("https://:8000", "empty host"),        # port only
        ("https://user@", "empty host"),        # credentials only
        ("https://dev.gobeauty.site /api", "whitespace"),   # space inside
        (" https://dev.gobeauty.site", "whitespace"),       # leading space
        ("https://dev.gobeauty.site ", "whitespace"),       # trailing space
        ("https://dev.gobeauty.site\n", "whitespace"),      # stray newline
        ("ftp://dev.gobeauty.site", "scheme"),  # wrong scheme
        ("redis://localhost:6379", "scheme"),   # right shape, wrong protocol
        ("http://host:notaport", "port"),       # port that is not a number
        ("http://[::1", "not parseable"),       # unclosed IPv6 bracket
    ])
    def test_unusable_urls_fail_with_a_named_reason(self, value, expected_fragment):
        assert is_structurally_valid_http_url(value) is False
        defect = describe_url_defect(value)
        assert defect is not None
        assert expected_fragment in defect, (
            f"defect message for {value!r} must say what is wrong "
            f"({expected_fragment!r}), got: {defect!r}"
        )

    def test_none_is_reported_as_unset(self):
        assert is_structurally_valid_http_url(None) is False
        assert describe_url_defect(None) == "is not set"

    def test_defect_message_never_echoes_the_value(self):
        """These vars sit next to credentials in .env and the message
        reaches logs / Sentry. Only the shape may be described."""
        secretish = "https://s3cr3t-token@internal-host.example/path"  # pragma: allowlist secret  # noqa: E501
        defect = describe_url_defect(secretish + " ")  # whitespace defect
        assert defect is not None
        assert "s3cr3t-token" not in defect
        assert "internal-host.example" not in defect


class TestEnforceUrlEnv:
    _NAME = "AYLA_PUBLIC_BASE_URL"

    def test_valid_value_passes_in_strict_prod(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "production")
        monkeypatch.setenv(self._NAME, "https://dev.gobeauty.site")
        enforce_url_env((self._NAME,), "public base")  # must not raise

    def test_strict_prod_raises_and_names_the_variable(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "production")
        monkeypatch.setenv(self._NAME, "да")
        with pytest.raises(ImproperlyConfigured) as exc_info:
            enforce_url_env((self._NAME,), "public base for return_url")
        message = str(exc_info.value)
        # A person woken at 04:00 must learn WHICH var and WHAT is wrong.
        assert self._NAME in message
        assert "no scheme" in message
        assert "public base for return_url" in message

    def test_unset_variable_is_not_a_defect(self, monkeypatch):
        # Presence policy belongs to _REQUIRED_PROD_ENV, not here.
        monkeypatch.setenv("DJANGO_ENV", "production")
        monkeypatch.delenv(self._NAME, raising=False)
        enforce_url_env((self._NAME,), "public base")  # must not raise

    def test_explicitly_blank_variable_is_not_a_defect(self, monkeypatch):
        # `NAME=` in .env is the documented off-switch for these
        # settings (publisher no-ops, OpenAI falls back to its default).
        # Turning it into a failed boot would break a supported config.
        monkeypatch.setenv("DJANGO_ENV", "production")
        monkeypatch.setenv(self._NAME, "")
        enforce_url_env((self._NAME,), "public base")  # must not raise

    def test_every_bad_value_is_reported(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "production")
        monkeypatch.setenv("AYLA_PUBLIC_BASE_URL", "https://")
        monkeypatch.setenv("BOT_PLATFORM_BASE_URL", "bot.example")
        with pytest.raises(ImproperlyConfigured) as exc_info:
            enforce_url_env(
                ("AYLA_PUBLIC_BASE_URL", "BOT_PLATFORM_BASE_URL"),
                "purpose",
            )
        message = str(exc_info.value)
        assert "AYLA_PUBLIC_BASE_URL" in message
        assert "BOT_PLATFORM_BASE_URL" in message

    def test_staging_warns_instead_of_raising(self, monkeypatch):
        """The pilot deploys with DJANGO_ENV=staging. A malformed URL
        there must be loud, not fatal — this gate stands in front of the
        boot path of a running contour."""
        monkeypatch.setenv("DJANGO_ENV", "staging")
        monkeypatch.setenv(self._NAME, "https://")
        with patch("core.env_strictness.logger") as mock_logger:
            enforce_url_env((self._NAME,), "public base")  # must not raise
        assert mock_logger.warning.called
        args, _ = mock_logger.warning.call_args
        rendered = args[0] % args[1:]
        assert self._NAME in rendered
        assert "DJANGO_ENV=staging" in rendered

    def test_unset_django_env_is_strict(self, monkeypatch):
        # Fail-closed: a deploy that forgets DJANGO_ENV is treated as prod.
        monkeypatch.delenv("DJANGO_ENV", raising=False)
        monkeypatch.setenv(self._NAME, "TODO")
        with pytest.raises(ImproperlyConfigured):
            enforce_url_env((self._NAME,), "public base")
