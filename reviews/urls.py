from django.urls import path

from .views import ReviewCreateView, SpecialistReviewsView

urlpatterns = [
    path('', ReviewCreateView.as_view(), name='review-create'),
    path('specialists/<uuid:specialist_id>/', SpecialistReviewsView.as_view(), name='specialist-reviews'),
]
