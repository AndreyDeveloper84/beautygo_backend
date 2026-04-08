"""User views: auth endpoints + profile management."""
from __future__ import annotations

import logging

from django.conf import settings
import rest_framework.parsers
from rest_framework import generics, permissions
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Profile, SpecialistProfile, User
from .permissions import IsClient, IsClientApp, IsProApp, IsSpecialist
from .response import error_response, success_response
from .serializers import (
    BindPhoneSerializer,
    ClientProfileSerializer,
    DeleteAccountSerializer,
    LoginSerializer,
    LogoutSerializer,
    ProfileSerializer,
    RegisterPhoneSerializer,
    SendCodeSerializer,
    SocialAuthSerializer,
    SpecialistProfileCreateSerializer,
    SpecialistProfileDetailSerializer,
    SpecialistProfileUpdateSerializer,
    VerifyOTPSerializer,
)
from .social_auth import (
    SocialAuthError,
    SocialAuthService,
)
from .services import AuthError, AuthService

logger = logging.getLogger(__name__)


# --- Auth Views (phone-based OTP) ---

class RegisterPhoneView(APIView):
    """POST /api/v1/auth/register/ — Register with phone number."""
    permission_classes = [permissions.AllowAny]

    def post(self, request: Request) -> Response:
        """Register new user with phone number."""
        serializer = RegisterPhoneSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid input",
                details=serializer.errors, status_code=400,
            )

        try:
            auth_service = AuthService()
            user = auth_service.register(
                phone=serializer.validated_data['phone'],
                app_type=request.app_type,
            )
            return success_response(
                {"phone": user.phone, "message": "OTP sent"},
                status_code=201,
            )
        except AuthError as e:
            return error_response(e.code, str(e), status_code=e.status_code)


class LoginView(APIView):
    """POST /api/v1/auth/login/ — Send OTP to phone."""
    permission_classes = [permissions.AllowAny]

    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid input",
                details=serializer.errors, status_code=400,
            )

        try:
            auth_service = AuthService()
            auth_service.login(phone=serializer.validated_data['phone'])
            return success_response({"message": "OTP sent"})
        except AuthError as e:
            return error_response(e.code, str(e), status_code=e.status_code)


class SendOTPView(APIView):
    """POST /api/v1/auth/send-otp/ — Unified: register or login + send OTP."""
    permission_classes = [permissions.AllowAny]

    def post(self, request: Request) -> Response:
        serializer = RegisterPhoneSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid input",
                details=serializer.errors, status_code=400,
            )

        phone = serializer.validated_data['phone']
        auth_service = AuthService()
        is_new_user = not User.objects.filter(phone=phone).exists()
        try:
            if is_new_user:
                auth_service.register(
                    phone=phone,
                    app_type=getattr(request, 'app_type', 'client'),
                )
            else:
                auth_service.login(phone=phone)
        except AuthError as e:
            return error_response(e.code, str(e), status_code=e.status_code)

        return success_response({
            "expires_in": settings.OTP_EXPIRY_MINUTES * 60,
            "retry_after": settings.OTP_RATE_LIMIT_SECONDS,
            "is_new_user": is_new_user,
        })


class VerifyOTPView(APIView):
    """POST /api/v1/auth/verify-otp/ — Verify OTP and get JWT tokens."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = VerifyOTPSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid input",
                details=serializer.errors, status_code=400,
            )

        try:
            auth_service = AuthService()
            tokens = auth_service.verify_and_get_tokens(
                phone=serializer.validated_data['phone'],
                code=serializer.validated_data['code'],
                device_id=serializer.validated_data.get('device_id'),
            )
            return success_response(tokens)
        except AuthError as e:
            return error_response(e.code, str(e), status_code=e.status_code)


class LogoutView(APIView):
    """POST /api/v1/auth/logout/ — Blacklist refresh token."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid input",
                details=serializer.errors, status_code=400,
            )

        try:
            token = RefreshToken(serializer.validated_data['refresh'])
            token.blacklist()
            return success_response({"message": "Logged out"})
        except TokenError:
            return error_response(
                "INVALID_TOKEN", "Token is invalid or expired",
                status_code=400,
            )


# --- Legacy View (kept for compatibility) ---

class RegisterView(APIView):
    """Legacy username/password registration."""
    def post(self, request):
        from .serializers import RegisterSerializer
        serializer = RegisterSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return success_response(
                {"message": "Регистрация прошла успешно"},
                status_code=201,
            )
        return error_response(
            "VALIDATION_ERROR", "Invalid input",
            details=serializer.errors, status_code=400,
        )


# --- Send Code View ---

class SendCodeView(APIView):
    """POST /api/v1/auth/send-code/ — Send OTP to phone (reauth)."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = SendCodeSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid input",
                details=serializer.errors, status_code=400,
            )

        phone = serializer.validated_data['phone']
        if not User.objects.filter(phone=phone).exists():
            return error_response(
                "USER_NOT_FOUND", "User with this phone not found",
                status_code=404,
            )

        try:
            from .services import OTPService
            otp_service = OTPService()
            otp_service.send_otp(phone)
            return success_response({"message": "OTP sent"})
        except AuthError as e:
            return error_response(e.code, str(e), status_code=e.status_code)


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
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid input",
                details=serializer.errors, status_code=400,
            )
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
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid input",
                details=serializer.errors, status_code=400,
            )
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
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid input",
                details=serializer.errors, status_code=400,
            )
        serializer.save()
        return success_response(serializer.data)

    def partial_update(self, request, *args, **kwargs):
        kwargs['partial'] = True
        return self.update(request, *args, **kwargs)


# --- Account Deletion ---

class UserMeView(APIView):
    """GET/DELETE /api/v1/auth/users/me/ — Current user info & account deletion."""
    permission_classes = [permissions.IsAuthenticated]

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
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid input",
                details=serializer.errors, status_code=400,
            )

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

    def post(self, request, provider):
        serializer = SocialAuthSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid input",
                details=serializer.errors, status_code=400,
            )

        try:
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
        except SocialAuthError as e:
            return error_response(
                e.code, e.message, status_code=e.status_code,
            )


class BindPhoneView(APIView):
    """POST /api/v1/auth/bind-phone/ — Bind phone to social-auth account."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = BindPhoneSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid input",
                details=serializer.errors, status_code=400,
            )

        try:
            service = SocialAuthService()
            service.bind_phone(
                user=request.user,
                phone=serializer.validated_data["phone"],
                code=serializer.validated_data["code"],
            )
            return success_response({"message": "Phone bound successfully"})
        except SocialAuthError as e:
            return error_response(
                e.code, e.message, status_code=e.status_code,
            )
