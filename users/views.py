"""User views: auth endpoints + profile management."""
from __future__ import annotations

import logging

from django.conf import settings
import rest_framework.parsers
from rest_framework import generics, permissions
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Profile, SpecialistProfile, User
from .permissions import IsClient, IsClientApp, IsProApp, IsSpecialist
from .response import error_response, success_response
from .serializers import (
    AnonymousAuthSerializer,
    BindPhoneSerializer,
    ClientProfileSerializer,
    DeleteAccountSerializer,
    LoginSerializer,
    LogoutSerializer,
    OnboardingSerializer,
    ProfileSerializer,
    RegisterPhoneSerializer,
    SendCodeSerializer,
    SocialAuthSerializer,
    SpecialistProfileCreateSerializer,
    SpecialistProfileDetailSerializer,
    SpecialistProfileUpdateSerializer,
    VerifyOTPSerializer,
)
from .social_auth import SocialAuthService
from .services import AuthService

logger = logging.getLogger(__name__)


# --- Auth Views (phone-based OTP) ---

class RegisterPhoneView(APIView):
    """POST /api/v1/auth/register/ — Register with phone number."""
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

    def post(self, request: Request) -> Response:
        """Register new user with phone number."""
        serializer = RegisterPhoneSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        auth_service = AuthService()
        user = auth_service.register(
            phone=serializer.validated_data['phone'],
            app_type=request.app_type,
        )
        return success_response(
            {"phone": user.phone, "message": "OTP sent"},
            status_code=201,
        )


class LoginView(APIView):
    """POST /api/v1/auth/login/ — Send OTP to phone."""
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        auth_service = AuthService()
        auth_service.login(phone=serializer.validated_data['phone'])
        return success_response({"message": "OTP sent"})


class SendOTPView(APIView):
    """POST /api/v1/auth/send-otp/ — Unified: register or login + send OTP."""
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

    def post(self, request: Request) -> Response:
        serializer = RegisterPhoneSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        phone = serializer.validated_data['phone']
        auth_service = AuthService()
        is_new_user = not User.objects.filter(phone=phone).exists()
        if is_new_user:
            auth_service.register(
                phone=phone,
                app_type=getattr(request, 'app_type', 'client'),
            )
        else:
            auth_service.login(phone=phone)

        return success_response({
            "expires_in": settings.OTP_EXPIRY_MINUTES * 60,
            "retry_after": settings.OTP_RATE_LIMIT_SECONDS,
            "is_new_user": is_new_user,
        })


class VerifyOTPView(APIView):
    """POST /api/v1/auth/verify-otp/ — Verify OTP and get JWT tokens.

    Accepts optional anonymous_token — if provided, merges anonymous session
    into the authenticated account (AI chat history, etc.).
    Returns is_new_user and onboarding_completed flags.
    """
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

    def post(self, request):
        serializer = VerifyOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        auth_service = AuthService()
        tokens = auth_service.verify_and_get_tokens(
            phone=serializer.validated_data['phone'],
            code=serializer.validated_data['code'],
            device_id=serializer.validated_data.get('device_id'),
        )

        # Merge anonymous session if token provided
        anonymous_token = serializer.validated_data.get('anonymous_token', '')
        if anonymous_token:
            _merge_anonymous_session(anonymous_token, tokens.get('user', {}).get('id'))

        return success_response(tokens)


def _merge_anonymous_session(anonymous_token: str, real_user_id) -> None:
    """
    Merge anonymous session into the real user account after OTP verification.

    Decodes the anonymous access token (from /auth/anonymous), finds the ghost
    user, migrates any related data (AI conversations), then cleans up.
    Silently ignores invalid/expired tokens.
    """
    from rest_framework_simplejwt.tokens import AccessToken

    try:
        token = AccessToken(anonymous_token)
        anon_user_id = token.get(settings.SIMPLE_JWT.get('USER_ID_CLAIM', 'user_id'))
        if not anon_user_id:
            return

        anon_user = User.objects.filter(id=anon_user_id, is_guest=True).first()
        if not anon_user:
            return

        real_user = User.objects.filter(id=real_user_id).first() if real_user_id else None

        if real_user:
            # Migrate AI conversations (stub — ai_assistant app not yet implemented)
            # Conversation.objects.filter(user=anon_user).update(user=real_user)
            logger.info(
                "Merging anonymous user %s into real user %s",
                anon_user.id, real_user.id,
            )

        # Clean up: delete anonymous session and ghost user
        try:
            anon_user.anonymous_session.delete()
        except Exception:
            pass
        anon_user.delete()

    except (TokenError, Exception) as e:
        logger.debug("Anonymous session merge failed (ignored): %s", e)


