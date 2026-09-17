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
- **C5.3 / AMD-020** ``GET /api/v1/internal/users/{ayla_user_id}/personal-data/erasure-status/``
  (DRF-1984) — authoritative readback of the C5.2 erasure: the state of the
  personal-context row of every identity of the subject and a verdict, never
  the values. The bot's durable retry (ai-bot-platform DRF-1950) says
  «удалено» only after this read.

Subject (DRF-1038): the account AND its linked proxies
(``users.subject_identities.subject_users``). Export adds
``linked_identities`` and (DRF-1918) the specialist profile with the whole
portfolio for every identity that has one; delete erases the personal context of every linked
proxy that holds one. One journal row per request, under the URL subject.

Pilot scope (C5.2): personal context only. Transactional records
(bookings, payments) follow statutory retention; anonymization is
post-pilot and explicitly out of this contract.

Auth: service-to-service Bearer **restricted to the caller's own subject**
(``IsInternalBearerForSubject``, DRF-1617 / B-2.1): ``X-External-User-ID`` is
required and must resolve — without creating a row — to the ``user_id`` in
the URL. Until 12.09.2026 the plain ``IsInternalBearer`` let any holder of the
runtime token export or erase ANY person by UUID.
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

from tenants.models import Tenant
from users.models import Profile, SpecialistPortfolio, SpecialistProfile, User, UserPersonalContext
from privacy_audit.mixins import AuditedPersonalDataAccess
from privacy_audit.models import PersonalDataAccessLog
from users.permissions import IsInternalBearerForSubject
from users.personal_context_erasure import (
    context_row_state,
    erase_personal_context,
    identity_is_erased,
)
from users.personal_context_views import UserPersonalContextSerializer
from users.subject_identities import subject_users
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


def _get_user_for_erasure(user_id: UUID) -> User | None:
    """Fetch the user for a delete, soft-deleted accounts included.

    DRF-1368 — erasure must not depend on the order the person happened to
    press the buttons. When the mobile delete ran first, this endpoint used
    to answer 404 ``NOT_FOUND`` and the bot's cascade gave up, so whoever
    deleted from the app stayed in memory forever. A delete addressed to
    someone already gone is not an error: it is an erasure with nothing left
    to erase, and it says so (``deleted: []``). Export keeps the strict
    ``_get_live_user`` — a deleted account has nothing to hand out.
    """
    return User.objects.filter(pk=user_id).first()


def _profile_subset(user: User) -> dict:
    """Client profile only. Closed field list: extend deliberately, never via
    serializer drift (the bot mirrors the specialist display pair elsewhere)."""
    profile: Profile | None = getattr(user, "profile", None)
    return {
        "phone": user.phone or "",
        "email": user.email or "",
        "full_name": (profile.full_name if profile else "") or "",
        "bio": (profile.bio if profile else "") or "",
        "city": (profile.city if profile else "") or "",
    }


#: Профиль мастера в выгрузке (DRF-1918): поле модели → ключ ответа. Закрытый
#: список, как у клиентского профиля. Стёртое исполнителем удаления поле
#: (``users.deletion_executor.SPECIALIST_PROFILE_ERASED_FIELDS``) обязано быть
#: здесь или в ``SPECIALIST_EXCLUDED_FIELDS`` с причиной; каждое поле модели
#: классифицировано (сторож — ``users/tests/test_personal_data_export_specialist_1918.py``).
SPECIALIST_EXPORTED_FIELDS: dict[str, str] = {
    "display_name": "display_name",
    "bio": "bio",
    "address": "address",
    "location_lat": "location_lat",
    "location_lng": "location_lng",
    "avatar": "avatar_url",
    "experience_years": "experience_years",
    "timezone": "timezone",
    # Собственный MAX-идентификатор субъекта — как external_user_id прокси (#451).
    "provisioned_external_user_id": "provisioned_external_user_id",
    # У соло-мастера это его платёжный субсчёт; только id, в ЮKassa не ходим.
    "yookassa_account_id": "yookassa_account_id",
    # Место: своё выгружается, место салона — нет (``_works_at``).
    "works_at": "works_at",
}

_EXTERNAL_REF = "ссылка на внешнюю систему/организацию, не данные о человеке"

#: Поля профиля мастера, которые в выгрузку не идут, — каждое с причиной.
SPECIALIST_EXCLUDED_FIELDS: dict[str, str] = {
    "id": "служебный ключ строки",
    "user": "субъект выгрузки — его user_id уже в ответе",
    "tenant": _EXTERNAL_REF,
    "booking_source": _EXTERNAL_REF,
    "yclients_company_id": _EXTERNAL_REF,
    "yclients_staff_id": _EXTERNAL_REF,
    "status": "служебное состояние профиля на платформе, не данные о человеке",
    "rating": "производное от отзывов клиентов, не данные о человеке",
    "reviews_count": "производное от отзывов клиентов, не данные о человеке",
    "is_available": "служебный флаг записи, не данные о человеке",
    "is_booking_enabled": "служебный флаг записи, не данные о человеке",
    "created_at": "служебная отметка времени строки",
    "updated_at": "служебная отметка времени строки",
}

