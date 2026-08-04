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
from users.permissions import IsIdentityProvisioningBearer, IsInternalBearer
from users.response import error_response, success_response

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
            # Include request_id for cross-service log correlation —
            # bot-side dead-letter logs already carry their event_id;
            # request_id stamped by users/middleware.py:RequestIDMiddleware
            # closes the loop when ops triages a not-found across both
            # sides.
            logger.info(
                "internal.users.profile_not_found user_id=%s request_id=%s",
                user_id, getattr(request, "request_id", "-"),
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


class _BindExternalIdentityRequestSerializer(serializers.Serializer):
    """Request body for POST /internal/users/bind-external/.

    Both sides are named explicitly in the body — same defense-in-depth
    idea as ``client_id`` on the payments surface: a leaked bearer alone
    is not enough to silently re-point an identity, the caller must know
    the target ``ayla_user_id``.
    """

    external_user_id = serializers.CharField(max_length=200)
    ayla_user_id = serializers.UUIDField()


class _BindExternalIdentityResponseSerializer(serializers.Serializer):
    external_user_id = serializers.CharField()
    ayla_user_id = serializers.UUIDField()
    proxy_user_id = serializers.UUIDField()
    proxy_created = serializers.BooleanField()
    bound = serializers.BooleanField()


class InternalBindExternalIdentityView(APIView):
    """POST /api/v1/internal/users/bind-external/ — E2E-BOT-02B.

    Establishes the Phase C proxy→real binding: after this call,
    ``resolve_external_user(external_user_id)`` — and therefore every
    ``IsBotServiceWithVerifiedClient`` surface (records, booking,
    payments, nutrition) — resolves the bound REAL account instead of
    the isolated proxy.

    PROVISIONING-ONLY (security review P1-1). The request body names
    the target account explicitly and the endpoint performs no
    server-side proof of ownership, so it is gated by
    ``IsIdentityProvisioningBearer`` — a credential provisioned
    independently of the general bot service token. The standard BOT
    runtime credential (``AYLA_INTERNAL_API_TOKEN``) is rejected here
    by construction. Production bot-driven binding is NOT supported
    until a verified ownership flow exists (AYLA-DEC-0016 §6: relink
    only for verified identity references); until then this endpoint
    serves trusted provisioning / E2E bootstrap / ops only.

    Contract narrowing (security review P1-2): the target must be a
    real, active, non-soft-deleted CLIENT account — binding an external
    identity to a staff/admin or proxy account is rejected with the
    same info-hidden 404 as an unknown id. Binding is one-way: a
    re-bind to a different account is a 409 conflict, never a silent
    overwrite; concurrent binds serialize on the proxy row lock
    (P1-1). Success/idempotent outcomes are audited ATOMICALLY with
    the binding (the audit write joins the binding transaction and its
    failure rolls the binding back); conflict/rejected outcomes are
    audited best-effort since no mutation happened (P1-3).

    Bearer-only auth (IsIdentityProvisioningBearer); no
    X-External-User-ID header — the identity being bound is named in
    the body. Catalog-shaped lookup is N/A here: the operation is keyed
    by the pair (external_user_id, ayla_user_id), both required.

    Post-bind contract: the caller MUST persist the returned
    ``ayla_user_id`` and send it wherever a ``client_id`` cross-check
    applies (payments internal) — from the bind on, s2s surfaces
    resolve the REAL account, and a stale proxy id 403s by design.
    Binding re-points resolution only; pre-binding proxy-held data
    (food logs, personal context, appointments) is NOT migrated —
    a separate managed operation, Wave 2.

    Responses: 200 bound (idempotent when re-binding the same pair),
    400 malformed external_user_id, 403 missing/invalid bearer —
    including a valid GENERAL bot token (DRF permission denial — the
    endpoint declares no authentication classes, so a failed bearer
    check is 403, not 401), 404 unknown/unbindable ayla_user_id,
    409 external identity already bound to a DIFFERENT account or
    naming an existing non-proxy account.
    """

    authentication_classes: list = []
    permission_classes = [IsIdentityProvisioningBearer]
    serializer_class = _BindExternalIdentityRequestSerializer

    @extend_schema(
        operation_id="internal_users_bind_external",
        tags=["internal"],
        request=_BindExternalIdentityRequestSerializer,
        responses={
            200: _BindExternalIdentityResponseSerializer,
            400: OpenApiResponse(description="Malformed external_user_id"),
            403: OpenApiResponse(
                description="Missing / invalid provisioning bearer token "
                            "(the general bot service token is NOT accepted)",
            ),
            404: OpenApiResponse(description="ayla_user_id not bindable"),
            409: OpenApiResponse(
                description="External identity bound to a different account "
                            "or names an existing non-proxy account",
            ),
        },
        description=(
            "PROVISIONING-ONLY: bind a bot external identity "
            "(X-External-User-ID value) to a real Ayla account. Gated by a "
            "dedicated provisioning credential — the standard BOT runtime "
            "token cannot call this endpoint. Production bot-driven binding "
            "is not supported until a verified ownership flow exists. "
            "After binding, all on-behalf-of-user s2s endpoints resolve "
            "the real account for this external id."
        ),
    )
    def post(self, request: Request) -> Response:
        from users.services import (
            IdentityBindingError,
            InvalidExternalUserIDError,
            bind_external_identity,
        )

        serializer = _BindExternalIdentityRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        external_user_id = serializer.validated_data["external_user_id"]
        ayla_user_id = serializer.validated_data["ayla_user_id"]

        request_id = getattr(request, "request_id", None)
        try:
            proxy, created = bind_external_identity(
                external_user_id, ayla_user_id,
                initiator="identity_provisioning", request_id=request_id,
            )
        except InvalidExternalUserIDError as exc:
            return error_response("VALIDATION_ERROR", str(exc))
        except IdentityBindingError as exc:
            return error_response(exc.code, str(exc), status_code=exc.status_code)

        logger.info(
            "internal.users.bind_external external_user_id=%s ayla_user_id=%s "
            "proxy_created=%s request_id=%s",
            external_user_id, ayla_user_id, created,
            request_id or "-",
        )
        return success_response({
            "external_user_id": external_user_id,
            "ayla_user_id": str(ayla_user_id),
            "proxy_user_id": str(proxy.pk),
            "proxy_created": created,
            "bound": True,
        })
