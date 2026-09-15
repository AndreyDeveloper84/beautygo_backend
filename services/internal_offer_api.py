"""GET/POST /api/v1/internal/specialists/{specialist_id}/services/selection/ (DRF-1800, M8a).

Экран «Выберите услуги» мастера (макет 3, P15/P18) через бота. Сторож —
:class:`IsInternalBearerForSpecialistSubject`: связанный мастер (LINKED)
или, до связи, владелец своего provisioned DRAFT workspace (M28). Логика —
``services/offer_selection.py``; здесь только субъект, форма входа и ответа.

Ответ один на обе операции — текущее состояние выбора: список и два
счётчика, посчитанные сервером (счётчики экрана только из ответа):
``selected`` = активные выбранные строки, ``configured`` = из них те, у
которых есть предложение мастера с ценой и длительностью.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.errors import ErrorCode
from users.permissions import IsInternalBearerForSpecialistSubject
from users.response import error_response, success_response

from .models import ServiceCategory
from .offer_selection import (
    MAX_TEMPLATES_PER_CALL,
    HasFutureAppointments,
    SelectedService,
    SelectionRefused,
    ServiceNotSelected,
    TemplatesNotFound,
    remove_service,
    select_templates,
    selected_services,
    set_offer,
)


class _SelectBody(serializers.Serializer):
    template_ids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        max_length=MAX_TEMPLATES_PER_CALL,
    )


def _category_roots(category_ids: set) -> dict:
    """Самый верхний предок каждой категории (DRF-1912) — одним запросом на дерево.

    Группа экрана 04 — направление, то есть корень дерева категорий ШАБЛОНА
    на любой глубине. Дерево грузится целиком одним запросом и проходится в
    Python, поэтому число запросов не зависит ни от числа строк, ни от глубины.
    Цикл в данных не вешает ответ: проход останавливается на уже виденной вершине.

    Оговорка решения: корни канона (22) ≠ 6 направлений экрана 02 — открытый
    вопрос владельцу G7; до решения группа = корень канона.
    """
    if not category_ids:
        return {}
    nodes = {
        node["id"]: node
        for node in ServiceCategory.objects.values("id", "parent_id", "name", "sort_order")
    }
    roots = {}
    for category_id in category_ids:
        current = category_id
        seen = {current}
        while current in nodes:
            parent = nodes[current]["parent_id"]
            if parent is None or parent not in nodes or parent in seen:
                break
            seen.add(parent)
            current = parent
        roots[category_id] = nodes.get(current)
    return roots


def _item(entry: SelectedService, roots: dict) -> dict:
    row = entry.salon_service
    offer = entry.offer
    template_category = row.template.category if row.template_id else None
    root = roots.get(template_category.pk) if template_category is not None else None
    return {
        "salon_service_id": str(row.pk),
        "template_id": str(row.template_id),
        "name": row.name,
        "category_id": str(row.category_id) if row.category_id else None,
        "is_active": row.is_active,
        "mapping_status": row.mapping_status,
        "offer": None if offer is None else {
            "id": str(offer.pk),
            "price": str(offer.price),
            "duration_minutes": offer.resolved_duration(),
            "is_active": offer.is_active,
        },
        "configured": entry.configured,
        # DRF-1912: группа экрана 04 — от канона (шаблона), не от строки салона.
        "category_name": template_category.name if template_category is not None else None,
        "direction_id": str(root["id"]) if root else None,
        "direction_name": root["name"] if root else None,
        "direction_sort_order": root["sort_order"] if root else None,
    }


def _state(profile) -> dict:
    entries = selected_services(profile)
    roots = _category_roots(
        {e.salon_service.template.category_id for e in entries if e.salon_service.template_id}
    )
    return {
        "specialist_id": str(profile.pk),
        "tenant_id": str(profile.tenant_id),
        "selected": sum(1 for e in entries if e.salon_service.is_active),
        "configured": sum(1 for e in entries if e.configured),
        "services": [_item(e, roots) for e in entries],
    }


def _refused(exc: SelectionRefused) -> Response:
    return error_response(
        ErrorCode.SERVICE_SELECTION_REFUSED,
        "Service selection is not available for this workspace.",
        details={"reason": exc.reason},
        status_code=status.HTTP_409_CONFLICT,
    )


class InternalSpecialistServiceSelectionView(APIView):
    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSpecialistSubject]
    subject_url_kwarg = "specialist_id"

    @staticmethod
    def _profile(specialist_id: UUID):
        from users.models import SpecialistProfile

        return SpecialistProfile.objects.select_related("tenant").filter(pk=specialist_id).first()

    def get(self, request: Request, specialist_id: UUID) -> Response:
        profile = self._profile(specialist_id)
        if profile is None:
            return error_response(
                ErrorCode.SPECIALIST_NOT_FOUND, "Specialist not found.", status_code=404,
            )
        try:
            return success_response(_state(profile))
        except SelectionRefused as exc:
            return _refused(exc)

    def post(self, request: Request, specialist_id: UUID) -> Response:
        profile = self._profile(specialist_id)
        if profile is None:
            return error_response(
                ErrorCode.SPECIALIST_NOT_FOUND, "Specialist not found.", status_code=404,
            )
        body = _SelectBody(data=request.data)
        if not body.is_valid():
            return error_response(
                ErrorCode.VALIDATION_ERROR,
                "template_ids must be a non-empty list of UUIDs.",
                details=body.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            created = select_templates(profile, body.validated_data["template_ids"])
        except SelectionRefused as exc:
            return _refused(exc)
        except TemplatesNotFound as exc:
            return error_response(
                ErrorCode.NOT_FOUND,
                "Service template not found.",
                details={"reason": "template_not_found", "template_ids": exc.template_ids},
                status_code=status.HTTP_404_NOT_FOUND,
            )
        payload = {**_state(profile), "created": created}
        return success_response(
            payload, status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


# --- M8b: PUT …/services/{salon_service_id}/offer/, DELETE …/services/{salon_service_id}/ ---


class _OfferBody(serializers.Serializer):
    price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("1"))
    duration_minutes = serializers.IntegerField(min_value=5, max_value=480)


def _not_selected() -> Response:
    return error_response(
        ErrorCode.NOT_FOUND,
        "Service is not selected in this workspace.",
        details={"reason": "service_not_selected"},
        status_code=status.HTTP_404_NOT_FOUND,
    )


def _no_specialist() -> Response:
    return error_response(
        ErrorCode.SPECIALIST_NOT_FOUND, "Specialist not found.", status_code=404,
    )


class InternalSpecialistServiceOfferView(APIView):
    """PUT — цена и длительность выбранной услуги (шторка 4.2/4.4, P36–P40).

    Первая цена создаёт ``SpecialistService`` мастера (до неё предложения нет,
    см. ``offer_selection``), следующие обновляют ту же строку: 201 / 200.
    Цена >= 1, длительность 5..480 — обе обязательны.
    """

    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSpecialistSubject]
    subject_url_kwarg = "specialist_id"

    def put(self, request: Request, specialist_id: UUID, salon_service_id: UUID) -> Response:
        profile = InternalSpecialistServiceSelectionView._profile(specialist_id)
        if profile is None:
            return _no_specialist()
        body = _OfferBody(data=request.data)
        if not body.is_valid():
            return error_response(
                ErrorCode.VALIDATION_ERROR,
                "price >= 1 and duration_minutes 5..480 are required.",
                details=body.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            offer, created = set_offer(
                profile,
                salon_service_id,
                price=body.validated_data["price"],
                duration_minutes=body.validated_data["duration_minutes"],
            )
        except SelectionRefused as exc:
            return _refused(exc)
        except ServiceNotSelected:
            return _not_selected()
        payload = {**_state(profile), "offer_id": str(offer.pk)}
        return success_response(
            payload, status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class InternalSpecialistSelectedServiceView(APIView):
    """DELETE — «Убрать из моих услуг» (шторка 4.2, P40).

    Будущая живая запись — 409 ``HAS_APPOINTMENTS`` с ``count``; иначе строка
    удаляется или, если её держат записи или решение модератора, выключается
    (``removal``: ``deleted`` / ``deactivated``). Ответ — состояние выбора.
    """

    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSpecialistSubject]
    subject_url_kwarg = "specialist_id"

    def delete(self, request: Request, specialist_id: UUID, salon_service_id: UUID) -> Response:
        profile = InternalSpecialistServiceSelectionView._profile(specialist_id)
        if profile is None:
            return _no_specialist()
        try:
            outcome = remove_service(profile, salon_service_id)
        except SelectionRefused as exc:
            return _refused(exc)
        except ServiceNotSelected:
            return _not_selected()
        except HasFutureAppointments as exc:
            return error_response(
                ErrorCode.HAS_APPOINTMENTS,
                "Service has upcoming appointments.",
                details={"reason": "has_future_appointments", "count": exc.count},
                status_code=status.HTTP_409_CONFLICT,
            )
        return success_response({**_state(profile), "removal": outcome})
