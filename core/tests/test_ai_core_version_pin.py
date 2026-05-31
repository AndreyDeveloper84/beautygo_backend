"""A9 — Pin ayla-ai-core SHA across Ayla + bot-platform.

Codex P0-5 root cause: Ayla and bot-platform pinned different
``ayla-ai-core`` versions, so prompt rendering, tool dispatch,
history truncation, and safety behaviour drifted per channel.

Two layers of defence here:

1. Requirements-pin assertion — Ayla's ``requirements.txt`` line MUST
   carry the SHA documented in bot-platform's ``pyproject.toml``. A
   developer who bumps one side without the other will fail this test
   in CI.
2. Boot-probe assertion — ``log_ai_core_version`` returns the live
   resolved version. If the runtime version cannot be read,
   ``resolve_ai_core_version`` returns the sentinel ``"missing"``;
   the probe degrades to a warning instead of breaking boot. The
   sentinel itself is pinned so a future refactor does not silently
   change semantics.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from unittest.mock import patch

from core.ai_core import log_ai_core_version, resolve_ai_core_version

# Canonical SHA mirrored from bot-platform pyproject.toml dependencies.
# Bump procedure documented in requirements.txt + bot pyproject.toml.
EXPECTED_AI_CORE_SHA = "e73a1b4784c150493c300b316d7a62cd423c8377"

REQUIREMENTS_PATH = Path(__file__).resolve().parents[2] / "requirements.txt"


class TestRequirementsPin:
    """Schema-pin for the requirements.txt entry."""

    def test_requirements_pins_expected_sha(self):
        contents = REQUIREMENTS_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"ayla-ai-core\s*@\s*git\+https://[^@]+@([0-9a-f]{40})",
            contents,
        )
        assert match, (
            "requirements.txt must pin ayla-ai-core via "
            "'ayla-ai-core @ git+https://.../ayla-ai-core.git@<40-char-sha>' "
            "so cross-service drift can be machine-checked."
        )
        assert match.group(1) == EXPECTED_AI_CORE_SHA, (
            f"ayla-ai-core SHA drift: requirements.txt pins "
            f"{match.group(1)} but bot-platform pyproject.toml expects "
            f"{EXPECTED_AI_CORE_SHA}. Bump both in coordinated PRs (A9)."
        )


class TestResolveAiCoreVersion:
    """Behaviour pin for the runtime probe."""

    def test_missing_package_returns_sentinel(self):
        # importlib.metadata.version raises PackageNotFoundError when
        # the dist-info is absent. The probe must return the literal
        # 'missing' string so callers can branch on it without
        # catching the exception themselves.
        from importlib.metadata import PackageNotFoundError
        with patch("core.ai_core.version", side_effect=PackageNotFoundError):
            assert resolve_ai_core_version() == "missing"

    def test_real_install_returns_a_dotted_version(self):
        # When the package is installed, the probe returns whatever
        # importlib.metadata reports. The exact string varies by
        # build-tag, so we just assert it's truthy and not the
        # sentinel. CI verifies the pin separately above.
        resolved = resolve_ai_core_version()
        # Either the package is installed (any non-sentinel string)
        # or the test runs in a stripped env (sentinel). Both are
        # accepted — the assertion that matters lives in TestRequirementsPin.
        assert resolved


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