class AnonymousAuthView(APIView):
    """
    POST /api/v1/auth/anonymous/ — Issue anonymous JWT for first app launch.

    Idempotent: same device_id returns the same user's token.
    Allows browsing the catalog without registration.
    """
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

    def post(self, request: Request) -> Response:
        from datetime import timedelta
        from django.utils import timezone as tz
        from .models import AnonymousSession

        serializer = AnonymousAuthSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        device_id = serializer.validated_data['device_id']
        platform = serializer.validated_data['platform']

        # Idempotent: return existing session if valid
        session = AnonymousSession.objects.filter(device_id=device_id).first()
        if session and not session.is_expired:
            refresh = RefreshToken.for_user(session.user)
            return success_response({
                "access_token": str(refresh.access_token),
                "is_anonymous": True,
            })

        # Create or reuse guest user for this device
        if session:
            # Session expired — reuse ghost user
            anon_user = session.user
        else:
            # New device — create ghost user
            short_id = str(device_id).replace('-', '')[:12]
            anon_user = User.objects.create(
                username=f"anon_{short_id}",
                role='client',
                is_guest=True,
                is_verified=False,
                is_active=True,
            )

        # TTL: 30 days
        expires_at = tz.now() + timedelta(days=30)

        # Create or update session
        AnonymousSession.objects.update_or_create(
            device_id=device_id,
            defaults={
                'user': anon_user,
                'platform': platform,
                'expires_at': expires_at,
            },
        )

        refresh = RefreshToken.for_user(anon_user)
        return success_response({
            "access_token": str(refresh.access_token),
            "is_anonymous": True,
        }, status_code=201)


class OnboardingView(APIView):
    """
    POST /api/v1/auth/onboarding/ — Complete onboarding after OTP verification.

    Saves first_name, last_name, optional lat/lon to profile,
    marks onboarding_completed = True on the user.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request: Request) -> Response:
        serializer = OnboardingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        data = serializer.validated_data

        user.first_name = data['first_name']
        user.last_name = data.get('last_name', '')
        user.onboarding_completed = True
        user.save(update_fields=['first_name', 'last_name', 'onboarding_completed'])

        profile, _ = Profile.objects.get_or_create(user=user)
        profile.full_name = f"{data['first_name']} {data.get('last_name', '')}".strip()
        update_fields = ['full_name']

        lat = data.get('lat')
        lon = data.get('lon')
        if lat is not None:
            profile.default_location_lat = lat
            update_fields.append('default_location_lat')
        if lon is not None:
            profile.default_location_lng = lon
            update_fields.append('default_location_lng')

        profile.save(update_fields=update_fields)

        return success_response({
            "id": str(user.pk),
            "phone": user.phone,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "role": user.role,
            "onboarding_completed": True,
        })


class LogoutView(APIView):
    """POST /api/v1/auth/logout/ — Blacklist refresh token."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            token = RefreshToken(serializer.validated_data['refresh'])
            token.blacklist()
            return success_response({"message": "Logged out"})
        except TokenError:
            return error_response(
                "INVALID_TOKEN", "Token is invalid or expired",
                status_code=400,
            )


# --- Send Code View ---

class SendCodeView(APIView):
    """POST /api/v1/auth/send-code/ — Send OTP to phone (reauth)."""
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

    def post(self, request):
        serializer = SendCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        phone = serializer.validated_data['phone']
        if not User.objects.filter(phone=phone).exists():
            return error_response(
                "USER_NOT_FOUND", "User with this phone not found",
                status_code=404,
            )

        from .services import OTPService
        otp_service = OTPService()
        otp_service.send_otp(phone)
        return success_response({"message": "OTP sent"})


# --- Complete Profile View ---

