"""Authentication and OTP business logic."""

import logging
import random
import re
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from .models import OTPCode, User

logger = logging.getLogger(__name__)


def get_jwt_tenant_claim(user) -> str | None:
    """Determine the ``tenant_id`` JWT claim per #246 sub-phase 1.E.

    Customer (role=client) → ``None``. Per founder ack 2026-05-25 the
    customer is multi-provider by default — tenant context is action-
    scope (X-Tenant header), not a default user property. JWT carries
    no primary.

    Staff (role=specialist/admin) → primary active staff/admin TUR's
    tenant. Picked by ``-granted_at`` (most-recently-active default
    per Q3 closed). Multi-tenant staff support (specialist mobility
    Q2) gets a UI tenant switcher later; for now the most-recently
    granted staff tenant wins.

    Returns ``None`` when the user has no active staff/admin TUR —
    e.g. a brand-new specialist who hasn't been linked to a salon
    yet. ``TenantContextMiddleware`` then falls back to the X-Tenant
    header.

    String-stringified UUID to keep the JWT payload JSON-portable.
    """
    # User.role is a plain CharField with choices ('client',
    # 'specialist', 'admin') — see users/models.py:11.
    if user.role == 'client':
        return None
    from .models import TenantUserRelationship
    primary = (
        TenantUserRelationship.objects
        .filter(
            user=user,
            is_active=True,
            role__in=[
                TenantUserRelationship.Role.STAFF,
                TenantUserRelationship.Role.ADMIN,
            ],
        )
        .order_by('-granted_at')
        .first()
    )
    return str(primary.tenant_id) if primary else None


# --- Proxy user resolution (DRF-246) ---

_EXTERNAL_USER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*:[A-Za-z0-9_-]{1,64}$")


class InvalidExternalUserIDError(ValueError):
    """Raised when X-External-User-ID does not match `<source>:<id>` shape."""


def resolve_external_user(external_user_id: str) -> User:
    """Resolve `<source>:<id>` (e.g. `bot:12345`) to a User, lazily creating.

    Used by service-to-service endpoints (e.g. nutrition/internal/scan/) when
    the caller acts on behalf of an external identity that does not yet have
    an Ayla account. The created User has `is_proxy=True`, `role='client'`,
    `is_guest=False`. Phase C migration links proxy → real account by setting
    `User.linked_proxy_id` and migrating data.

    Format: `<source>:<id>` where source is lowercase alphanumeric (e.g. `bot`,
    `formula`), id is up to 64 chars of alphanumeric/underscore/hyphen.
    Stored as `username` directly (already unique on AbstractUser).
    """
    if not external_user_id or not _EXTERNAL_USER_ID_RE.match(external_user_id):
        raise InvalidExternalUserIDError(
            f"external_user_id must match '<source>:<id>', got {external_user_id!r}"
        )
    user, _ = User.objects.get_or_create(
        username=external_user_id,
        defaults={"role": "client", "is_proxy": True, "is_guest": False},
    )
    return user


# --- Tenant-relationship revoke (#246 Q1) ---


class TURNotFoundError(LookupError):
    """No active TUR for (user, tenant) pair — admin's revoke request
    targets a relationship that doesn't exist or is already revoked.

    Treated as a 404 by the view: we don't disclose which of the two
    (no such user / not in this tenant / already revoked) matched."""


