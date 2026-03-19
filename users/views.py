"""User views: auth endpoints + service/profile management."""

import logging

from django_filters.rest_framework import FilterSet, filters
from rest_framework import generics, permissions, viewsets
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Profile, Service
from .response import error_response, success_response
from .serializers import (
    LoginSerializer,
    LogoutSerializer,
    ProfileSerializer,
    RegisterPhoneSerializer,
    ServiceSerializer,
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


# --- Permissions ---

class IsSpecialist(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == 'specialist'


# --- Service/Profile Views ---

class ServiceViewSet(viewsets.ModelViewSet):
    serializer_class = ServiceSerializer
    permission_classes = [permissions.IsAuthenticated, IsSpecialist]

    def get_queryset(self):
        return Service.objects.filter(specialist=self.request.user)

    def perform_create(self, serializer):
        serializer.save(specialist=self.request.user)


class ServiceFilter(FilterSet):
    min_price = filters.NumberFilter(field_name="price", lookup_expr='gte')
    max_price = filters.NumberFilter(field_name="price", lookup_expr='lte')
    name = filters.CharFilter(field_name="name", lookup_expr='icontains')

    class Meta:
        model = Service
        fields = ['name', 'min_price', 'max_price', 'duration_minutes', 'specialist']


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
