"""Место работы соло-мастера под субъектом (DRF-1803, M11; макет 5).

``GET  /api/v1/internal/specialists/{specialist_id}/service-locations/`` — своё место и зоны выезда;
``POST …/service-locations/`` — новое место (``kind`` = ``private_studio`` / ``salon_or_studio``)
или зона выезда (``kind`` = ``mobile``);
``PATCH …/service-locations/{item_id}/`` — поля своего места или охват своей зоны.

Правила записи — в :mod:`tenants.master_places`, здесь только ввод и ответ.

**Субъект** — :class:`~users.permissions.IsInternalBearerForSpecialistSubject`: профиль
в URL — свой; владелец DRAFT-workspace до связи — по claim (M28, G1): место —
настройка workspace, как часы и услуги.

**Журнал §96.** Своё место мастера выгружается субъекту и стирается при удалении
аккаунта — это его данные: ``ObjectCategory.SPECIALIST_PROFILE`` /
``READ_SERVICE_LOCATION``, ``WRITE_SERVICE_LOCATION``. Обе операции — не
FAIL_CLOSED: закрытый список политики называет удаление и раскрытие, запись
полей в него не входит.

**Тело разбирается здесь, без сериализатора.** Разрешённые поля перечислены;
остальное — 400, в том числе ``tenant_id`` (тенант места — всегда workspace
мастера) и ``status`` (место подтверждает человек, не мастер).
"""
from __future__ import annotations

import logging
from uuid import UUID

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.errors import ErrorCode
from privacy_audit.mixins import AuditedPersonalDataAccess
from privacy_audit.models import PersonalDataAccessLog
from tenants.master_places import (
    ADDRESS_MAX,
    AREA_KINDS,
    COVERAGES,
    LABEL_MAX,
    NOTE_MAX,
    PLACE_KINDS,
    PlaceRefused,
    create_area,
    create_place,
    owned_item,
    state,
    update_area,
    update_place,
)
from tenants.models import ServiceLocation
from users.models import SpecialistProfile
from users.permissions import IsInternalBearerForSpecialistSubject
from users.response import error_response, success_response

logger = logging.getLogger(__name__)

_Op = PersonalDataAccessLog.Operation

_PLACE_FIELDS = frozenset({"kind", "address", "label", "note_for_client"})
_AREA_FIELDS = frozenset({"kind", "coverage"})
_AREA_PATCH_FIELDS = frozenset({"coverage"})

_REFUSALS = {
    403: OpenApiResponse(description="Not the acting subject"),
    404: OpenApiResponse(description="Specialist or item not found"),
    409: OpenApiResponse(description="Not a solo workspace, or the place/area is already set"),
}