def revoke_tenant_user_relationship(
    *,
    target_user,
    tenant,
    actor,
    reason: str,
    notify_customer: bool = False,
):
    """Revoke an active ``TenantUserRelationship`` row.

    Side effects (all inside one ``transaction.atomic`` block):

    1. The active TUR row flips ``is_active=False`` with
       ``revoked_at=now`` and ``revoke_reason=reason``.
    2. An OutboxEvent ``tenant.relationship.revoked`` is ALWAYS emitted
       — audit trail per founder Q1 ack 2026-05-26 ("internal audit
       event + internal event ALWAYS emitted regardless of
       notification setting").
    3. When ``notify_customer`` is True AND the revoked role is
       ``customer``, a push notification "Связь с салоном X
       прекращена" is queued. Generic message — no internal reason
       exposed; customer can ask details via Ayla follow-up.

    Raises:
        TURNotFoundError — no active TUR exists for (target_user, tenant).

    Why this lives in users.services, not the view: the cascade (Q2
    specialist mobility) will call this same primitive from inside a
    larger SpecialistDepartureService, so it must be reusable. Also:
    the admin endpoint must not be the only entry point — Django admin
    save and management commands need the same audit guarantee.
    """
    from django.db import transaction
    from django.utils import timezone

    from appointments.infrastructure.outbox import emit_outbox_event
    from notifications.services.dispatcher import NotificationService

    from .models import TenantUserRelationship

    with transaction.atomic():
        # Lock the candidate row so a parallel revoke doesn't double-
        # emit. select_for_update is a no-op on SQLite (dev/test) but
        # serialises concurrent admin actions on prod Postgres.
        try:
            tur = (
                TenantUserRelationship.objects
                .select_for_update()
                .get(
                    user=target_user,
                    tenant=tenant,
                    is_active=True,
                )
            )
        except TenantUserRelationship.DoesNotExist as exc:
            raise TURNotFoundError(
                f"No active TUR for user={target_user.pk} tenant={tenant.pk}"
            ) from exc

        tur.is_active = False
        tur.revoked_at = timezone.now()
        tur.revoke_reason = reason
        tur.save(update_fields=[
            "is_active", "revoked_at", "revoke_reason",
        ])

        # ALWAYS emit audit event — Q1 founder ack: "internal audit
        # event + internal event ALWAYS emitted regardless of
        # notification setting". Consumers dedupe by event_id.
        emit_outbox_event(
            topic="tenant.relationship.revoked",
            data={
                "user_id": str(target_user.pk),
                "tenant_id": str(tenant.pk),
                "role": tur.role,
                "revoke_reason": reason,
                "notify_customer": bool(notify_customer),
                # Actor's id is in user_id field of the envelope; this
                # nested field tells consumers who initiated. user_id
                # in the envelope refers to the TARGET (subject of the
                # event) — actor is a separate datum.
                "revoked_by_user_id": str(actor.pk) if actor else None,
            },
            actor="admin",
            user_id=target_user.pk,
            tenant_id=tenant.pk,
        )

    # Notification fires OUTSIDE the transaction so a failure in the
    # FCM queue doesn't roll back the revoke. The DB write is
    # the authoritative record; push is best-effort.
    if (
        notify_customer
        and tur.role == TenantUserRelationship.Role.CUSTOMER
    ):
        NotificationService().send(
            user=target_user,
            template_id="tenant_relationship_revoked",
            context={"tenant_name": tenant.name},
        )

    logger.info(
        "tur.revoked user_id=%s tenant_id=%s role=%s "
        "actor_id=%s notify_customer=%s",
        target_user.pk, tenant.pk, tur.role,
        getattr(actor, "pk", None), bool(notify_customer),
    )
    return tur


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

    def consume_otp(self, phone: str, code: str) -> bool:
        """Consume an OTP code (single-use).

        Not a pure check — on every call this method:
        - increments the attempts counter on the latest valid OTP,
        - marks the OTP as used when the code matches,
        - raises MaxAttemptsError once OTP_MAX_ATTEMPTS is reached.

        Each invocation mutates the OTP row. Never call this from a
        health-check, idempotent retry, or anywhere that re-runs on
        network flakes — a wrong retry burns a real OTP attempt.

        Returns True if the code is valid. Raises InvalidOTPError or
        MaxAttemptsError otherwise.
        """
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

    def complete_otp_flow(self, phone: str, code: str) -> tuple[User, bool]:
        """Consume the OTP code and activate the user.

        Two orthogonal side effects were hidden inside verify_and_get_tokens
        before: (1) burn an OTP + flip ``is_verified`` on first login, and
        (2) mint JWT tokens. They're split so that callers who need a user
        reference (session-merge, auditing, analytics) can get it without
        minting tokens as a side effect, and so that flows that should mint
        a new token pair for an already-authenticated user (refresh under
        a new device_id, elevate anonymous to real) can call ``issue_tokens``
        directly.

        Returns ``(user, is_new_user)``.
        Raises ``InvalidOTPError`` / ``MaxAttemptsError`` from the OTP layer.
        """
        self.otp_service.consume_otp(phone, code)

        user = User.objects.get(phone=phone)

        is_new_user = not user.is_verified
        if is_new_user:
            user.is_verified = True
            user.save(update_fields=['is_verified'])

        return user, is_new_user

    @staticmethod
    def issue_tokens(user: User, device_id: str | None = None) -> dict:
        """Issue a JWT access+refresh pair for ``user``.

        Pure token minting — no DB writes, no OTP consumption. Optionally
        embeds a ``device_id`` claim so refresh tokens can be scoped per
        device.

        Response-shape contract — callers may inject extra keys on top of
        the returned dict (``verify_and_get_tokens`` adds ``is_new_user``).
        When adding fields here, avoid keys that collide with those
        wrapper-owned names.
        """
        refresh = RefreshToken.for_user(user)
        if device_id:
            refresh['device_id'] = device_id
        # #246 sub-phase 1.E — role-aware JWT tenant claim:
        # - Customer (role=client): None. Multi-provider by default
        #   per founder ack 2026-05-25 — tenant context comes from
        #   the X-Tenant header per-request, not a default JWT claim.
        # - Staff (role=specialist/admin): primary active staff TUR.
        # See `get_jwt_tenant_claim`.
        refresh['tenant_id'] = get_jwt_tenant_claim(user)

        return {
            "access_token": str(refresh.access_token),
            "refresh_token": str(refresh),
            "expires_in": int(refresh.access_token.lifetime.total_seconds()),
            "onboarding_completed": user.onboarding_completed,
            "user": {
                "id": user.pk,
                "phone": user.phone,
                "role": user.role,
                "is_verified": user.is_verified,
            },
        }

    def verify_and_get_tokens(
        self, phone: str, code: str, device_id: str = None,
    ) -> dict:
        """Legacy one-call entry point. Prefer ``complete_otp_flow`` +
        ``issue_tokens`` at new call-sites; this wrapper is retained only
        so existing VerifyOTPView code keeps working.
        """
        user, is_new_user = self.complete_otp_flow(phone, code)
        payload = self.issue_tokens(user, device_id=device_id)
        payload["is_new_user"] = is_new_user
        return payload

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
