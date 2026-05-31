"""Probe + log the resolved ``ayla-ai-core`` version at startup.

Handoff Block A → A9, codex P0-5. The shared AI orchestration library
must run on the same version in Ayla backend AND bot-platform; the
two services historically drifted (Ayla on v0.6.0, bot on a v0.8.1
SHA, local checkout on 0.8.1). Logging the version on every boot
gives operators a way to confirm cross-service alignment without
having to inspect installed packages.

The probe is intentionally non-fatal: if ``ayla-ai-core`` is somehow
missing (CI run before the install completes, vendor extraction in
progress) we log an explicit warning instead of preventing boot —
that decision keeps the smoke check honest in degraded environments
without blocking dev iteration.
"""
from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version

logger = logging.getLogger(__name__)


# The expected SHA prefix lives in requirements.txt; we cannot read it
# at runtime without re-parsing the file. Instead we publish what was
# observed at boot so the operator can compare it against ops notes /
# bot-platform's matching probe.
def resolve_ai_core_version() -> str:
    """Return the installed ``ayla-ai-core`` package version, or
    ``"missing"`` if the package is not importable."""
    try:
        return version("ayla-ai-core")
    except PackageNotFoundError:
        return "missing"


def log_ai_core_version() -> None:
    """Emit the resolved version under the ``ayla.bootstrap`` logger.

    Called from ``users.apps.UsersConfig.ready`` so the line lands
    once per process boot, after Django has finished importing
    settings but before any request handler runs. The chosen logger
    name ``ayla.bootstrap`` keeps these messages searchable without
    interleaving with per-request access logs.
    """
    boot_logger = logging.getLogger("ayla.bootstrap")
    resolved = resolve_ai_core_version()
    if resolved == "missing":
        boot_logger.warning(
            "ayla-ai-core not installed — AI orchestration features will "
            "raise on first use. Expected when running tests with "
            "AI_CORE_AVAILABLE=0; otherwise check requirements install."
        )
        return
    boot_logger.info(
        "ayla-ai-core resolved version=%s — compare against bot-platform "
        "startup log to confirm cross-service alignment (handoff A9).",
        resolved,
    )
