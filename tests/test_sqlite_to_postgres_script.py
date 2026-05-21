"""Unit tests for scripts/migrate_sqlite_to_postgres.py helpers.

Real end-to-end migration test requires both a SQLite file with
synthetic data AND a running Postgres — that's a manual smoke run,
not unit-test territory. These tests exercise the pure helpers so a
future edit to the script's pre/post-flight logic can't silently
regress.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest


def _load_script_module():
    """Side-load scripts/migrate_sqlite_to_postgres.py without making
    scripts/ a Python package. Keeps the script directory free of
    __init__.py noise.
    """
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    script = repo_root / "scripts" / "migrate_sqlite_to_postgres.py"
    spec = importlib.util.spec_from_file_location("migrate_sqlite_to_postgres", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def script_mod():
    return _load_script_module()


class TestExcludedModels:
    def test_includes_django_managed_tables(self, script_mod):
        for name in (
            "contenttypes.contenttype",
            "auth.permission",
            "admin.logentry",
            "sessions.session",
        ):
            assert name in script_mod.EXCLUDED_MODELS, (
                f"{name} must stay in EXCLUDED_MODELS — re-loading these "
                "from SQLite causes PK collisions with Django's own data "
                "operations during `migrate`."
            )

    def test_excludes_token_blacklist(self, script_mod):
        """Outstanding/blacklisted JWTs are re-issued on the next user
        login. Migrating them across only revives invalidated session
        state and creates a confusing audit trail.
        """
        for name in (
            "token_blacklist.outstandingtoken",
            "token_blacklist.blacklistedtoken",
        ):
            assert name in script_mod.EXCLUDED_MODELS


class TestVerifyModelsList:
    def test_covers_user_facing_data(self, script_mod):
        """The verifier prints row-count parity for these models.
        Removing one means a silent diff slips past the post-run eyeball.
        """
        for dotted in (
            "users.User",
            "services.Service",
            "appointments.Appointment",
            "payments.Payment",
        ):
            assert dotted in script_mod.VERIFY_MODELS


class TestCheckSqlite:
    def test_aborts_on_missing_file(self, script_mod, tmp_path):
        missing = tmp_path / "no-such.sqlite3"
        with pytest.raises(SystemExit) as exc:
            script_mod._check_sqlite(missing)
        assert exc.value.code == 1

    def test_aborts_on_empty_file(self, script_mod, tmp_path):
        empty = tmp_path / "empty.sqlite3"
        empty.write_bytes(b"")
        with pytest.raises(SystemExit) as exc:
            script_mod._check_sqlite(empty)
        assert exc.value.code == 1

    def test_passes_on_non_empty_file(self, script_mod, tmp_path):
        path = tmp_path / "ok.sqlite3"
        path.write_bytes(b"SQLite format 3\x00" + b"x" * 100)
        # Must not raise.
        script_mod._check_sqlite(path)


class TestInjectSqliteAlias:
    def test_adds_alias_with_sqlite_engine(self, script_mod, settings, tmp_path):
        path = tmp_path / "fake.sqlite3"
        path.write_bytes(b"x")
        # Snapshot DATABASES to ensure the injection touches only the
        # new key, not the default.
        before_default = dict(settings.DATABASES["default"])
        script_mod._inject_sqlite_alias(path)
        assert "sqlite_legacy" in settings.DATABASES
        alias = settings.DATABASES["sqlite_legacy"]
        assert alias["ENGINE"] == "django.db.backends.sqlite3"
        assert alias["NAME"] == str(path)
        # Default connection must be unchanged.
        assert settings.DATABASES["default"] == before_default

    def test_alias_carries_django_required_keys(self, script_mod, settings, tmp_path):
        """Catches the next Django minor adding a new defaulted key.

        ``ConnectionHandler.configure_settings`` will quietly start
        defaulting anything we miss — and a missing ``TEST.MIGRATE`` is
        the kind of drift that surfaces as a confusing test-runner error
        when someone uses this alias for the first time off CI.
        """
        path = tmp_path / "fake.sqlite3"
        path.write_bytes(b"x")
        script_mod._inject_sqlite_alias(path)
        alias = settings.DATABASES["sqlite_legacy"]
        assert alias["ATOMIC_REQUESTS"] is False
        assert alias["AUTOCOMMIT"] is True
        assert alias["CONN_MAX_AGE"] == 0
        assert alias["CONN_HEALTH_CHECKS"] is False
        assert alias["TIME_ZONE"] is None
        # TEST sub-dict — every key Django 5.2 looks up.
        assert alias["TEST"]["NAME"] is None
        assert alias["TEST"]["MIRROR"] is None
        assert alias["TEST"]["MIGRATE"] is True
