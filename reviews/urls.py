from django.urls import path

from .views import (
    ReviewCreateView, ReviewReplyView, ReviewUpdateView,
    SpecialistReviewsView,
)

urlpatterns = [
    path('', ReviewCreateView.as_view(), name='review-create'),
    path('<uuid:pk>/', ReviewUpdateView.as_view(), name='review-update'),
    path('<uuid:pk>/reply/', ReviewReplyView.as_view(), name='review-reply'),
    path(
        'specialists/<uuid:specialist_id>/',
        SpecialistReviewsView.as_view(), name='specialist-reviews',
    ),
]
