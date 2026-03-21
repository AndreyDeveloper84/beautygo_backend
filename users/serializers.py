import re

from rest_framework import serializers

from .models import Profile, User


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
    """Verify OTP: phone + code + optional device_id."""
    code = serializers.CharField(max_length=6, min_length=4)
    device_id = serializers.CharField(max_length=255, required=False)

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


# --- Legacy serializer (kept for compatibility) ---

class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ('username', 'password', 'email', 'phone', 'role')

    def create(self, validated_data):
        user = User.objects.create_user(
            username=validated_data['username'],
            email=validated_data.get('email'),
            phone=validated_data.get('phone'),
            role=validated_data.get('role'),
            password=validated_data['password']
        )
        return user


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
