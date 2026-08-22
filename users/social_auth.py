"""Social authentication service for OAuth providers."""

import logging
from dataclasses import dataclass, field

import jwt
import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework_simplejwt.tokens import RefreshToken

from .models import SocialAccount, User

logger = logging.getLogger(__name__)

APP_TYPE_TO_ROLE = {
    "client": "client",
    "pro": "specialist",
}


# --- Exceptions ---

class SocialAuthError(Exception):
    """Base exception for social auth."""
    code = "SOCIAL_AUTH_ERROR"
    status_code = 400

    def __init__(self, message="Social auth error"):
        self.message = message
        super().__init__(message)


class InvalidProviderError(SocialAuthError):
    code = "INVALID_PROVIDER"
    status_code = 400

    def __init__(self):
        super().__init__("Unknown social auth provider")


class SocialAuthTokenError(SocialAuthError):
    code = "SOCIAL_TOKEN_INVALID"
    status_code = 401

    def __init__(self, message="Invalid or expired provider token"):
        super().__init__(message)


class PhoneAlreadyBoundError(SocialAuthError):
    code = "PHONE_ALREADY_BOUND"
    status_code = 400

    def __init__(self):
        super().__init__("Phone number already bound to another account")


class SocialProviderDisabledError(SocialAuthError):
    """Provider is administratively disabled (AY-01 containment gate)."""
    code = "SOCIAL_PROVIDER_DISABLED"
    status_code = 403

    def __init__(self, provider: str):
        super().__init__(
            f"Authentication via '{provider}' is temporarily disabled",
        )


# --- Provider data ---

@dataclass
class SocialUserInfo:
    """Normalized user info from any social provider."""
    provider: str
    provider_uid: str
    email: str | None = None
    first_name: str = ""
    last_name: str = ""
    phone: str | None = None
    extra_data: dict = field(default_factory=dict)


# --- Provider verifiers ---

def verify_vk_token(token: str) -> SocialUserInfo:
    """Verify VK access token and fetch user profile."""
    try:
        response = requests.get(
            "https://api.vk.com/method/users.get",
            params={
                "access_token": token,
                "fields": "first_name,last_name,photo_200,contacts",
                "v": "5.199",
            },
            timeout=10,
        )
        data = response.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("VK API error: %s", e)
        raise SocialAuthTokenError("VK API unavailable")

    if "error" in data:
        logger.warning("VK token invalid: %s", data["error"])
        raise SocialAuthTokenError()

    user = data["response"][0]
    return SocialUserInfo(
        provider="vk",
        provider_uid=str(user["id"]),
        email=None,
        first_name=user.get("first_name", ""),
        last_name=user.get("last_name", ""),
        phone=user.get("mobile_phone") or None,
        extra_data=user,
    )


def verify_google_token(token: str) -> SocialUserInfo:
    """Verify Google ID token via tokeninfo endpoint."""
    try:
        response = requests.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": token},
            timeout=10,
        )
    except requests.RequestException as e:
        logger.warning("Google API error: %s", e)
        raise SocialAuthTokenError("Google API unavailable")

    if response.status_code != 200:
        raise SocialAuthTokenError()

    data = response.json()

    if settings.GOOGLE_CLIENT_ID and data.get("aud") != settings.GOOGLE_CLIENT_ID:
        logger.warning("Google aud mismatch: %s", data.get("aud"))
        raise SocialAuthTokenError("Invalid audience")

    return SocialUserInfo(
        provider="google",
        provider_uid=data["sub"],
        email=data.get("email"),
        first_name=data.get("given_name", ""),
        last_name=data.get("family_name", ""),
        phone=None,
        extra_data=data,
    )


def verify_apple_token(token: str) -> SocialUserInfo:
    """Verify Apple identity token (JWT) using Apple's public keys."""
    try:
        keys_response = requests.get(
            "https://appleid.apple.com/auth/keys", timeout=10,
        )
        apple_keys = keys_response.json()["keys"]
    except (requests.RequestException, KeyError, ValueError) as e:
        logger.warning("Apple keys fetch error: %s", e)
        raise SocialAuthTokenError("Apple API unavailable")

    try:
        header = jwt.get_unverified_header(token)
        key_data = next(
            (k for k in apple_keys if k["kid"] == header.get("kid")),
            None,
        )
        if not key_data:
            raise SocialAuthTokenError("Apple key not found")

        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)

        decode_options = {
            "verify_aud": bool(settings.APPLE_CLIENT_ID),
        }
        payload = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=settings.APPLE_CLIENT_ID or None,
            issuer="https://appleid.apple.com",
            options=decode_options,
        )
    except jwt.PyJWTError as e:
        logger.warning("Apple JWT decode error: %s", e)
        raise SocialAuthTokenError()

    return SocialUserInfo(
        provider="apple",
        provider_uid=payload["sub"],
        email=payload.get("email"),
        first_name="",
        last_name="",
        phone=None,
        extra_data=payload,
    )


def verify_yandex_token(token: str) -> SocialUserInfo:
    """Verify Yandex OAuth token and fetch user info."""
    try:
        response = requests.get(
            "https://login.yandex.ru/info",
            params={"format": "json"},
            headers={"Authorization": f"OAuth {token}"},
            timeout=10,
        )
    except requests.RequestException as e:
        logger.warning("Yandex API error: %s", e)
        raise SocialAuthTokenError("Yandex API unavailable")

    if response.status_code != 200:
        raise SocialAuthTokenError()

    data = response.json()
    phone = None
    if data.get("default_phone", {}).get("number"):
        phone = data["default_phone"]["number"]

    return SocialUserInfo(
        provider="yandex",
        provider_uid=data["id"],
        email=data.get("default_email"),
        first_name=data.get("first_name", ""),
        last_name=data.get("last_name", ""),
        phone=phone,
        extra_data=data,
    )


