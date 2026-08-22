"""DRF-1244 — prod.py checks the *form* of URL-shaped env, not just presence.

``_REQUIRED_PROD_ENV`` proves a variable was set. It cannot tell
``AYLA_PUBLIC_BASE_URL=https://dev.gobeauty.site`` apart from
``AYLA_PUBLIC_BASE_URL=да`` — both are "set". The second one boots
happily and fails at the first call that builds a URL out of it: in
production, on a real booking, hours after the deploy went green. The
same class was filed against the bot side as DRF-1221 (``AYLA_BASE_URL``).

These tests pin three things:

1. a malformed URL aborts the boot under ``DJANGO_ENV=production``, with
   the variable name and the reason in the message;
2. the pilot profile (``DJANGO_ENV=staging``) warns and boots — this gate
   stands in front of a running contour and must not be able to stop it;
3. the check does not exist outside the prod settings profile — dev.py
   and test.py import with a deliberately broken value and don't care.

Reload pattern mirrors ``test_prod_required_env.py``: prod.py runs its
gates at import time, so env has to be injected before the import.
"""
from __future__ import annotations

import importlib
import sys
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured


def _reload_settings(module_path, monkeypatch, **env_overrides):
    """Reload a settings module with env overrides applied."""
    for key, value in env_overrides.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    for name in (
        "djangoProject.settings.prod",
        "djangoProject.settings.test",
        "djangoProject.settings.dev",
        "djangoProject.settings.base",
    ):
        sys.modules.pop(name, None)
    return importlib.import_module(module_path)


# Every var the presence gate already requires, so a failure in these
# tests can only come from the URL check under test.
_FULL_ENV = dict(
    DJANGO_SECRET_KEY="test-secret",  # pragma: allowlist secret
    GOOGLE_CLIENT_ID="google-id",
    APPLE_CLIENT_ID="apple-id",
    YOOKASSA_WEBHOOK_ALLOWED_IPS="185.71.76.0/27",
    AYLA_INTERNAL_API_TOKEN="bearer-token",
)

# The shapes the pilot / prod stack actually runs with: public TLS host,
# internal cluster host, docker-compose service name with a port.
_GOOD_URLS = dict(
    AYLA_INTERNAL_BASE_URL="http://web:8000",
    AYLA_PUBLIC_BASE_URL="https://dev.gobeauty.site",
    BOT_PLATFORM_BASE_URL="https://api-dev.gobeauty.site",
    YCLIENTS_API_BASE_URL="https://api.yclients.com/api/v1",
    MINIO_ENDPOINT="http://minio:9000",
    OPENAI_BASE_URL="https://gateway.example/openai/v1",
)

# One entry per failure mode a human actually produces in a .env file.
_BAD_VALUES = [
    ("a human's yes", "да"),
    ("a placeholder that shipped", "TODO"),
    ("bare host, no scheme", "dev.gobeauty.site"),
    ("truncated paste", "https://"),
    ("scheme and path but no host", "http:///api/v1"),
    ("stray trailing space", "https://dev.gobeauty.site "),
    ("space in the middle", "https://dev.gobeauty.site /api"),
    ("wrong protocol", "ftp://dev.gobeauty.site"),
    ("port that is not a number", "http://dev.gobeauty.site:notaport"),
]


class TestProdUrlEnvMembership:
    """Schema-pin: which vars are URL-shaped, and which must NOT be."""

    def test_url_shaped_tuple_contents(self, monkeypatch):
        module = _reload_settings(
            "djangoProject.settings.prod", monkeypatch, **_FULL_ENV, **_GOOD_URLS,
        )
        assert set(module._URL_SHAPED_ENV) == {
            "AYLA_INTERNAL_BASE_URL",
            "AYLA_PUBLIC_BASE_URL",
            "BOT_PLATFORM_BASE_URL",
            "YCLIENTS_API_BASE_URL",
            "MINIO_ENDPOINT",
            "OPENAI_BASE_URL",
        }

    def test_non_url_secrets_are_not_url_checked(self, monkeypatch):
        """The opposite defect: an IP list and a bearer token are not
        URLs, and validating them as URLs would break a correct deploy."""
        module = _reload_settings(
            "djangoProject.settings.prod", monkeypatch, **_FULL_ENV, **_GOOD_URLS,
        )
        for name in (
            "YOOKASSA_WEBHOOK_ALLOWED_IPS",
            "AYLA_INTERNAL_API_TOKEN",
            "GOOGLE_CLIENT_ID",
            "APPLE_CLIENT_ID",
            "REDIS_URL",       # redis:// — right shape, wrong scheme set
            "OPENAI_PROXY",    # legitimately socks5://
            "SENTRY_DSN",
        ):
            assert name not in module._URL_SHAPED_ENV


