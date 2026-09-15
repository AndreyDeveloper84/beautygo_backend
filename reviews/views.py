"""Reviews views — DRF-96."""
from __future__ import annotations

import logging

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from users.response import error_response, success_response

from users.permissions import IsClient, IsSpecialist
from .models import Review
from .services import ReviewRefused, create_review, recalculate_rating
from .serializers import (
    ReviewCreateSerializer, ReviewDetailSerializer,
    ReviewListSerializer, ReviewReplySerializer, ReviewUpdateSerializer,
)

logger = logging.getLogger(__name__)


def _recalculate_rating(specialist) -> None:
    """Kept as the name other views call; the implementation is shared (DRF-1855)."""
    recalculate_rating(specialist)


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

        data = serializer.validated_data
        # DRF-1855 — one implementation for the app door and the bot's
        # internal door: reviews.services.create_review.
        try:
            review = create_review(
                client=request.user,
                appointment_id=data['appointment_id'],
                rating=data['rating'],
                text=data.get('text', ''),
                is_anonymous=data.get('is_anonymous', False),
            )
        except ReviewRefused as refused:
            return error_response(
                refused.code, refused.message, status_code=refused.status_code,
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
            .select_related('client', 'service', 'salon_service')
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
            review = Review.objects.select_related(
                'service', 'salon_service',
            ).get(id=pk, client=request.user)
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
                'specialist__user', 'service', 'salon_service',
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