# --- Main service ---

PROVIDER_VERIFIERS = {
    "vk": verify_vk_token,
    "google": verify_google_token,
    "apple": verify_apple_token,
    "yandex": verify_yandex_token,
}


class SocialAuthService:
    """Orchestrates social authentication flow."""

    def authenticate(
        self,
        provider: str,
        token: str,
        app_type: str,
        device_id: str | None = None,
        extra_fields: dict | None = None,
    ) -> dict:
        """
        Authenticate via social provider.

        Returns dict with JWT tokens, user info, is_new_user flag.
        """
        # 1. Validate provider
        verifier = PROVIDER_VERIFIERS.get(provider)
        if not verifier:
            raise InvalidProviderError()

        # 2. AY-01 containment (W0-D1): disabled providers are rejected
        #    fail-closed BEFORE the verifier call, any SocialAccount
        #    lookup, email/phone matching, or user creation. The token
        #    value is never logged or echoed here.
        disabled = {
            str(p).strip().lower()
            for p in getattr(
                settings, "SOCIAL_AUTH_DISABLED_PROVIDERS", ("vk", "yandex"),
            )
        }
        if provider.strip().lower() in disabled:
            logger.warning(
                "Rejected disabled social provider '%s'", provider,
            )
            raise SocialProviderDisabledError(provider)

        # 3. Verify token with provider
        info = verifier(token)

        # 4. Apple: overlay name from extra_fields
        if extra_fields and provider == "apple":
            if extra_fields.get("first_name"):
                info.first_name = extra_fields["first_name"]
            if extra_fields.get("last_name"):
                info.last_name = extra_fields["last_name"]

        # 5. Find or create user
        user, is_new = self._find_or_create_user(info, app_type)

        # 6. Update extra_data
        SocialAccount.objects.filter(
            provider=provider, provider_uid=info.provider_uid,
        ).update(extra_data=info.extra_data)

        # 7. Generate tokens
        return self._build_response(user, is_new, device_id)

    def bind_phone(self, user: User, phone: str, code: str) -> None:
        """Bind phone number to a social-auth user."""
        from .services import OTPService

        # Check user doesn't already have a phone
        if user.phone:
            raise PhoneAlreadyBoundError()

        # Check phone not taken
        if User.objects.filter(phone=phone).exists():
            raise PhoneAlreadyBoundError()

        # Verify OTP
        otp_service = OTPService()
        if not otp_service.consume_otp(phone, code):
            from .services import InvalidOTPError
            raise InvalidOTPError()

        # Bind phone
        user.phone = phone
        user.is_verified = True
        user.save(update_fields=["phone", "is_verified"])

        logger.info("Phone %s bound to user %s", phone, user.pk)

    def _find_or_create_user(
        self, info: SocialUserInfo, app_type: str,
    ) -> tuple[User, bool]:
        """Find existing user or create new one."""
        with transaction.atomic():
            # Try existing SocialAccount
            try:
                social = SocialAccount.objects.select_related('user').get(
                    provider=info.provider,
                    provider_uid=info.provider_uid,
                )
                return social.user, False
            except SocialAccount.DoesNotExist:
                pass

            # Try match by email
            if info.email:
                try:
                    user = User.objects.get(email=info.email)
                    self._link_social_account(user, info)
                    return user, False
                except User.DoesNotExist:
                    pass

            # Try match by phone
            if info.phone:
                try:
                    user = User.objects.get(phone=info.phone)
                    self._link_social_account(user, info)
                    return user, False
                except User.DoesNotExist:
                    pass

            # Create new user
            return self._create_user(info, app_type), True

    def _create_user(self, info: SocialUserInfo, app_type: str) -> User:
        """Create a new user from social provider info."""
        role = APP_TYPE_TO_ROLE.get(app_type, "client")
        username = f"social_{info.provider}_{info.provider_uid}"

        try:
            user = User.objects.create(
                username=username,
                phone=info.phone,
                email=info.email or "",
                first_name=info.first_name,
                last_name=info.last_name,
                role=role,
                is_verified=bool(info.phone),
            )
        except IntegrityError:
            # Race condition: user was just created
            social = SocialAccount.objects.select_related('user').get(
                provider=info.provider,
                provider_uid=info.provider_uid,
            )
            return social.user

        self._link_social_account(user, info)
        logger.info(
            "Created social user %s via %s", user.pk, info.provider,
        )
        return user

    def _link_social_account(
        self, user: User, info: SocialUserInfo,
    ) -> SocialAccount:
        """Link a social provider to an existing user."""
        try:
            return SocialAccount.objects.create(
                user=user,
                provider=info.provider,
                provider_uid=info.provider_uid,
                extra_data=info.extra_data,
            )
        except IntegrityError:
            # Already linked (race condition)
            return SocialAccount.objects.get(
                provider=info.provider,
                provider_uid=info.provider_uid,
            )

    def _build_response(
        self, user: User, is_new: bool, device_id: str | None,
    ) -> dict:
        """Build JWT tokens response."""
        refresh = RefreshToken.for_user(user)
        if device_id:
            refresh["device_id"] = device_id

        return {
            "access_token": str(refresh.access_token),
            "refresh_token": str(refresh),
            "expires_in": int(refresh.access_token.lifetime.total_seconds()),
            "user": {
                "id": user.pk,
                "phone": user.phone,
                "role": user.role,
                "is_verified": user.is_verified,
            },
            "is_new_user": is_new,
            "onboarding_completed": user.onboarding_completed,
            "phone_required": not bool(user.phone),
        }
