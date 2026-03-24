from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    BindPhoneView,
    ClientProfileView,
    LoginView,
    LogoutView,
    MasterMeView,
    RegisterPhoneView,
    SendCodeView,
    SocialAuthView,
    UserMeView,
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

    # Current user (GET info, DELETE account)
    path('users/me/', UserMeView.as_view(), name='user-me'),

    # Social auth
    path('social/<str:provider>/', SocialAuthView.as_view(), name='social-auth'),
    path('bind-phone/', BindPhoneView.as_view(), name='bind-phone'),

    # Specialist profile (GET/POST/PATCH)
    path('masters/me/', MasterMeView.as_view(), name='master-me'),

    # Client profile (GET/PATCH)
    path('clients/me/', ClientProfileView.as_view(), name='client-profile'),
]
