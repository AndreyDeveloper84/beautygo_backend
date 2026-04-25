"""HTTP deprecation signalling for legacy URL aliases.

When an endpoint is moved to a new path we keep the old one live for a
deprecation window, but every response from the old path must announce
itself with the IETF deprecation headers so mobile clients can react
before the cut-off:

- ``Deprecation: true`` — RFC draft "deprecated" indicator.
- ``Sunset: <HTTP-date>`` — RFC 8594 — the date after which the legacy
  path may be removed.

Mount the alias by subclassing the canonical view and mixing this in:

    class LegacyMasterMeView(DeprecatedAliasMixin, MasterMeView):
        sunset_date = "Sun, 31 May 2026 23:59:59 GMT"

Then route ``/old/path/`` to ``LegacyMasterMeView``. The canonical view
stays untouched.
"""
from __future__ import annotations

from rest_framework.views import APIView

# Default sunset date for endpoints renamed during the Notion API
# Specification v2.0 alignment (DRF-208 / DRF-209). Override on the
# subclass when shipping new aliases under a different deprecation
# window.
DEFAULT_SUNSET_DATE = "Sun, 31 May 2026 23:59:59 GMT"


class DeprecatedAliasMixin:
    """Adds ``Deprecation`` and ``Sunset`` headers to every response.

    Sits left of the canonical view in MRO so ``finalize_response``
    intercepts the canonical view's rendered response and tags it
    before it leaves the framework.
    """

    sunset_date: str = DEFAULT_SUNSET_DATE

    def finalize_response(self, request, response, *args, **kwargs):
        # ``super()`` walks past us to the real APIView chain so the
        # canonical view's serialization runs unchanged.
        response = super().finalize_response(  # type: ignore[misc]
            request, response, *args, **kwargs,
        )
        response["Deprecation"] = "true"
        response["Sunset"] = self.sunset_date
        return response


# Re-export so importers don't need to know about APIView resolution.
__all__ = ["DeprecatedAliasMixin", "DEFAULT_SUNSET_DATE", "APIView"]
