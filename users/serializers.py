import re

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Profile, SpecialistProfile, User


# --- Phone validation mixin ---

class PhoneSerializer(serializers.Serializer):
    """Validates Russian phone number format."""
    phone = serializers.CharField(max_length=20)

    def validate_phone(self, value):
        cleaned = re.sub(r'[\s\-\(\)]', '', value)
        pattern = r'^(\+7|8)\d{10}$'
        if not re.match(pattern, cleaned):
            raise serializers.ValidationError("Invalid phone format. Use +7XXXXXXXXXX")
        if cleaned.startswith('8'):
            cleaned = '+7' + cleaned[1:]
        return cleaned


# --- Auth serializers ---

class RegisterPhoneSerializer(PhoneSerializer):
    """Registration: phone only. Role determined by X-App-Type header."""
    pass


class LoginSerializer(PhoneSerializer):
    """Login (send OTP): phone only."""
    pass


class VerifyOTPSerializer(PhoneSerializer):
    """Verify OTP: phone + code + optional device_id + optional anonymous_token for merge."""
    code = serializers.CharField(max_length=6, min_length=4)
    device_id = serializers.CharField(max_length=255, required=False)
    anonymous_token = serializers.CharField(required=False, allow_blank=True)

    def validate_code(self, value):
        if not value.isdigit():
            raise serializers.ValidationError("Code must be numeric")
        return value


class SendCodeSerializer(PhoneSerializer):
    """Send OTP code to phone."""
    PURPOSE_CHOICES = [('verify', 'Verify'), ('login', 'Login')]
    purpose = serializers.ChoiceField(choices=PURPOSE_CHOICES, default='login')


class LogoutSerializer(serializers.Serializer):
    """Logout: refresh token to blacklist."""
    refresh = serializers.CharField()


class UserShortSerializer(serializers.ModelSerializer):
    """Minimal user info returned after auth."""
    class Meta:
        model = User
        fields = ['id', 'phone', 'role', 'is_verified']
        read_only_fields = fields


# --- Profile serializers ---

class ProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Profile
        fields = ['id', 'full_name', 'avatar', 'bio', 'city', 'experience_years']


ALLOWED_AVATAR_TYPES = ('image/jpeg', 'image/png', 'image/webp')
MAX_AVATAR_SIZE = 5 * 1024 * 1024  # 5 MB


class ClientProfileSerializer(serializers.ModelSerializer):
    """Serializer for client profile with avatar and location validation."""

    class Meta:
        model = Profile
        fields = [
            'id', 'full_name', 'avatar', 'city',
            'default_location_lat', 'default_location_lng',
        ]

    def validate_full_name(self, value):
        if len(value.strip()) < 2:
            raise serializers.ValidationError(
                "Имя должно содержать минимум 2 символа"
            )
        return value.strip()

    def validate_avatar(self, value):
        if value.content_type not in ALLOWED_AVATAR_TYPES:
            raise serializers.ValidationError(
                "Допустимые форматы: JPEG, PNG, WebP"
            )
        if value.size > MAX_AVATAR_SIZE:
            raise serializers.ValidationError(
                "Размер файла не должен превышать 5 МБ"
            )
        return value


# --- Social Auth serializers ---

class SocialAuthSerializer(serializers.Serializer):
    """Validate social auth request."""
    token = serializers.CharField(
        help_text="OAuth access_token or id_token from provider",
    )
    device_id = serializers.CharField(max_length=255, required=False)
    first_name = serializers.CharField(
        max_length=150, required=False, default="",
    )
    last_name = serializers.CharField(
        max_length=150, required=False, default="",
    )


class BindPhoneSerializer(PhoneSerializer):
    """Bind a phone number to a social-auth user via OTP."""
    code = serializers.CharField(max_length=6, min_length=4)

    def validate_code(self, value):
        if not value.isdigit():
            raise serializers.ValidationError("Code must be numeric")
        return value


# --- Specialist Profile serializers ---

class SpecialistProfileCreateSerializer(serializers.ModelSerializer):
    """Step 1: create profile (display_name, avatar, bio)."""

    class Meta:
        model = SpecialistProfile
        fields = ['display_name', 'avatar', 'bio', 'experience_years']

    def validate_display_name(self, value):
        if len(value.strip()) < 2:
            raise serializers.ValidationError(
                "Имя должно содержать минимум 2 символа"
            )
        return value.strip()

    def validate_avatar(self, value):
        if value.content_type not in ALLOWED_AVATAR_TYPES:
            raise serializers.ValidationError(
                "Допустимые форматы: JPEG, PNG, WebP"
            )
        if value.size > MAX_AVATAR_SIZE:
            raise serializers.ValidationError(
                "Размер файла не должен превышать 5 МБ"
            )
        return value


class SpecialistProfileUpdateSerializer(serializers.ModelSerializer):
    """Step 2+: update profile (address, location, bio, etc)."""

    class Meta:
        model = SpecialistProfile
        fields = [
            'display_name', 'avatar', 'bio', 'experience_years',
            'address', 'location_lat', 'location_lng',
            'is_available',
        ]

    def validate_display_name(self, value):
        if len(value.strip()) < 2:
            raise serializers.ValidationError(
                "Имя должно содержать минимум 2 символа"
            )
        return value.strip()

    def validate_avatar(self, value):
        if value.content_type not in ALLOWED_AVATAR_TYPES:
            raise serializers.ValidationError(
                "Допустимые форматы: JPEG, PNG, WebP"
            )
        if value.size > MAX_AVATAR_SIZE:
            raise serializers.ValidationError(
                "Размер файла не должен превышать 5 МБ"
            )
        return value


class SpecialistProfileDetailSerializer(serializers.ModelSerializer):
    """Full profile for GET /masters/me."""
    services_count = serializers.SerializerMethodField()

    class Meta:
        model = SpecialistProfile
        fields = [
            'id', 'display_name', 'avatar', 'bio', 'experience_years',
            'address', 'location_lat', 'location_lng',
            'status', 'rating', 'reviews_count', 'is_available',
            'services_count', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'status', 'rating', 'reviews_count',
            'created_at', 'updated_at',
        ]

    @extend_schema_field(serializers.IntegerField())
    def get_services_count(self, obj):
        return obj.services.count()


# --- Auth v2 serializers (DRF-173) ---

class AnonymousAuthSerializer(serializers.Serializer):
    """POST /auth/anonymous — create/retrieve anonymous session."""
    device_id = serializers.UUIDField()
    platform = serializers.ChoiceField(choices=['ios', 'android'])


class OnboardingSerializer(serializers.Serializer):
    """POST /auth/onboarding — save name + geo, complete onboarding."""
    first_name = serializers.CharField(min_length=2, max_length=150)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default='')
    lat = serializers.DecimalField(
        max_digits=9, decimal_places=6, required=False, allow_null=True,
    )
    lon = serializers.DecimalField(
        max_digits=9, decimal_places=6, required=False, allow_null=True,
    )

    def validate_first_name(self, value):
        return value.strip()


class DeleteAccountSerializer(serializers.Serializer):
    """DELETE /users/me — soft delete account. otp_code optional for extra verification."""
    reason = serializers.CharField(required=False, allow_blank=True, default='')
    otp_code = serializers.CharField(
        max_length=6, min_length=4, required=False, allow_blank=True,
    )

    def validate_otp_code(self, value):
        if value and not value.isdigit():
            raise serializers.ValidationError("OTP code must be numeric.")
        return value
