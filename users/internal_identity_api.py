"""GET /api/v1/internal/me/identity/ — DRF-1043 (backend half of DRF-1035).

Why this endpoint exists
------------------------

Every on-behalf-of-user service-to-service call from the bot names its
acting subject in ``X-External-User-ID`` (``bot:{channel}:{id}``).
``IsBotServiceWithVerifiedClient`` resolves that header into a concrete
Ayla ``User`` — lazily creating an ``is_proxy=True`` row on first sight —
and swaps it into ``request.user``. Until now there was no way for the
caller to ask **who Ayla resolved it to**: the canonical UUID only ever
leaked out as a side effect of some other call. That gap is the reason
``BotUser.ayla_user_id`` had no writer in production (DRF-1035) and why
booking create failed with ``ayla_client_id_missing`` for every subject
that had not been provisioned by hand.

This module closes the gap with one GET and nothing else.

Shape of the contract
---------------------

``GET`` with **no request body and no subject selector of any kind**.
The subject can only ever come from the authenticated context, so a
holder of the service token cannot ask "give me the Ayla id for an
arbitrary external identity I name" — only "which subject is *me*".
That is a deliberate narrowing versus a ``POST /resolve`` shape: it
removes the subject-substitution surface entirely rather than guarding
it. Query parameters are ignored for the same reason — there is no code
path here that reads one.

Mounted at ``me/identity/`` rather than the bare ``me/`` root:
``api/v1/internal/me/`` is a **namespace prefix** in the root urlconf
(``me/bookings/``, ``me/catalog/recommendations/`` live under it), not a
resource. ``me/<resource>/`` mirrors the sibling that already shipped.

Response minimality (deliberate)
--------------------------------

Exactly two fields. This is an identity endpoint, not a customer card.
It is reachable with the single shared ``AYLA_INTERNAL_API_TOKEN``, so
every field added here is a field that leaks with that one secret. No
phone, no email, no profile, no tenant, no consent state, no personal
context, no booking history — and the bar for adding anything later is
"DRF-1035 cannot work without it".

Known, accepted risk (DRF-1036, tracked separately)
---------------------------------------------------

This endpoint maps an enumerable external identity onto an internal
UUID, and four existing ``/api/v1/internal/users/{uuid}/…`` surfaces
authorise on that UUID under a plain Bearer. It is NOT the root cause —
the UUID was already obtainable, e.g. from a booking-create response —
but it lowers the cost of exploiting the defect if the service token
leaks. Out of scope here (different surface, different blast radius,
different test set); the mitigation applied *here* is the two-field
response above.
"""
from __future__ import annotations

import logging

from django.conf import settings
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from users.permissions import IsBotServiceWithVerifiedClient
from users.response import success_response
from users.services import is_valid_external_user_id


logger = logging.getLogger(__name__)


class InternalMeIdentitySerializer(serializers.Serializer):
    """Closed-shape response DTO — the wire-level guard on §2 minimality.

    Adding a field means editing this class, which means a reviewer sees
    it in the diff. A serializer drifting behind the model cannot widen
    the payload by accident.
    """

    ayla_user_id = serializers.UUIDField()
    is_proxy = serializers.BooleanField()


def _denial_reason(request: Request) -> str:
    """Classify a permission denial for the log — WITHOUT re-checking
    the token.

    ``IsBotServiceWithVerifiedClient`` returns a bare ``False`` for five
    distinct failures, so a 403 in production is otherwise unattributable.
    §7 asks specifically for "auth refused" and "resolve refused" to be
    distinguishable after the pilot.

    Two rules this function obeys:

    * It never compares the bearer against the expected secret. A
      non-constant-time comparison here would reintroduce exactly the
      timing oracle ``compare_digest`` exists to prevent, and the log
      does not need to know *which* wrong token was sent — only that a
      well-formed request was rejected. Hence ``auth_rejected`` as the
      catch-all.
    * It never calls ``resolve_external_user``. That function creates
      the proxy row as a side effect; calling it on the denial path
      would provision accounts for rejected callers. The format check
      is the side-effect-free predicate instead.

    Mirrors the permission class's own ordering so the reason names the
    first gate that actually failed.
    """
    if not (getattr(settings, "AYLA_INTERNAL_API_TOKEN", "") or ""):
        return "service_token_not_configured"
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if not auth_header.startswith("Bearer "):
        return "no_bearer"
    if not auth_header[len("Bearer "):].strip():
        return "empty_bearer"
    external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
    if not external_user_id:
        return "no_external_user_id"
    if not is_valid_external_user_id(external_user_id):
        return "invalid_external_user_id"
    return "auth_rejected"


class InternalMeIdentityView(APIView):
    """GET /api/v1/internal/me/identity/

    Only ``get`` is defined, so every other method is a 405 from DRF —
    there is no write surface and no way to name a subject.
    """

    # The bot bearer is not a JWT: leaving JWTAuthentication enabled
    # would 401 before the permission class ever runs. The permission is
    # the sole auth boundary — same pattern as the #97 records endpoints
    # and appointments._InternalAuthMixin.
    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(
        operation_id="internal_me_identity",
        tags=["internal"],
        request=None,
        responses={
            200: inline_serializer(
                name="InternalMeIdentityResponse",
                fields={"data": InternalMeIdentitySerializer()},
            ),
            403: OpenApiResponse(
                description=(
                    "Bearer missing/invalid, or X-External-User-ID missing/"
                    "malformed"
                ),
            ),
        },
    )
    def get(self, request: Request) -> Response:
        # request.user is already the canonical subject: the permission
        # class ran resolve_external_user and replaced AnonymousUser with
        # the result. There is nothing left for this view to resolve —
        # it only reports what the auth boundary decided.
        user = request.user

        logger.info(
            "internal.me.identity.resolved user_id=%s is_proxy=%s "
            "external_user_id=%s",
            user.id,
            user.is_proxy,
            # Logged per the existing s2s policy (nutrition/views.py does
            # the same): the external id is an opaque channel handle, not
            # PII — no phone, email or name is emitted here or anywhere
            # on this path.
            request.META.get("HTTP_X_EXTERNAL_USER_ID", ""),
        )

        # is_proxy=False means the external identity was bound to a REAL
        # account via bind_external_identity and the resolver followed
        # the pointer (users/services.py Phase C). The bot carries the
        # flag for observability; it does not branch on it today.
        return success_response({
            "ayla_user_id": str(user.id),
            "is_proxy": bool(user.is_proxy),
        })

    def permission_denied(self, request, message=None, code=None):
        """Log the classified denial, then defer to DRF's 403."""
        logger.warning(
            "internal.me.identity.denied reason=%s",
            _denial_reason(request),
        )
        super().permission_denied(request, message=message, code=code)