WORKS_AT_ORGANISATION = "адрес организации"
WORKS_AT_UNKNOWN_KIND = "вид места не определён"


def _decimal(value) -> str | None:
    return None if value is None else str(value)


def _file_url(fieldfile) -> str | None:
    return fieldfile.url if fieldfile else None


def _works_at(sp: SpecialistProfile) -> dict | None:
    """Место мастера. Своё (без салона или workspace соло-мастера) — выгружается;
    место салона — исключение «адрес организации»; вид, которого код не знает,
    — исключение с причиной, а не молча в выгрузку."""
    place = sp.works_at
    if place is None:
        return None
    tenant = place.tenant
    if tenant is None or tenant.kind == Tenant.Kind.SOLO:
        return {
            "address": place.address,
            "latitude": _decimal(place.latitude),
            "longitude": _decimal(place.longitude),
        }
    if tenant.kind == Tenant.Kind.SALON:
        return {"excluded": WORKS_AT_ORGANISATION}
    return {"excluded": WORKS_AT_UNKNOWN_KIND}


def _specialist_profile(user: User) -> dict | None:
    """Профиль мастера и всё портфолио (DRF-1918); null без строки — выгрузка
    не создаёт данных о человеке. Портфолио — все строки по sort_order: путь
    M21 держит 10, старый Pro-путь пускал 30."""
    sp = SpecialistProfile.objects.filter(user=user).select_related("works_at__tenant").first()
    if sp is None:
        return None
    portfolio = [
        {"image_url": _file_url(item.image), "sort_order": item.sort_order}
        for item in SpecialistPortfolio.objects.filter(specialist=sp).order_by("sort_order", "created_at")
    ]
    return {
        "display_name": sp.display_name,
        "bio": sp.bio,
        "address": sp.address,
        "location_lat": _decimal(sp.location_lat),
        "location_lng": _decimal(sp.location_lng),
        "avatar_url": _file_url(sp.avatar),
        "experience_years": sp.experience_years,
        "timezone": sp.timezone,
        "provisioned_external_user_id": sp.provisioned_external_user_id,
        "yookassa_account_id": sp.yookassa_account_id,
        "works_at": _works_at(sp),
        "portfolio": portfolio,
    }


def _context_data(user: User) -> dict | None:
    """Full personal-context catalogue; null without a row — no lazy create
    on export, an export must not CREATE data about the user."""
    ctx = UserPersonalContext.objects.filter(user=user).first()
    return UserPersonalContextSerializer(ctx).data if ctx is not None else None


def _not_found(request: Request, user_id: UUID) -> Response:
    logger.info(
        "internal.personal_data.user_not_found user_id=%s request_id=%s",
        user_id, getattr(request, "request_id", "-"),
    )
    return error_response("NOT_FOUND", "User not found.", status_code=404)


