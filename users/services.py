"""Authentication and OTP business logic."""

import logging
import random
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from .models import OTPCode, User

logger = logging.getLogger(__name__)


# --- Custom Exceptions ---

class AuthError(Exception):
    """Base auth error."""
    code = "AUTH_ERROR"
    status_code = 400

    def __init__(self, message=None):
        self.message = message or self.code
        super().__init__(self.message)


class PhoneAlreadyRegisteredError(AuthError):
    code = "PHONE_ALREADY_REGISTERED"
    status_code = 400

    def __init__(self):
        super().__init__("Phone number is already registered")


class UserNotFoundError(AuthError):
    code = "USER_NOT_FOUND"
    status_code = 404

    def __init__(self):
        super().__init__("User with this phone not found")


class InvalidOTPError(AuthError):
    code = "INVALID_OTP"
    status_code = 400

    def __init__(self):
        super().__init__("Invalid OTP code")


class OTPExpiredError(AuthError):
    code = "OTP_EXPIRED"
    status_code = 400

    def __init__(self):
        super().__init__("OTP code has expired")


class MaxAttemptsError(AuthError):
    code = "MAX_ATTEMPTS_EXCEEDED"
    status_code = 429

    def __init__(self):
        super().__init__("Maximum verification attempts exceeded")


class RateLimitError(AuthError):
    code = "RATE_LIMITED"
    status_code = 429

    def __init__(self):
        super().__init__("Please wait before requesting a new code")


# --- Services ---

APP_TYPE_TO_ROLE = {
    "client": "client",
    "pro": "specialist",
}


class OTPService:
    """Handles OTP generation, sending, and verification."""

    def send_otp(self, phone: str) -> None:
        """Generate OTP and send via SMS."""
        now = timezone.now()

        # Rate limiting: check last OTP for this phone
        last_otp = OTPCode.objects.filter(phone=phone).order_by('-created_at').first()
        if last_otp:
            seconds_since = (now - last_otp.created_at).total_seconds()
            if seconds_since < settings.OTP_RATE_LIMIT_SECONDS:
                raise RateLimitError()

        # Generate code
        if settings.DEBUG and not getattr(settings, 'SMS_ENABLED', False):
            code = settings.OTP_DEBUG_CODE
        else:
            code = str(random.randint(1000, 9999))

        expires_at = now + timedelta(minutes=settings.OTP_EXPIRY_MINUTES)

        OTPCode.objects.create(
            phone=phone,
            code=code,
            expires_at=expires_at,
        )

        # Send SMS (or log in dev mode)
        from .sms import SMSService
        SMSService().send_otp(phone, code)

    def verify_otp(self, phone: str, code: str) -> bool:
        """Verify OTP code. Returns True if valid."""
        now = timezone.now()

        otp = (
            OTPCode.objects
            .filter(phone=phone, is_used=False, expires_at__gt=now)
            .order_by('-created_at')
            .first()
        )

        if not otp:
            raise InvalidOTPError()

        otp.attempts += 1

        if otp.attempts >= settings.OTP_MAX_ATTEMPTS:
            otp.save(update_fields=['attempts'])
            raise MaxAttemptsError()

        if otp.code != code:
            otp.save(update_fields=['attempts'])
            raise InvalidOTPError()

        otp.is_used = True
        otp.save(update_fields=['attempts', 'is_used'])
        return True


class AuthService:
    """Handles registration and authentication flows."""

    def __init__(self):
        self.otp_service = OTPService()

    def register(self, phone: str, app_type: str) -> User:
        """Register a new user."""
        role = APP_TYPE_TO_ROLE.get(app_type, "client")

        if User.objects.filter(phone=phone).exists():
            raise PhoneAlreadyRegisteredError()

        user = User.objects.create_user(
            username=f"user_{phone.replace('+', '')}",
            phone=phone,
            role=role,
            password=None,
        )

        self.otp_service.send_otp(phone)
        return user

    def login(self, phone: str) -> None:
        """Initiate login by sending OTP."""
        if not User.objects.filter(phone=phone).exists():
            raise UserNotFoundError()

        self.otp_service.send_otp(phone)

    def verify_and_get_tokens(
        self, phone: str, code: str, device_id: str = None,
    ) -> dict:
        """Verify OTP and return JWT tokens."""
        self.otp_service.verify_otp(phone, code)

        user = User.objects.get(phone=phone)

        is_new_user = not user.is_verified
        if is_new_user:
            user.is_verified = True
            user.save(update_fields=['is_verified'])

        refresh = RefreshToken.for_user(user)
        if device_id:
            refresh['device_id'] = device_id

        return {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "is_new_user": is_new_user,
            "user": {
                "id": user.pk,
                "phone": user.phone,
                "role": user.role,
                "is_verified": user.is_verified,
            },
        }

    @staticmethod
    def delete_account(user, reason: str = "") -> None:
        """
        Soft delete user account: anonymize PII, deactivate, schedule cleanup.

        Args:
            user: User instance to delete.
            reason: Optional reason for deletion.
        """
        from django.utils import timezone
        from rest_framework_simplejwt.token_blacklist.models import (
            OutstandingToken,
        )

        from .models import DeviceToken

        logger.info(
            "Deleting account user_id=%s reason=%s", user.pk, reason,
        )

        # 1. Anonymize PII
        user.phone = None
        user.email = ""
        user.first_name = "Удалён"
        user.last_name = ""
        user.is_active = False
        user.is_verified = False
        user.deleted_at = timezone.now()
        user.save(update_fields=[
            "phone", "email", "first_name", "last_name",
            "is_active", "is_verified", "deleted_at",
        ])

        # 2. Clear avatar from profile
        if hasattr(user, 'profile'):
            profile = user.profile
            profile.avatar = None
            profile.full_name = "Удалён"
            profile.save(update_fields=["avatar", "full_name"])

        # 3. Deactivate specialist profile
        if hasattr(user, 'specialist_profile'):
            sp = user.specialist_profile
            sp.is_available = False
            sp.display_name = "Удалён"
            sp.avatar = None
            sp.save(update_fields=[
                "is_available", "display_name", "avatar",
            ])

        # 4. Blacklist all refresh tokens
        tokens = OutstandingToken.objects.filter(user=user)
        for token in tokens:
            try:
                from rest_framework_simplejwt.token_blacklist.models import (
                    BlacklistedToken,
                )
                BlacklistedToken.objects.get_or_create(token=token)
            except Exception:
                pass

        # 5. Delete device tokens
        DeviceToken.objects.filter(user=user).delete()

        # 6. Delete social accounts
        user.social_accounts.all().delete()

        logger.info("Account deleted: user_id=%s", user.pk)
