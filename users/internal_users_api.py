"""Internal user-profile endpoint — codex audit P1-3.

bot-platform's ``apps/eventbus/consumers/identity.py`` handles the
``user.profile.updated`` cross-service event by issuing a fetch back
to Ayla. Before this module, the bot client targeted
``GET /api/v1/users/{user_id}`` but no such route existed — every
fetch 404'd and the consumer dead-lettered.

This module adds the missing route at
``GET /api/v1/internal/users/{user_id}/`` (Ayla's ``/internal/``
convention for service-to-service calls — symmetric with
``/api/v1/internal/me/...`` and ``/api/v1/payments/internal/...``).

Hard rules
----------

* **PII §7 closed subset**: response carries exactly
  ``display_name`` + ``avatar_url``. No phone / email / birthday /
  language / role / tenant / city / coordinates. The serializer is
  a closed-shape DTO; adding a field requires an explicit code
  change here, not a backend serializer drift.
* **Bearer-only auth (IsInternalBearer)**: the call is service-to-
  service. ``request.user`` stays Anonymous. No JWT path. A leaked
  bearer can enumerate every user — but is bounded to the two safe
  fields, so the blast radius is minimised by the response shape.
* **No write surface**: GET only. The bot mirrors fields locally; if
  it ever needs to change Ayla's profile, the existing JWT-auth
  ``PATCH /users/me/`` is the path (the user is involved).

This endpoint is NOT pilot-critical — ``user.profile.updated`` is
in the "future slice" of cross-service events. The endpoint
unblocks the bot consumer that's already shipped (#446) but the
emit-site on Ayla still pending. Filler-priority work.
"""
from __future__ import annotations

import logging
from uuid import UUID

from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import Profile, SpecialistProfile, User
from users.permissions import IsInternalBearer
from users.response import error_response

logger = logging.getLogger(__name__)


class _InternalUserProfileSerializer(serializers.Serializer):
    """Closed-shape response — PII §7 enforcement at the wire.

    Two fields, both strings, both empty-string when the underlying
    record is null (mirroring bot's ``ProfileFetchError`` "missing
    keys" guard: empty string = "Ayla cleared the field", absent key
    = malformed).
    """

    display_name = serializers.CharField(allow_blank=True)
    avatar_url = serializers.CharField(allow_blank=True)


def _resolve_profile(user: User) -> tuple[str, str]:
    """Pick the display_name + avatar_url from the right profile row.

    Priority order:

    1. SpecialistProfile (master) — bot users tied to a master see
       the master's display name + portfolio avatar.
    2. Profile (client) — full_name + avatar field.
    3. Fallbacks — empty strings if neither profile exists.

    The bot consumer treats empty strings as "Ayla cleared the field"
    rather than "missing data" — same semantic as the profile_client
    F3 fix on bot side. The contract is: keys ALWAYS present, values
    MAY be empty.
    """
    # OneToOne reverse accessor raises ``RelatedObjectDoesNotExist`` —
    # NOT ``None`` — when no related row exists, so ``getattr(..., None)``
    # does not work here. Catch the model-specific DoesNotExist.
    try:
        specialist: SpecialistProfile = user.specialist_profile
    except SpecialistProfile.DoesNotExist:
        specialist = None  # type: ignore[assignment]
    if specialist is not None:
        display_name = specialist.display_name or ""
        avatar_url = specialist.avatar.url if specialist.avatar else ""
        return display_name, avatar_url

    try:
        profile: Profile = user.profile
    except Profile.DoesNotExist:
        profile = None  # type: ignore[assignment]
    if profile is not None:
        display_name = profile.full_name or ""
        avatar_url = profile.avatar.url if profile.avatar else ""
        return display_name, avatar_url

    return "", ""


class InternalUserProfileView(APIView):
    """GET /api/v1/internal/users/{user_id}/ — codex P1-3.

    Returns the PII §7 subset (display_name + avatar_url) for the
    given user. Bearer-only auth via IsInternalBearer; no
    X-External-User-ID required (catalog-shaped lookup, not
    on-behalf-of-user write).
    """

    # JWT auth is OFF — bot is the caller, identity comes from the
    # service bearer. Same pattern as the records / masters /
    # payments internal endpoints (Block A A5 wiring): no
    # AllowAny mixin, IsInternalBearer alone gates.
    authentication_classes: list = []
    permission_classes = [IsInternalBearer]
    serializer_class = _InternalUserProfileSerializer

    @extend_schema(
        operation_id="internal_user_profile_retrieve",
        tags=["internal"],
        responses={
            200: _InternalUserProfileSerializer,
            404: OpenApiResponse(
                description="User does not exist (deleted or wrong id)",
                response=inline_serializer(
                    name="InternalUserProfileNotFound",
                    fields={"error": serializers.DictField()},
                ),
            ),
            401: OpenApiResponse(description="Missing / invalid bearer token"),
        },
        description=(
            "Internal service-to-service fetch of a single user's "
            "approved PII subset (display_name + avatar_url). Bot's "
            "user.profile.updated consumer calls this after an Ayla "
            "emit to refresh the local BotUser mirror. No phone / "
            "email / birthday / language exposed by design."
        ),
    )
    def get(self, request: Request, user_id: UUID) -> Response:
        try:
            user = (
                User.objects
                .select_related("profile", "specialist_profile")
                .get(pk=user_id)
            )
        except User.DoesNotExist:
            # 404 is intentional — bot's profile_client treats this as
            # non-retryable: the user is gone, retrying won't bring
            # them back. Ops can investigate from the event_id.
            logger.info(
                "internal.users.profile_not_found user_id=%s",
                user_id,
            )
            return error_response(
                "NOT_FOUND",
                "User not found.",
                status_code=404,
            )

        display_name, avatar_url = _resolve_profile(user)
        return Response(
            _InternalUserProfileSerializer(
                {"display_name": display_name, "avatar_url": avatar_url},
            ).data
        )
