"""Отзыв клиента из бота — под субъектом (DRF-1855, карта кабинета K12; DRF-1527).

``POST /api/v1/internal/users/{user_id}/reviews/``

До этого отзыв мог оставить только клиент в приложении по своему JWT; у
бота внутреннего контура отзывов не было, и ответ человека на «как прошло?»
некуда было положить. Правила — те же, что у ``POST /api/v1/reviews/``, и
живут в одном месте (:func:`reviews.services.create_review`): своя
завершённая запись, один отзыв на визит, пересчёт рейтинга.

**Субъект.** Сторож — :class:`~users.permissions.IsInternalBearerForSubject`:
runtime-Bearer, названный ``X-External-User-ID``, связь личности, и клиент в
URL — ровно этот актор. Отзыв пишется от его имени, поэтому чужой
``user_id`` — 403 до чтения тела.

**Журнал §96.** Отзыв — текст самого человека о его визите, сохраняемый от
его имени: это запись персональных данных субъекта, и доступ журналируется
(``ObjectCategory.REVIEW`` / ``Operation.REVIEW_CREATE``).
"""
from __future__ import annotations

import logging
from uuid import UUID

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from privacy_audit.mixins import AuditedPersonalDataAccess
from privacy_audit.models import PersonalDataAccessLog
from reviews.serializers import ReviewCreateSerializer, ReviewDetailSerializer
from reviews.services import ReviewRefused, create_review
from users.models import User
from users.permissions import IsInternalBearerForSubject
from users.response import error_response, success_response

logger = logging.getLogger(__name__)


class InternalReviewCreateView(AuditedPersonalDataAccess, APIView):
    """См. докстринг модуля."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSubject]
    subject_url_kwarg = "user_id"
    audit_object_category = PersonalDataAccessLog.ObjectCategory.REVIEW
    audit_operations = {"POST": PersonalDataAccessLog.Operation.REVIEW_CREATE}
    serializer_class = ReviewCreateSerializer

    @extend_schema(
        operation_id="internal_review_create",
        tags=["internal"],
        request=ReviewCreateSerializer,
        responses={
            201: ReviewDetailSerializer,
            400: OpenApiResponse(description="Appointment not completed"),
            403: OpenApiResponse(description="Not the acting subject"),
            404: OpenApiResponse(description="No such appointment of this client"),
            409: OpenApiResponse(description="Review already exists"),
        },
    )
    def post(self, request: Request, user_id: UUID) -> Response:
        client = User.objects.filter(pk=user_id).first()
        if client is None:
            return error_response("NOT_FOUND", "User not found.", status_code=404)

        serializer = ReviewCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            review = create_review(
                client=client,
                appointment_id=data["appointment_id"],
                rating=data["rating"],
                text=data.get("text", ""),
                is_anonymous=data.get("is_anonymous", False),
            )
        except ReviewRefused as refused:
            return error_response(
                refused.code, refused.message, status_code=refused.status_code,
            )
        logger.info(
            "internal.review.created review=%s specialist=%s", review.id, review.specialist_id,
        )
        return success_response(ReviewDetailSerializer(review).data, status_code=201)