class CompleteProfileView(APIView):
    """POST /api/v1/auth/complete-profile/ — Complete registration for new users."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request: Request) -> Response:
        first_name = (request.data.get('first_name') or '').strip()
        last_name = (request.data.get('last_name') or '').strip()

        if not first_name or len(first_name) < 2:
            return error_response(
                "VALIDATION_ERROR",
                "first_name is required (min 2 chars)",
                status_code=400,
            )

        user = request.user
        user.first_name = first_name
        user.last_name = last_name
        user.save(update_fields=['first_name', 'last_name'])

        profile, _ = Profile.objects.get_or_create(user=user)
        profile.full_name = f"{first_name} {last_name}".strip()
        profile.save(update_fields=['full_name'])

        return success_response({
            "id": str(user.pk),
            "phone": user.phone,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "role": user.role,
        })


# --- Specialist Profile Views ---

class MasterMeView(APIView):
    """GET/POST/PATCH /api/v1/auth/masters/me/ — Master profile management."""
    permission_classes = [permissions.IsAuthenticated, IsProApp, IsSpecialist]
    parser_classes = [
        rest_framework.parsers.MultiPartParser,
        rest_framework.parsers.JSONParser,
    ]

    def get(self, request):
        try:
            profile = SpecialistProfile.objects.get(user=request.user)
        except SpecialistProfile.DoesNotExist:
            return error_response(
                "PROFILE_NOT_FOUND",
                "Specialist profile not found",
                status_code=404,
            )
        serializer = SpecialistProfileDetailSerializer(profile)
        return success_response(serializer.data)

    def post(self, request):
        """Step 1: create specialist profile."""
        if SpecialistProfile.objects.filter(user=request.user).exists():
            return error_response(
                "PROFILE_EXISTS",
                "Specialist profile already exists, use PATCH to update",
                status_code=400,
            )
        serializer = SpecialistProfileCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(user=request.user)
        return success_response(serializer.data, status_code=201)

    def patch(self, request):
        """Step 2+: update specialist profile."""
        try:
            profile = SpecialistProfile.objects.get(user=request.user)
        except SpecialistProfile.DoesNotExist:
            return error_response(
                "PROFILE_NOT_FOUND",
                "Create profile first with POST",
                status_code=404,
            )
        serializer = SpecialistProfileUpdateSerializer(
            profile, data=request.data, partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        if (
            profile.status == SpecialistProfile.ProfileStatus.DRAFT
            and profile.display_name
            and profile.address
        ):
            profile.status = SpecialistProfile.ProfileStatus.PENDING
            profile.save(update_fields=['status'])
        detail = SpecialistProfileDetailSerializer(profile)
        return success_response(detail.data)


# --- Profile Views ---

class ProfileDetailView(generics.RetrieveAPIView):
    queryset = Profile.objects.all()
    serializer_class = ProfileSerializer
    permission_classes = [permissions.AllowAny]


class MyProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = ProfileSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        profile, created = Profile.objects.get_or_create(user=self.request.user)
        return profile


class ClientProfileView(generics.RetrieveUpdateAPIView):
    """🟢 Client only — GET/PATCH /api/v1/auth/clients/me/"""
    serializer_class = ClientProfileSerializer
    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]
    parser_classes = [
        rest_framework.parsers.MultiPartParser,
        rest_framework.parsers.JSONParser,
    ]

    def get_object(self):
        profile, _ = Profile.objects.get_or_create(user=self.request.user)
        return profile

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return success_response(serializer.data)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(
            instance, data=request.data, partial=partial,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return success_response(serializer.data)

    def partial_update(self, request, *args, **kwargs):
        kwargs['partial'] = True
        return self.update(request, *args, **kwargs)


# --- Account Deletion ---

class UserMeView(APIView):
    """GET/DELETE /api/v1/auth/users/me/ — Current user info & account deletion.

    DELETE optionally consumes an OTP (see `consume_otp` below). That path
    is the same OTP-brute-force surface BindPhoneView has, so DELETE is
    throttled on the stricter ``auth_sensitive`` bucket (5/min). GET
    (profile read) falls through to default user-rate throttling (120/min).
    """
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = 'auth_sensitive'

    def get_throttles(self):
        # Apply the sensitive scope only on DELETE — GET /users/me/ is a
        # routine profile read and the stricter cap would flag normal use.
        if self.request.method == 'DELETE':
            return [ScopedRateThrottle()]
        return super().get_throttles()

    def get(self, request):
        """Return current user basic info."""
        user = request.user
        return success_response({
            "id": str(user.id),
            "phone": user.phone,
            "role": user.role,
            "is_verified": user.is_verified,
        })

    def delete(self, request):
        serializer = DeleteAccountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Optional OTP verification before deletion
        otp_code = serializer.validated_data.get('otp_code', '')
        if otp_code and request.user.phone:
            from .services import OTPService
            OTPService().consume_otp(request.user.phone, otp_code)

        from .services import AuthService
        AuthService.delete_account(
            user=request.user,
            reason=serializer.validated_data.get("reason", ""),
        )
        return success_response(
            {"message": "Account scheduled for deletion"},
        )


# --- Social Auth ---

class SocialAuthView(APIView):
    """POST /api/v1/auth/social/{provider}/ — Social auth entry point."""
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

    def post(self, request, provider):
        serializer = SocialAuthSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        service = SocialAuthService()
        result = service.authenticate(
            provider=provider,
            token=serializer.validated_data["token"],
            app_type=getattr(request, 'app_type', 'client'),
            device_id=serializer.validated_data.get("device_id"),
            extra_fields={
                "first_name": serializer.validated_data.get(
                    "first_name", "",
                ),
                "last_name": serializer.validated_data.get(
                    "last_name", "",
                ),
            },
        )
        return success_response(result, status_code=200)


class BindPhoneView(APIView):
    """POST /api/v1/auth/bind-phone/ — Bind phone to social-auth account.

    Scoped on ``auth_sensitive`` (5/min) rather than the standard ``auth``
    bucket because this endpoint consumes an OTP — a 10/min window lets an
    attacker make 10 guesses/min against a 4-digit code in addition to the
    per-row OTP_MAX_ATTEMPTS cap. 5/min is the guardrail.
    """
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_sensitive'

    def post(self, request):
        serializer = BindPhoneSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        service = SocialAuthService()
        service.bind_phone(
            user=request.user,
            phone=serializer.validated_data["phone"],
            code=serializer.validated_data["code"],
        )
        return success_response({"message": "Phone bound successfully"})
