from django.urls import path
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)
from rest_framework.routers import DefaultRouter
from .views import RegisterView, ServiceViewSet, ProfileDetailView, MyProfileView

urlpatterns = [
    path('login/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('register/', RegisterView.as_view(), name='register'),
]

router = DefaultRouter()
router.register(r'services', ServiceViewSet, basename='services')

urlpatterns += router.urls

urlpatterns += [
    path('profile/<int:pk>/', ProfileDetailView.as_view(), name='profile-detail'),
    path('profile/me/', MyProfileView.as_view(), name='my-profile'),
]
