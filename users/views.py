"""User views: auth endpoints + profile management."""

import logging

import rest_framework.parsers
from rest_framework import generics, permissions
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Profile, User
from .permissions import IsClient, IsClientApp
from .response import error_response, success_response
from .serializers import (
    ClientProfileSerializer,
    LoginSerializer,
    LogoutSerializer,
    ProfileSerializer,
    RegisterPhoneSerializer,
    SendCodeSerializer,
    VerifyOTPSerializer,
)
from .services import AuthError, AuthService

logger = logging.getLogger(__name__)


# --- Auth Views (phone-based OTP) ---

class RegisterPhoneView(APIView):
    """POST /api/v1/auth/register/ — Register with phone number."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
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

    def post(self, request):
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
