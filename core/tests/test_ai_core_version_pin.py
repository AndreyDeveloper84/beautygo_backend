"""A9 — Boot-probe coverage for the ayla-ai-core version log.

Codex P0-5 root cause: Ayla and bot-platform pinned different
``ayla-ai-core`` versions, so prompt rendering, tool dispatch,
history truncation, and safety behaviour drifted per channel.

This PR ships the runtime observability piece — ``log_ai_core_version``
emits the resolved version at boot so operators can compare against
bot-platform's matching probe. The actual SHA alignment lands in a
follow-up PR after the v0.7.0 ``tenant_id`` migration is wired
through ``ai/concierge_factory.py`` (joint with W2).

Tests:

* ``resolve_ai_core_version`` returns the literal sentinel
  ``"missing"`` when the package is absent — callers branch on the
  string without catching the exception themselves.
* ``log_ai_core_version`` emits INFO at ``ayla.bootstrap`` when the
  package is installed, WARNING when missing (degraded but not
  boot-fatal).
"""
from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

from core.ai_core import log_ai_core_version, resolve_ai_core_version


class TestResolveAiCoreVersion:
    """Behaviour pin for the runtime probe."""

    def test_missing_package_returns_sentinel(self):
        with patch("core.ai_core.version", side_effect=PackageNotFoundError):
            assert resolve_ai_core_version() == "missing"

    def test_real_install_returns_a_truthy_string(self):
        # When the package is installed, the probe returns whatever
        # importlib.metadata reports. We just assert it's a non-empty
        # string — the actual version + SHA tracking is documented in
        # requirements.txt and ops notes.
        resolved = resolve_ai_core_version()
        assert isinstance(resolved, str)
        assert resolved  # non-empty


class TestLogAiCoreVersion:
    """Boot probe emits actionable output for ops."""

    def test_log_emits_info_when_resolved(self, caplog):
        caplog.set_level(logging.INFO, logger="ayla.bootstrap")
        with patch("core.ai_core.resolve_ai_core_version", return_value="0.8.1"):
            log_ai_core_version()
        messages = " ".join(rec.getMessage() for rec in caplog.records)
        assert "0.8.1" in messages
        assert "ayla-ai-core resolved version" in messages

    def test_log_emits_warning_when_missing(self, caplog):
        caplog.set_level(logging.WARNING, logger="ayla.bootstrap")
        with patch("core.ai_core.resolve_ai_core_version", return_value="missing"):
            log_ai_core_version()
        messages = " ".join(rec.getMessage() for rec in caplog.records)
        assert "ayla-ai-core not installed" in messages
        # Warning level — a missing package is degraded mode but not
        # boot-fatal. Higher severity would page ops on every CI run
        # that does not install the dep.
        assert any(
            rec.levelno == logging.WARNING and "ayla.bootstrap" == rec.name
            for rec in caplog.records
        )
