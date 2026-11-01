"""Security hardening gates in prod.py (fix/security-hardening).

prod.py serves BOTH real production and the staging / pilot VPS
(``DJANGO_ENV=staging``, .github/workflows/ci.yml). ``enforce_required_env``
downgrades a missing variable to a warning on staging so a pilot can boot
before every credential exists. The gates pinned here are the ones for
which "boot anyway" is not a degraded state but a broken one, so they
are fatal on staging as well:

1. ``GOOGLE_CLIENT_ID`` / ``APPLE_CLIENT_ID`` — users/social_auth.py now
   refuses every provider token when the id is empty (the ``aud`` check is
   no longer skippable), so an empty id is "social login down".
2. ``DJANGO_SECRET_KEY`` — base.py no longer ships a fallback; dev.py has
   a development-only one; prod.py rejects missing / blank /
   ``django-insecure-`` values regardless of DJANGO_ENV.
3. ``OTP_FIXED_DEV_CODE_ENABLED`` — the constant "0000" login code is a
   development opt-in; prod.py refuses to boot with it on and forces it
   off.
4. ``MINIO_ROOT_USER`` / ``MINIO_ROOT_PASSWORD`` — no vendor-default
   fallback in prod.py or docker-compose.yml; required on production and
   staging.

Reload pattern mirrors ``test_prod_required_env.py``: prod.py runs its
gates at import time, so env has to be injected before the import.
"""
from __future__ import annotations

import importlib
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured

_SETTINGS_MODULES = (
    "djangoProject.settings.prod",
    "djangoProject.settings.test",
    "djangoProject.settings.dev",
    "djangoProject.settings.base",
)


def _reload_settings(module_path, monkeypatch, **env_overrides):
    for key, value in env_overrides.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    # base.py calls load_dotenv(BASE_DIR / '.env'); a developer's local
    # .env would otherwise re-supply the very variable a test deletes.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)

    for name in _SETTINGS_MODULES:
        sys.modules.pop(name, None)
    return importlib.import_module(module_path)


def _reload_prod(monkeypatch, **env_overrides):
    return _reload_settings("djangoProject.settings.prod", monkeypatch, **env_overrides)


# Every variable any prod.py gate requires, so a failure in a test below
# can only come from the one variable the test removes.
_FULL_ENV = dict(
    DJANGO_SECRET_KEY="a-strong-random-test-key",  # pragma: allowlist secret
    GOOGLE_CLIENT_ID="google-id",
    APPLE_CLIENT_ID="apple-id",
    YOOKASSA_WEBHOOK_ALLOWED_IPS="185.71.76.0/27",
    AYLA_INTERNAL_API_TOKEN="bearer-token",  # pragma: allowlist secret
    MINIO_ROOT_USER="minio-access-key",
    MINIO_ROOT_PASSWORD="minio-secret-key",  # pragma: allowlist secret
)


class TestOAuthClientIdsAreRequiredOnStagingToo:
    """The pilot runs prod.py with DJANGO_ENV=staging. Without a client id
    social_auth fails closed on every token — a boot that succeeds there
    hides an outage, so the boot must not succeed."""

    @pytest.mark.parametrize("label", ["production", "staging"])
    @pytest.mark.parametrize("name", ["GOOGLE_CLIENT_ID", "APPLE_CLIENT_ID"])
    def test_missing_client_id_aborts_the_boot(self, monkeypatch, label, name):
        monkeypatch.setenv("DJANGO_ENV", label)
        with pytest.raises(ImproperlyConfigured) as exc_info:
            _reload_prod(monkeypatch, **{**_FULL_ENV, name: None})
        assert name in str(exc_info.value)

    @pytest.mark.parametrize("name", ["GOOGLE_CLIENT_ID", "APPLE_CLIENT_ID"])
    def test_empty_string_counts_as_missing(self, monkeypatch, name):
        monkeypatch.setenv("DJANGO_ENV", "staging")
        with pytest.raises(ImproperlyConfigured) as exc_info:
            _reload_prod(monkeypatch, **{**_FULL_ENV, name: ""})
        assert name in str(exc_info.value)

    def test_membership_is_pinned(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "staging")
        module = _reload_prod(monkeypatch, **_FULL_ENV)
        assert "GOOGLE_CLIENT_ID" in module._REQUIRED_PRODUCTION_LIKE_ENV
        assert "APPLE_CLIENT_ID" in module._REQUIRED_PRODUCTION_LIKE_ENV

    def test_other_labels_still_warn_and_boot(self, monkeypatch):
        """A local docker run of prod.py (DJANGO_ENV=dev) keeps the old
        warn-and-boot behaviour — the hard gate is for environments that
        face real people."""
        monkeypatch.setenv("DJANGO_ENV", "dev")
        module = _reload_prod(monkeypatch, **{**_FULL_ENV, "GOOGLE_CLIENT_ID": None})
        assert module.GOOGLE_CLIENT_ID is None

    def test_fully_configured_staging_boots(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "staging")
        module = _reload_prod(monkeypatch, **_FULL_ENV)
        assert module.GOOGLE_CLIENT_ID == "google-id"
        assert module.APPLE_CLIENT_ID == "apple-id"


