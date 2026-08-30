"""Reviews views — DRF-96."""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Avg, Count
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from appointments.models import Appointment
from users.response import error_response, success_response

from users.permissions import IsClient, IsSpecialist
from .models import Review
from .serializers import (
    ReviewCreateSerializer, ReviewDetailSerializer,
    ReviewListSerializer, ReviewReplySerializer, ReviewUpdateSerializer,
)

logger = logging.getLogger(__name__)


def _recalculate_rating(specialist) -> None:
    """
    Recalculate and persist specialist rating + reviews_count.

    Synchronous — called inside the create-review transaction.
    When Celery is available, this can be extracted into a shared_task.
    """
    result = Review.objects.filter(
        specialist=specialist, is_hidden=False,
    ).aggregate(
        avg=Avg('rating'),
        cnt=Count('id'),
    )
    avg = result['avg'] or 0
    cnt = result['cnt'] or 0

    specialist.__class__.objects.filter(id=specialist.id).update(
        rating=round(avg, 1),
        reviews_count=cnt,
    )
    logger.info(
        "Rating recalculated: specialist=%s rating=%.1f count=%d",
        specialist.id, avg, cnt,
    )


class ReviewPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class ReviewCreateView(APIView):
    """
    POST /api/v1/reviews/

    Client creates a review for a completed appointment.
    Rules:
    - appointment.status must be 'completed'
    - appointment.client must be the current user
    - One review per appointment (OneToOne)
    """
    permission_classes = [permissions.IsAuthenticated, IsClient]
    serializer_class = ReviewCreateSerializer

    @extend_schema(
        request=ReviewCreateSerializer,
        responses={
            201: ReviewDetailSerializer,
            400: OpenApiResponse(description="Appointment not completed"),
            404: OpenApiResponse(description="Appointment not found"),
            409: OpenApiResponse(description="Review already exists for this appointment"),
        },
    )
    def post(self, request: Request) -> Response:
        serializer = ReviewCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        appointment_id = serializer.validated_data['appointment_id']

        # Fetch and validate appointment
        try:
            appointment = Appointment.objects.select_related(
                'specialist', 'service',
            ).get(id=appointment_id, client=request.user)
        except Appointment.DoesNotExist:
            return error_response(
                "NOT_FOUND", "Appointment not found.", status_code=404,
            )

        if appointment.status != Appointment.Status.COMPLETED:
            return error_response(
                "APPOINTMENT_NOT_COMPLETED",
                "Reviews can only be left for completed appointments.",
                status_code=400,
            )

        # Check for duplicate
        if Review.objects.filter(appointment=appointment).exists():
            return error_response(
                "REVIEW_EXISTS",
                "A review for this appointment already exists.",
                status_code=409,
            )

        with transaction.atomic():
            review = Review.objects.create(
                appointment=appointment,
                client=request.user,
                specialist=appointment.specialist,
                service=appointment.service,
                rating=serializer.validated_data['rating'],
                text=serializer.validated_data.get('text', ''),
                is_anonymous=serializer.validated_data.get('is_anonymous', False),
            )
            _recalculate_rating(appointment.specialist)

        logger.info(
            "Review created: id=%s specialist=%s rating=%d",
            review.id, review.specialist_id, review.rating,
        )

        return success_response(
            ReviewDetailSerializer(review).data,
            status_code=201,
        )


class SpecialistReviewsView(APIView):
    """
    GET /api/v1/specialists/{specialist_id}/reviews/

    Public listing of reviews for a specialist.
    Supports ?sort=recent (default) or ?sort=rating
    """
    permission_classes = [permissions.AllowAny]
    serializer_class = ReviewListSerializer

    @extend_schema(
        responses={
            200: ReviewListSerializer(many=True),
            404: OpenApiResponse(description="Specialist not found"),
        },
    )
    def get(self, request: Request, specialist_id) -> Response:
        from users.models import SpecialistProfile

        try:
            specialist = SpecialistProfile.objects.get(id=specialist_id)
        except SpecialistProfile.DoesNotExist:
            return error_response("NOT_FOUND", "Specialist not found.", status_code=404)

        qs = (
            Review.objects
            .filter(specialist=specialist, is_hidden=False)
            .select_related('client', 'service')
        )

        # '-id' closes the sort key. Without it rows sharing a
        # created_at (a moderation backfill writes a batch in one
        # transaction and auto_now_add stamps them identically) are left
        # in no defined order, and each offset page is a separate
        # execution free to order them differently — DRF-1128.
        sort = request.query_params.get('sort', 'recent')
        if sort == 'rating':
            qs = qs.order_by('-rating', '-created_at', '-id')
        else:
            qs = qs.order_by('-created_at', '-id')

        paginator = ReviewPagination()
        page = paginator.paginate_queryset(qs, request)
        serializer = ReviewListSerializer(page, many=True)

        return success_response(
            serializer.data,
            meta={
                'count': paginator.page.paginator.count,
                'page': paginator.page.number,
                'page_size': paginator.get_page_size(request),
                'pages': paginator.page.paginator.num_pages,
            },
        )


class ReviewUpdateView(APIView):
    """
    PATCH /api/v1/reviews/{id}/

    Client edits their own review (text only).
    """
    permission_classes = [permissions.IsAuthenticated, IsClient]
    serializer_class = ReviewUpdateSerializer

    @extend_schema(
        request=ReviewUpdateSerializer,
        responses={
            200: ReviewDetailSerializer,
            404: OpenApiResponse(description="Review not found"),
        },
    )
    def patch(self, request: Request, pk) -> Response:
        try:
            review = Review.objects.select_related('service').get(
                id=pk, client=request.user,
            )
        except Review.DoesNotExist:
            return error_response("NOT_FOUND", "Review not found.", status_code=404)

        serializer = ReviewUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        review.text = serializer.validated_data.get('text', review.text)
        review.save(update_fields=['text', 'updated_at'])

        return success_response(ReviewDetailSerializer(review).data)


class ReviewReplyView(APIView):
    """
    POST /api/v1/reviews/{id}/reply/

    Specialist replies to a review about them.
    """
    permission_classes = [permissions.IsAuthenticated, IsSpecialist]
    serializer_class = ReviewReplySerializer

    @extend_schema(
        request=ReviewReplySerializer,
        responses={
            200: ReviewDetailSerializer,
            403: OpenApiResponse(description="Access denied"),
            404: OpenApiResponse(description="Review not found"),
        },
    )
    def post(self, request: Request, pk) -> Response:
        try:
            review = Review.objects.select_related(
                'specialist__user', 'service',
            ).get(id=pk)
        except Review.DoesNotExist:
            return error_response("NOT_FOUND", "Review not found.", status_code=404)

        # Only the specialist this review is about can reply
        if review.specialist.user_id != request.user.id:
            raise PermissionDenied("Access denied.")

        serializer = ReviewReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        review.specialist_reply = serializer.validated_data['text']
        review.save(update_fields=['specialist_reply', 'updated_at'])

        return success_response(ReviewDetailSerializer(review).data)
