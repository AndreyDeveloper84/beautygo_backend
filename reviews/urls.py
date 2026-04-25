from django.urls import path

from core.deprecation import DeprecatedAliasMixin

from .views import (
    ReviewCreateView, ReviewReplyView, ReviewUpdateView,
    SpecialistReviewsView,
)


# Legacy /api/v1/reviews/specialists/{id}/ — moved to
# /api/v1/specialists/{id}/reviews/ (DRF-208). Subclass keeps the canonical
# read-only behaviour and tags every response with deprecation headers.
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