class InternalPersonalDataExportView(AuditedPersonalDataAccess, APIView):
    """GET …/personal-data/export/ — C5.1 synchronous JSON export."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSubject]
    subject_url_kwarg = "user_id"
    audit_object_category = PersonalDataAccessLog.ObjectCategory.PERSONAL_DATA
    audit_operations = {"GET": PersonalDataAccessLog.Operation.EXPORT}

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
            "personal-context catalogue + the specialist profile with "
            "the whole portfolio (DRF-1918). Synchronous JSON; archives "
            "are post-pilot."
        ),
    )
    def get(self, request: Request, user_id: UUID) -> Response:
        user = _get_live_user(user_id)
        if user is None:
            return _not_found(request, user_id)

        profile_data = _profile_subset(user)
        context_data = _context_data(user)
        # DRF-1038: rows written on a proxy BEFORE it was bound stay on the
        # proxy; they belong to this subject and are exported with it.
        linked = [
            {
                "external_user_id": identity.username,
                "profile": _profile_subset(identity),
                "personal_context": _context_data(identity),
                "specialist_profile": _specialist_profile(identity),
            }
            for identity in subject_users(user)[1:]
        ]

        logger.info(
            "internal.personal_data.exported user_id=%s request_id=%s",
            user_id, getattr(request, "request_id", "-"),
        )
        return success_response({
            "user_id": str(user.pk),
            "exported_at": timezone.now().isoformat(),
            "profile": profile_data,
            "personal_context": context_data,
            "specialist_profile": _specialist_profile(user),
            "linked_identities": linked,
        })


class InternalPersonalDataDeleteView(AuditedPersonalDataAccess, APIView):
    """DELETE …/personal-data/ — C5.2/AMD-006 idempotent wipe + audit."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSubject]
    subject_url_kwarg = "user_id"
    #: DRF-1947 — стирание уже удалённого через бота (C5.2, идемпотентно).
    allow_inactive_subject = "стирание уже удалённого субъекта через бота идемпотентно (C5.2)"
    audit_object_category = PersonalDataAccessLog.ObjectCategory.PERSONAL_DATA
    audit_operations = {"DELETE": PersonalDataAccessLog.Operation.DELETE}

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
            "with an empty scope, and so does a DELETE for an account that "
            "was already deleted from the app (DRF-1368). Every call writes "
            "an AnalyticsEvent audit record (AMD-010) without personal values."
        ),
    )
    def delete(self, request: Request, user_id: UUID) -> Response:
        user = _get_user_for_erasure(user_id)
        if user is None:
            return _not_found(request, user_id)

        # DRF-1366 — one verb for every erasure path. It leaves a
        # tombstone (all fields at their default, provenance ``erased``)
        # so tonight's inference cannot refill what this call emptied,
        # and it writes the AMD-010 audit itself — repeats included,
        # scope=[] meaning nothing was left to remove.
        # DRF-1038: the account always; a linked proxy only when it holds a
        # context row — an erasure never CREATES a tombstone (and an audit
        # event) for an identity that had nothing.
        scope: list[str] = []
        for identity in subject_users(user):
            if identity is not user and not UserPersonalContext.objects.filter(
                user=identity
            ).exists():
                continue
            for item in erase_personal_context(identity, initiator="internal_api"):
                if item not in scope:
                    scope.append(item)
        logger.info(
            "internal.personal_data.deleted user_id=%s scope=%s request_id=%s",
            user_id, scope, getattr(request, "request_id", "-"),
        )
        return success_response({
            "user_id": str(user.pk),
            "deleted": scope,
        })


class InternalPersonalDataErasureStatusView(AuditedPersonalDataAccess, APIView):
    """GET …/personal-data/erasure-status/ — C5.3/AMD-020 readback стирания (DRF-1984).

    Бот повторяет стирание (ai-bot-platform DRF-1950) и пишет человеку
    «удалено» только после этого чтения. Ни ответ DELETE (``deleted: []`` не
    отличает «уже стёрто» от «ничего не было»), ни экспорт (отдаёт все
    персданные, удалённому — 403) этого не доказывают. Здесь — состояние
    строки по каждой личности и вердикт; без значений, без внешних
    идентификаторов; ничего не создаёт.
    """

    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSubject]
    subject_url_kwarg = "user_id"
    #: DRF-1984 — подтвердить стирание уже удалённого можно только чтением у удалённого.
    allow_inactive_subject = "статус стирания уже удалённого субъекта — подтверждение C5.2 (DRF-1984)"
    audit_object_category = PersonalDataAccessLog.ObjectCategory.PERSONAL_DATA
    audit_operations = {"GET": PersonalDataAccessLog.Operation.ERASURE_STATUS_READ}

    @extend_schema(
        operation_id="internal_personal_data_erasure_status",
        tags=["internal"],
        responses={
            200: inline_serializer(
                name="InternalPersonalDataErasureStatus",
                fields={"data": serializers.DictField()},
            ),
            401: OpenApiResponse(description="Missing / invalid bearer token"),
            404: OpenApiResponse(description="User does not exist"),
        },
        description=(
            "Readback of the C5.2 erasure (C5.3, DRF-1984): for every identity "
            "of the subject — kind (account | linked_identity), context_row "
            "(absent | tombstone | holds_values | not_erased) and erased; plus "
            "the overall verdict. No personal values, no external ids; reads "
            "a deleted subject too; creates nothing."
        ),
    )
    def get(self, request: Request, user_id: UUID) -> Response:
        user = _get_user_for_erasure(user_id)
        if user is None:
            return _not_found(request, user_id)

        identities = []
        for identity in subject_users(user):
            is_account = identity.pk == user.pk
            state = context_row_state(identity)
            identities.append({
                "kind": "account" if is_account else "linked_identity",
                "context_row": state,
                "erased": identity_is_erased(identity, is_account=is_account, state=state),
            })
        erased = all(item["erased"] for item in identities)
        logger.info(
            "internal.personal_data.erasure_status user_id=%s erased=%s states=%s request_id=%s",
            user_id, erased, [item["context_row"] for item in identities],
            getattr(request, "request_id", "-"),
        )
        return success_response({
            "user_id": str(user.pk),
            "erased": erased,
            "identities": identities,
        })