class TestSecretKeyHasNoSharedFallback:
    """The old base.py default was a literal in a public repository, used
    to sign sessions and JWTs by any deploy that forgot the env var."""

    def test_base_has_no_fallback(self, monkeypatch):
        module = _reload_settings(
            "djangoProject.settings.base", monkeypatch, DJANGO_SECRET_KEY=None,
        )
        assert module.SECRET_KEY == ""

    def test_dev_supplies_a_development_only_fallback(self, monkeypatch):
        module = _reload_settings(
            "djangoProject.settings.dev", monkeypatch, DJANGO_SECRET_KEY=None,
        )
        assert module.SECRET_KEY.startswith("django-insecure-")

    def test_dev_prefers_the_env_value(self, monkeypatch):
        module = _reload_settings(
            "djangoProject.settings.dev", monkeypatch,
            DJANGO_SECRET_KEY="from-the-environment",  # pragma: allowlist secret
        )
        assert module.SECRET_KEY == "from-the-environment"  # pragma: allowlist secret

    def test_test_profile_still_imports_without_the_env(self, monkeypatch):
        """CI sets DJANGO_SECRET_KEY, a laptop may not — pytest must run."""
        module = _reload_settings(
            "djangoProject.settings.test", monkeypatch, DJANGO_SECRET_KEY=None,
        )
        assert module.SECRET_KEY

    @pytest.mark.parametrize("label", ["production", "staging", "dev"])
    @pytest.mark.parametrize("bad", [
        pytest.param(None, id="missing"),
        pytest.param("", id="blank"),
        pytest.param("   ", id="spaces"),
        pytest.param("django-insecure-dev-only-fallback", id="dev-fallback"),
        pytest.param("django-insecure-abc123", id="startproject-shape"),
    ])
    def test_prod_rejects_missing_blank_or_insecure_key(self, monkeypatch, label, bad):
        monkeypatch.setenv("DJANGO_ENV", label)
        with pytest.raises(ImproperlyConfigured) as exc_info:
            _reload_prod(monkeypatch, **{**_FULL_ENV, "DJANGO_SECRET_KEY": bad})
        message = str(exc_info.value)
        assert "DJANGO_SECRET_KEY" in message
        if bad:
            assert bad not in message  # never echo the value

    def test_prod_accepts_a_real_key(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "production")
        module = _reload_prod(monkeypatch, **_FULL_ENV)
        assert module.SECRET_KEY == _FULL_ENV["DJANGO_SECRET_KEY"]


class TestFixedOtpCodeIsRefusedByProdSettings:
    """Real phone numbers live behind this profile (production and the
    staging pilot alike) — a constant login code there is an open door."""

    @pytest.mark.parametrize("label", ["production", "staging", "dev"])
    def test_an_enabled_flag_aborts_the_boot(self, monkeypatch, label):
        monkeypatch.setenv("DJANGO_ENV", label)
        with pytest.raises(ImproperlyConfigured) as exc_info:
            _reload_prod(monkeypatch, **_FULL_ENV, OTP_FIXED_DEV_CODE_ENABLED="true")
        assert "OTP_FIXED_DEV_CODE_ENABLED" in str(exc_info.value)

    def test_unset_flag_boots_and_is_forced_off(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "staging")
        module = _reload_prod(monkeypatch, **_FULL_ENV, OTP_FIXED_DEV_CODE_ENABLED=None)
        assert module.OTP_FIXED_DEV_CODE_ENABLED is False

    def test_explicit_false_boots(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "production")
        module = _reload_prod(monkeypatch, **_FULL_ENV, OTP_FIXED_DEV_CODE_ENABLED="false")
        assert module.OTP_FIXED_DEV_CODE_ENABLED is False


class TestObjectStorageCredentialsHaveNoVendorDefault:
    """prod.py (and docker-compose.yml) defaulted MinIO to the vendor's
    documented credentials; a host that forgot the env ran on them."""

    @pytest.mark.parametrize("label", ["production", "staging"])
    @pytest.mark.parametrize("name", ["MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"])
    def test_missing_credential_aborts_the_boot(self, monkeypatch, label, name):
        monkeypatch.setenv("DJANGO_ENV", label)
        with pytest.raises(ImproperlyConfigured) as exc_info:
            _reload_prod(monkeypatch, **{**_FULL_ENV, name: None})
        assert name in str(exc_info.value)

    def test_membership_is_pinned(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "staging")
        module = _reload_prod(monkeypatch, **_FULL_ENV)
        assert "MINIO_ROOT_USER" in module._REQUIRED_PRODUCTION_LIKE_ENV
        assert "MINIO_ROOT_PASSWORD" in module._REQUIRED_PRODUCTION_LIKE_ENV

    def test_storage_uses_the_env_values_with_no_fallback(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "production")
        module = _reload_prod(monkeypatch, **_FULL_ENV)
        options = module.STORAGES["default"]["OPTIONS"]
        assert options["access_key"] == "minio-access-key"
        assert options["secret_key"] == "minio-secret-key"  # pragma: allowlist secret
        assert "minioadmin" not in (options["access_key"], options["secret_key"])


class TestComposeHasNoDefaultCredentials:
    """docker-compose.yml must fail loudly (`${VAR:?...}`) instead of
    falling back to well-known passwords when .env is incomplete."""

    def _compose_text(self):
        from pathlib import Path

        return (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(
            encoding="utf-8",
        )

    @pytest.mark.parametrize("name", [
        "POSTGRES_PASSWORD", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD",
    ])
    def test_credential_is_required_not_defaulted(self, name):
        text = self._compose_text()
        assert f"${{{name}:-" not in text, f"{name} still has a compose default"
        assert f"${{{name}:?" in text, f"{name} is not marked required"

    def test_vendor_default_is_gone(self):
        assert "minioadmin" not in self._compose_text()
