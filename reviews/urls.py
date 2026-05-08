from django.urls import path
from drf_spectacular.utils import OpenApiResponse, extend_schema

from core.deprecation import DeprecatedAliasMixin

from .serializers import ReviewListSerializer
from .views import (
    ReviewCreateView, ReviewReplyView, ReviewUpdateView,
    SpecialistReviewsView,
)


# Legacy /api/v1/reviews/specialists/{id}/ — moved to
# /api/v1/specialists/{id}/reviews/ (DRF-208). Subclass keeps the canonical
# read-only behaviour and tags every response with deprecation headers.
@extend_schema(
    deprecated=True,
    responses={
        200: ReviewListSerializer(many=True),
        404: OpenApiResponse(description="Specialist not found"),
    },
)
class LegacySpecialistReviewsView(DeprecatedAliasMixin, SpecialistReviewsView):
    pass


urlpatterns = [
    path('', ReviewCreateView.as_view(), name='review-create'),
    path('<uuid:pk>/', ReviewUpdateView.as_view(), name='review-update'),
    path('<uuid:pk>/reply/', ReviewReplyView.as_view(), name='review-reply'),
    path(
        'specialists/<uuid:specialist_id>/',
        LegacySpecialistReviewsView.as_view(),
        name='specialist-reviews-legacy',
    ),
]