class TestProdBootRejectsMalformedUrls:
    @pytest.mark.parametrize("name", sorted(_GOOD_URLS))
    @pytest.mark.parametrize("label,bad_value", _BAD_VALUES, ids=[
        label for label, _ in _BAD_VALUES
    ])
    def test_strict_prod_boot_raises(self, monkeypatch, name, label, bad_value):
        """Every URL-shaped var × every malformed shape aborts the boot."""
        monkeypatch.setenv("DJANGO_ENV", "production")
        env = {**_FULL_ENV, **_GOOD_URLS, name: bad_value}
        with pytest.raises(ImproperlyConfigured) as exc_info:
            _reload_settings("djangoProject.settings.prod", monkeypatch, **env)
        message = str(exc_info.value)
        # The operator must learn WHICH variable — "Invalid configuration"
        # is useless at 04:00.
        assert name in message

    def test_message_names_the_variable_and_the_reason(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "production")
        with pytest.raises(ImproperlyConfigured) as exc_info:
            _reload_settings(
                "djangoProject.settings.prod",
                monkeypatch,
                **{**_FULL_ENV, **_GOOD_URLS, "AYLA_PUBLIC_BASE_URL": "https://"},
            )
        message = str(exc_info.value)
        assert "AYLA_PUBLIC_BASE_URL" in message
        assert "no host" in message
        assert "https://host" in message  # the expected shape is spelled out

    def test_message_does_not_echo_the_value(self, monkeypatch):
        """These lines live next to credentials in .env and the traceback
        reaches logs and Sentry."""
        monkeypatch.setenv("DJANGO_ENV", "production")
        with pytest.raises(ImproperlyConfigured) as exc_info:
            _reload_settings(
                "djangoProject.settings.prod",
                monkeypatch,
                **{
                    **_FULL_ENV,
                    **_GOOD_URLS,
                    "AYLA_INTERNAL_BASE_URL": "https://t0ken@internal.example ",
                },
            )
        message = str(exc_info.value)
        assert "AYLA_INTERNAL_BASE_URL" in message
        assert "t0ken" not in message
        assert "internal.example" not in message

    def test_all_good_urls_boot(self, monkeypatch):
        """The values this stack actually runs on must survive the gate —
        a check that cannot pass is a check that takes prod down."""
        monkeypatch.setenv("DJANGO_ENV", "production")
        module = _reload_settings(
            "djangoProject.settings.prod", monkeypatch, **_FULL_ENV, **_GOOD_URLS,
        )
        assert module.AYLA_PUBLIC_BASE_URL == "https://dev.gobeauty.site"

    def test_unset_url_vars_still_boot(self, monkeypatch):
        """Presence stays the business of _REQUIRED_PROD_ENV. None of the
        URL vars are in it, so an unset one must not newly break a boot
        that works today."""
        monkeypatch.setenv("DJANGO_ENV", "production")
        env = {**_FULL_ENV, **{name: None for name in _GOOD_URLS}}
        module = _reload_settings("djangoProject.settings.prod", monkeypatch, **env)
        assert module.AYLA_PUBLIC_BASE_URL == ""

    @pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "spaces-only"])
    def test_explicitly_blank_url_vars_still_boot(self, monkeypatch, blank):
        """`BOT_PLATFORM_BASE_URL=` in .env means "publisher off" — a
        supported configuration, not a malformed URL. Same for a line
        that only carries stray spaces: every consumer reads these as
        `os.environ.get(NAME, "")` and branches on `if not value`, so
        blank is the documented off-switch, not a defect. Making it
        fatal would take down a deploy that works today.

        The *function* still rejects an empty string as an unusable URL
        (see core/tests/test_env_strictness.py); it is only the env gate
        that maps blank onto "unset".
        """
        monkeypatch.setenv("DJANGO_ENV", "production")
        env = {**_FULL_ENV, **{name: blank for name in _GOOD_URLS}}
        module = _reload_settings("djangoProject.settings.prod", monkeypatch, **env)
        assert module.BOT_PLATFORM_BASE_URL == blank


class TestPilotProfileIsNotBrokenByTheGate:
    def test_staging_warns_and_boots(self, monkeypatch):
        """The pilot deploys with DJANGO_ENV=staging
        (.github/workflows/ci.yml). A malformed URL there is loud, never
        fatal — this gate must not be able to stop a running contour."""
        monkeypatch.setenv("DJANGO_ENV", "staging")
        with patch("core.env_strictness.logger") as mock_logger:
            module = _reload_settings(
                "djangoProject.settings.prod",
                monkeypatch,
                **{**_FULL_ENV, **_GOOD_URLS, "AYLA_PUBLIC_BASE_URL": "да"},
            )
        assert hasattr(module, "_URL_SHAPED_ENV")  # boot reached the end
        assert mock_logger.warning.called
        args, _ = mock_logger.warning.call_args
        rendered = args[0] % args[1:]
        assert "AYLA_PUBLIC_BASE_URL" in rendered


class TestCheckDoesNotFireOutsideProd:
    """The gate lives in prod.py only. Local development and CI run
    dev.py / test.py and must be untouched by it — otherwise the first
    developer with a half-written .env can't run anything."""

    @pytest.mark.parametrize("module_path", [
        "djangoProject.settings.dev",
        "djangoProject.settings.test",
    ])
    def test_non_prod_profiles_ignore_malformed_urls(self, monkeypatch, module_path):
        # Strictest possible env label — the profile, not the label, is
        # what keeps dev/test out of the gate.
        monkeypatch.setenv("DJANGO_ENV", "production")
        module = _reload_settings(
            module_path,
            monkeypatch,
            DJANGO_SECRET_KEY="test-secret",  # pragma: allowlist secret
            AYLA_PUBLIC_BASE_URL="да",
            AYLA_INTERNAL_BASE_URL="https://",
            BOT_PLATFORM_BASE_URL="TODO",
            MINIO_ENDPOINT="not a url",
        )
        # Imported without raising, and the malformed value is simply
        # carried through as-is.
        assert module.AYLA_PUBLIC_BASE_URL == "да"
        assert not hasattr(module, "_URL_SHAPED_ENV")

    def test_non_prod_profiles_do_not_even_warn(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "production")
        with patch("core.env_strictness.logger") as mock_logger:
            _reload_settings(
                "djangoProject.settings.test",
                monkeypatch,
                DJANGO_SECRET_KEY="test-secret",  # pragma: allowlist secret
                AYLA_PUBLIC_BASE_URL="да",
            )
        assert not mock_logger.warning.called