def _invalid(details: dict) -> Response:
    return error_response(
        ErrorCode.VALIDATION_ERROR, "Invalid service location.", details=details,
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _refused(exc: PlaceRefused) -> Response:
    return error_response(
        ErrorCode.CONFLICT, "Service location is not available for this workspace.",
        details={"reason": exc.reason}, status_code=status.HTTP_409_CONFLICT,
    )


def _no_specialist() -> Response:
    return error_response(
        ErrorCode.SPECIALIST_NOT_FOUND, "Specialist not found.", status_code=status.HTTP_404_NOT_FOUND,
    )


def _body(request: Request) -> dict | None:
    return request.data if isinstance(request.data, dict) else None


def _text(data: dict, name: str, *, limit: int, required: bool, errors: dict) -> str:
    value = data.get(name, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        errors[name] = "must be a string"
        return ""
    text = value.strip()
    if required and not text:
        errors[name] = "required"
    elif len(text) > limit:
        errors[name] = f"at most {limit} characters"
    return text


def _place_fields(data: dict, *, partial: bool) -> tuple[dict, dict]:
    errors: dict = {}
    fields: dict = {}
    if not partial or "kind" in data:
        if data.get("kind") in PLACE_KINDS:
            fields["kind"] = data["kind"]
        else:
            errors["kind"] = f"one of {sorted(PLACE_KINDS)}"
    if not partial or "address" in data:
        fields["address"] = _text(data, "address", limit=ADDRESS_MAX, required=True, errors=errors)
    for name, limit in (("label", LABEL_MAX), ("note_for_client", NOTE_MAX)):
        if not partial or name in data:
            fields[name] = _text(data, name, limit=limit, required=False, errors=errors)
    return fields, errors


def _unknown(data: dict, allowed: frozenset) -> Response | None:
    unknown = sorted(set(data) - allowed)
    return _invalid({"unknown_fields": unknown}) if unknown else None


class _PlacesSurface(AuditedPersonalDataAccess, APIView):
    """Общее у двух ручек.

    ``permission_classes`` здесь намеренно не задан — его объявляет каждая
    ручка: сторож журнала перечисляет классы с субъектным сторожем, и база
    без маршрута читалась бы там как «защищена, но не журналируется».
    """

    authentication_classes: list = []
    subject_url_kwarg = "specialist_id"
    audit_object_category = PersonalDataAccessLog.ObjectCategory.SPECIALIST_PROFILE

    @staticmethod
    def _profile(specialist_id: UUID) -> SpecialistProfile | None:
        return SpecialistProfile.objects.select_related("tenant", "works_at").filter(pk=specialist_id).first()


class InternalSpecialistServiceLocationsView(_PlacesSurface):
    """См. докстринг модуля."""

    permission_classes = [IsInternalBearerForSpecialistSubject]
    audit_operations = {"GET": _Op.READ_SERVICE_LOCATION, "POST": _Op.WRITE_SERVICE_LOCATION}

    @extend_schema(
        operation_id="internal_specialist_service_locations_list",
        tags=["internal"],
        responses={200: OpenApiTypes.OBJECT, **_REFUSALS},
    )
    def get(self, request: Request, specialist_id: UUID) -> Response:
        profile = self._profile(specialist_id)
        if profile is None:
            return _no_specialist()
        try:
            return success_response(state(profile))
        except PlaceRefused as exc:
            return _refused(exc)

    @extend_schema(
        operation_id="internal_specialist_service_locations_create",
        tags=["internal"],
        request=OpenApiTypes.OBJECT,
        responses={201: OpenApiTypes.OBJECT, 400: OpenApiResponse(description="Invalid body"), **_REFUSALS},
    )
    def post(self, request: Request, specialist_id: UUID) -> Response:
        profile = self._profile(specialist_id)
        if profile is None:
            return _no_specialist()
        data = _body(request)
        if data is None:
            return _invalid({"body": "a JSON object is expected"})
        kind = data.get("kind")
        if kind not in PLACE_KINDS | AREA_KINDS:
            return _invalid({"kind": f"one of {sorted(PLACE_KINDS | AREA_KINDS)}"})
        refusal = _unknown(data, _PLACE_FIELDS if kind in PLACE_KINDS else _AREA_FIELDS)
        if refusal is not None:
            return refusal
        try:
            if kind in PLACE_KINDS:
                fields, errors = _place_fields(data, partial=False)
                if errors:
                    return _invalid(errors)
                place = create_place(profile.pk, **fields)
                logger.info("internal.service_location.created place=%s specialist=%s", place.pk, profile.pk)
            else:
                if data.get("coverage") not in COVERAGES:
                    return _invalid({"coverage": f"one of {sorted(COVERAGES)}"})
                area = create_area(profile.pk, coverage=data["coverage"])
                logger.info("internal.service_area.created area=%s specialist=%s", area.pk, profile.pk)
        except PlaceRefused as exc:
            return _refused(exc)
        return success_response(state(self._profile(specialist_id)), status_code=status.HTTP_201_CREATED)


class InternalSpecialistServiceLocationView(_PlacesSurface):
    """См. докстринг модуля."""

    permission_classes = [IsInternalBearerForSpecialistSubject]
    audit_operations = {"PATCH": _Op.WRITE_SERVICE_LOCATION}

    @extend_schema(
        operation_id="internal_specialist_service_location_update",
        tags=["internal"],
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiResponse(description="Invalid body"), **_REFUSALS},
    )
    def patch(self, request: Request, specialist_id: UUID, item_id: UUID) -> Response:
        profile = self._profile(specialist_id)
        if profile is None:
            return _no_specialist()
        data = _body(request)
        if data is None:
            return _invalid({"body": "a JSON object is expected"})
        try:
            item = owned_item(profile, item_id)
            if item is None:
                return error_response(
                    ErrorCode.NOT_FOUND, "Service location not found.", status_code=status.HTTP_404_NOT_FOUND,
                )
            if isinstance(item, ServiceLocation):
                refusal = _unknown(data, _PLACE_FIELDS)
                if refusal is not None:
                    return refusal
                fields, errors = _place_fields(data, partial=True)
                if errors:
                    return _invalid(errors)
                update_place(profile.pk, item.pk, fields)
            else:
                refusal = _unknown(data, _AREA_PATCH_FIELDS)
                if refusal is not None:
                    return refusal
                if data.get("coverage") not in COVERAGES:
                    return _invalid({"coverage": f"one of {sorted(COVERAGES)}"})
                update_area(profile.pk, item.pk, coverage=data["coverage"])
        except PlaceRefused as exc:
            return _refused(exc)
        return success_response(state(self._profile(specialist_id)))
