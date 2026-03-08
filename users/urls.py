from django.urls import path
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)
from rest_framework.routers import DefaultRouter
from .views import (
    RegisterView, ServiceViewSet, CategoryViewSet,
    SpecialistProfileViewSet, ScheduleViewSet,
    BookingViewSet, ReviewViewSet,
)

urlpatterns = [
    path('login/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('register/', RegisterView.as_view(), name='register'),
]

router = DefaultRouter()
router.register(r'services', ServiceViewSet, basename='services')
router.register(r'categories', CategoryViewSet, basename='categories')
router.register(r'specialists', SpecialistProfileViewSet, basename='specialists')
router.register(r'schedules', ScheduleViewSet, basename='schedules')
router.register(r'bookings', BookingViewSet, basename='bookings')
router.register(r'reviews', ReviewViewSet, basename='reviews')

urlpatterns += router.urls
