from django.urls import path
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.views import TokenRefreshView as _BaseTokenRefreshView

from core.deprecation import DeprecatedAliasMixin

from .auth_serializers import TenantAwareTokenRefreshSerializer

from .personal_context_views import (
    UserPersonalContextFieldDeleteView,
    UserPersonalContextSkipView,
    UserPersonalContextView,
)
from .views import (
    AnonymousAuthView,
    BindPhoneView,
    ClientProfileView,
    CompleteProfileView,
    LoginView,
    LogoutView,
    MasterMeView,
    OnboardingView,
    RegisterPhoneView,
    SendCodeView,
    SendOTPView,
    SocialAuthView,
    UserMeView,
    VerifyOTPView,
)


# Throttle refresh-token endpoint on the same 'auth' bucket as login/verify-otp.
# SimpleJWT's TokenRefreshView otherwise inherits the default user/anon limits.
# DRF-242.8: serializer_class swap re-resolves user.tenant_id on every
# refresh so admin-driven tenant moves propagate without re-login.
class TokenRefreshView(_BaseTokenRefreshView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'
    serializer_class = TenantAwareTokenRefreshSerializer


# Legacy /api/v1/auth/masters/me/ — moved to /api/v1/specialists/me/ (DRF-209).
# Subclass keeps the canonical view's behaviour and tags every response with
# the deprecation headers so mobile clients can migrate before the sunset.
class LegacyMasterMeView(DeprecatedAliasMixin, MasterMeView):
    deprecated_path = "/auth/masters/me/"


# Legacy /auth/register/ + /auth/login/ — moved to /auth/request-otp/ +
# /auth/verify-otp/ (DRF-173 Auth v2). Kept live during the deprecation
# window so mobile clients can migrate; each call logs a warning so we
# can measure remaining traffic before removal (DRF-220).
class LegacyRegisterPhoneView(DeprecatedAliasMixin, RegisterPhoneView):
    deprecated_path = "/auth/register/"


class LegacyLoginView(DeprecatedAliasMixin, LoginView):
    deprecated_path = "/auth/login/"


urlpatterns = [
    # Auth v2 (DRF-173)
    path('anonymous/', AnonymousAuthView.as_view(), name='anonymous-auth'),
    path('onboarding/', OnboardingView.as_view(), name='onboarding'),

    # Auth endpoints (phone-based OTP).
    # /register/ + /login/ are deprecated aliases — see LegacyRegisterPhoneView
    # / LegacyLoginView above (DRF-220). Use /request-otp/ + /verify-otp/.
    path('register/', LegacyRegisterPhoneView.as_view(), name='register'),
    path('login/', LegacyLoginView.as_view(), name='login'),
    path('verify-otp/', VerifyOTPView.as_view(), name='verify-otp'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('send-otp/', SendOTPView.as_view(), name='send-otp'),
    path('request-otp/', SendOTPView.as_view(), name='request-otp'),
    path('send-code/', SendCodeView.as_view(), name='send-code'),
    path('complete-profile/', CompleteProfileView.as_view(), name='complete-profile'),

    # Current user (GET info, DELETE account)
    path('users/me/', UserMeView.as_view(), name='user-me'),

    # Social auth
    path('social/<str:provider>/', SocialAuthView.as_view(), name='social-auth'),
    path('bind-phone/', BindPhoneView.as_view(), name='bind-phone'),

    # Specialist profile (GET/POST/PATCH).
    # Canonical path is /api/v1/specialists/me/ (DRF-209); this entry is the
    # deprecated alias and will be removed after the sunset date documented
    # by DeprecatedAliasMixin.
    path('masters/me/', LegacyMasterMeView.as_view(), name='master-me-legacy'),

    # Client profile (GET/PATCH)
    path('clients/me/', ClientProfileView.as_view(), name='client-profile'),

    # DRF-230 — UserPersonalContext (M3 P0, North Star memory).
    path(
        'users/me/personal-context/skip/',
        UserPersonalContextSkipView.as_view(),
        name='personal-context-skip',
    ),
    path(
        'users/me/personal-context/<str:field>/',
        UserPersonalContextFieldDeleteView.as_view(),
        name='personal-context-field-delete',
    ),
    path(
        'users/me/personal-context/',
        UserPersonalContextView.as_view(),
        name='personal-context',
    ),
]
