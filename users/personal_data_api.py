"""C5 — internal personal-data export/delete endpoints (152-ФЗ).

PILOT_CONTRACTS_2026-08-15 v1.3.0:

- **C5.1** ``GET /api/v1/internal/users/{ayla_user_id}/personal-data/export/``
  — synchronous JSON: profile subset + full personal-context catalogue
  (declared prefs). The bot (W3) aggregates this with bot-side data into
  the customer-facing export.
- **C5.2 / AMD-006** ``DELETE /api/v1/internal/users/{ayla_user_id}/personal-data/``
  — Ayla-side cascade of the customer delete: wipes UserPersonalContext.
  Idempotent: a repeat request returns 200 with an empty scope.
- **AMD-010** — deletion audit via ``AnalyticsEvent`` (actor, scope,
  initiator), NEVER the deleted personal values.

Pilot scope (C5.2): personal context only. Transactional records
(bookings, payments) follow statutory retention; anonymization is
post-pilot and explicitly out of this contract.

Auth: service-to-service Bearer (``IsInternalBearer``), same pattern as
``users/internal_users_api.py`` — JWT off, no X-External-User-ID.
"""
from __future__ import annotations

import logging
from uuid import UUID

from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import Profile, User, UserPersonalContext
from users.permissions import IsInternalBearer
from users.personal_context_events import emit_personal_data_deleted
from users.personal_context_views import UserPersonalContextSerializer
from users.response import error_response, success_response

logger = logging.getLogger(__name__)


def _get_live_user(user_id: UUID) -> User | None:
    """Fetch the user; soft-deleted accounts count as gone (404)."""
    return (
        User.objects
        .filter(pk=user_id, deleted_at__isnull=True)
        .select_related("profile")
        .first()
    )


def _not_found(request: Request, user_id: UUID) -> Response:
    logger.info(
        "internal.personal_data.user_not_found user_id=%s request_id=%s",
        user_id, getattr(request, "request_id", "-"),
    )
    return error_response("NOT_FOUND", "User not found.", status_code=404)


class InternalPersonalDataExportView(APIView):
    """GET …/personal-data/export/ — C5.1 synchronous JSON export."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]

    @extend_schema(
        operation_id="internal_personal_data_export",
        tags=["internal"],
        responses={
            200: inline_serializer(
                name="InternalPersonalDataExport",
                fields={"data": serializers.DictField()},
            ),
            401: OpenApiResponse(description="Missing / invalid bearer token"),
            404: OpenApiResponse(description="User does not exist"),
        },
        description=(
            "152-ФЗ personal-data export (C5.1): profile subset "
            "(phone, email, full_name, bio, city) + the full "
            "personal-context catalogue. Synchronous JSON; archives "
            "are post-pilot."
        ),
    )
    def get(self, request: Request, user_id: UUID) -> Response:
        user = _get_live_user(user_id)
        if user is None:
            return _not_found(request, user_id)

        # Profile subset — client profile only (bot mirrors the
        # specialist display pair via InternalUserProfileView). Closed
        # field list: extend deliberately, never via serializer drift.
        profile: Profile | None = getattr(user, "profile", None)
        profile_data = {
            "phone": user.phone or "",
            "email": user.email or "",
            "full_name": (profile.full_name if profile else "") or "",
            "bio": (profile.bio if profile else "") or "",
            "city": (profile.city if profile else "") or "",
        }

        # Full personal-context catalogue (declared prefs + provenance);
        # null when the user never personalised (no row — no lazy create
        # on export, an export must not CREATE data about the user).
        ctx = UserPersonalContext.objects.filter(user=user).first()
        context_data = (
            UserPersonalContextSerializer(ctx).data if ctx is not None else None
        )

        logger.info(
            "internal.personal_data.exported user_id=%s request_id=%s",
            user_id, getattr(request, "request_id", "-"),
        )
        return success_response({
            "user_id": str(user.pk),
            "exported_at": timezone.now().isoformat(),
            "profile": profile_data,
            "personal_context": context_data,
        })


class InternalPersonalDataDeleteView(APIView):
    """DELETE …/personal-data/ — C5.2/AMD-006 idempotent wipe + audit."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]

    @extend_schema(
        operation_id="internal_personal_data_delete",
        tags=["internal"],
        request=None,
        responses={
            200: inline_serializer(
                name="InternalPersonalDataDelete",
                fields={"data": serializers.DictField()},
            ),
            401: OpenApiResponse(description="Missing / invalid bearer token"),
            404: OpenApiResponse(description="User does not exist"),
        },
        description=(
            "152-ФЗ personal-data delete (C5.2): wipes the Ayla-side "
            "personal context. Idempotent — a repeat DELETE returns 200 "
            "with an empty scope. Every call writes an AnalyticsEvent "
            "audit record (AMD-010) without personal values."
        ),
    )
    def delete(self, request: Request, user_id: UUID) -> Response:
        user = _get_live_user(user_id)
        if user is None:
            return _not_found(request, user_id)

        deleted_count, _ = UserPersonalContext.objects.filter(user=user).delete()
        scope = ["personal_context"] if deleted_count else []

        # AMD-010 audit — best-effort (emit helper never raises); every
        # request is audited, repeats included (scope=[] = nothing left).
        emit_personal_data_deleted(user, scope=scope, initiator="internal_api")
        logger.info(
            "internal.personal_data.deleted user_id=%s scope=%s request_id=%s",
            user_id, scope, getattr(request, "request_id", "-"),
        )
        return success_response({
            "user_id": str(user.pk),
            "deleted": scope,
        })
