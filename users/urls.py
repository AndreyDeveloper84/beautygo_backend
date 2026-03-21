from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    ClientProfileView,
    LoginView,
    LogoutView,
    MasterMeView,
    MasterProfileView,
    MyProfileView,
    ProfileDetailView,
    RegisterPhoneView,
    SendCodeView,
    VerifyOTPView,
)

urlpatterns = [
    # Auth endpoints (phone-based OTP)
    path('register/', RegisterPhoneView.as_view(), name='register'),
    path('login/', LoginView.as_view(), name='login'),
    path('verify-otp/', VerifyOTPView.as_view(), name='verify-otp'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('send-code/', SendCodeView.as_view(), name='send-code'),

    # Specialist profile endpoints
    path('masters/profile/', MasterProfileView.as_view(), name='master-profile'),
    path('masters/me/', MasterMeView.as_view(), name='master-me'),

    # Profile endpoints
    path('profile/<int:pk>/', ProfileDetailView.as_view(), name='profile-detail'),
    path('profile/me/', MyProfileView.as_view(), name='my-profile'),
    path('clients/me/', ClientProfileView.as_view(), name='client-profile'),
]
