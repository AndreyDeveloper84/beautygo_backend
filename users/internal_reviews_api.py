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

from django.db.models import Avg, Count

from privacy_audit.mixins import AuditedPersonalDataAccess
from privacy_audit.models import PersonalDataAccessLog
from reviews.models import Review
from reviews.serializers import ReviewCreateSerializer, ReviewDetailSerializer
from reviews.services import ReviewRefused, create_review
from users.models import SpecialistProfile, User
from users.permissions import IsInternalBearerForSpecialistSubject, IsInternalBearerForSubject
from users.public_name import CLIENT_LABEL, initial_person_name
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


class InternalSpecialistReviewsView(AuditedPersonalDataAccess, APIView):
    """``GET /api/v1/internal/specialists/{specialist_id}/reviews/`` — «Мои отзывы».

    DRF-1857 (карта кабинета K14, обещание V04). Мастер читает отзывы о себе
    из кабинета в боте; до этого у бота был только публичный список, а экран
    «Отзывы» стоял заглушкой.

    **Субъект** — :class:`IsInternalBearerForSpecialistSubject`, как у часов:
    профиль в URL — собственный профиль актора, чужой — 403.

    **Что отдаётся.** Только видимые отзывы (``is_hidden=False``), новые
    первыми, не больше :attr:`LIMIT`; число и средняя оценка — по тем же
    видимым отзывам, оценка — только когда отзыв есть (ноль отзывов — «нет
    данных», а не 0.0). Клиент — именем и первой буквой фамилии, без имени —
    «Клиент», анонимный отзыв — без имени; телефона, фамилии целиком и
    ``username`` в ответе нет (правило владельца об общении через Ayla).

    **Журнал §96.** Мастер видит текст и имя другого человека — это доступ к
    персональным данным клиентов, журналируется (``ObjectCategory.REVIEW`` /
    ``Operation.REVIEW_READ``, объект — профиль мастера).
    """

    LIMIT = 50

    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSpecialistSubject]
    subject_url_kwarg = "specialist_id"
    audit_object_category = PersonalDataAccessLog.ObjectCategory.REVIEW
    audit_operations = {"GET": PersonalDataAccessLog.Operation.REVIEW_READ}

    @staticmethod
    def _row(review: Review) -> dict:
        return {
            "id": str(review.id),
            "rating": review.rating,
            "text": review.text,
            "client_name": (
                None if review.is_anonymous
                else initial_person_name(review.client, fallback=CLIENT_LABEL)
            ),
            "service_name": review.service_name,
            "created_at": review.created_at.isoformat(),
        }

    @extend_schema(
        operation_id="internal_specialist_reviews",
        tags=["internal"],
        responses={
            200: OpenApiResponse(description="specialist_id, review_count, rating, reviews[]"),
            403: OpenApiResponse(description="Not the acting subject"),
            404: OpenApiResponse(description="No such specialist"),
        },
    )
    def get(self, request: Request, specialist_id: UUID) -> Response:
        profile = SpecialistProfile.objects.filter(pk=specialist_id).first()
        if profile is None:
            return error_response("NOT_FOUND", "Specialist profile not found.", status_code=404)

        visible = Review.objects.filter(specialist=profile, is_hidden=False)
        stats = visible.aggregate(count=Count("id"), avg=Avg("rating"))
        count = stats["count"] or 0
        rows = (
            visible.select_related("client", "service", "salon_service")
            .order_by("-created_at", "-id")[: self.LIMIT]
        )
        return success_response({
            "specialist_id": str(profile.pk),
            "review_count": count,
            "rating": round(float(stats["avg"]), 1) if count else None,
            "reviews": [self._row(review) for review in rows],
        })
