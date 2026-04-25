"""Production env-strictness gate.

prod.py runs the same Django settings module on real production *and*
on staging / dev VPS environments that mirror the prod stack. Without
gating, any missing OAuth / webhook env raises ``ImproperlyConfigured``
at import — useful in real production (catches a misconfigured deploy
before traffic flows) but blocks a staging VPS from booting just
because it doesn't have real OAuth credentials yet.

``DJANGO_ENV`` separates the two:

- ``production`` (default) — strict; missing values raise.
- anything else (``staging``, ``dev``, …) — warn-and-boot; the same
  values are still expected eventually but their absence doesn't
  abort startup.

The default is ``production`` so a deploy that forgets the var is
treated as the strict case (fail-closed).
"""
from __future__ import annotations

import logging
import os

from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger("django.security")


def is_strict_production() -> bool:
    """Return ``True`` when the current process should treat missing
    security-critical env as fatal."""
    label = os.environ.get("DJANGO_ENV", "production").strip().lower()
    return label == "production"


def enforce_required_env(missing: list[str], purpose: str) -> None:
    """Either raise (strict prod) or warn (non-prod), based on the
    ``DJANGO_ENV`` label.

    ``missing`` is the list of env-var names that are unset/empty;
    ``purpose`` describes what defence is degraded so the warning
    has actionable context.
    """
    if not missing:
        return
    detail = f"missing {', '.join(missing)}: {purpose}"
    if is_strict_production():
        raise ImproperlyConfigured(
            f"Production requires these security-critical env vars: {detail}"
        )
    logger.warning(
        "DJANGO_ENV=%s — degraded security: %s. Set these before promoting "
        "this environment to production.",
        os.environ.get("DJANGO_ENV", "(unset)"),
        detail,
    )
