"""Authentication and OTP business logic."""

import logging
import random
import re
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
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

# Accepts a `<source>:<segment>[:<segment>...]` external id: a lowercase
# source followed by one OR MORE colon-separated segments. The single-
# segment form `bot:12345` (nutrition/payments s2s) stays valid; the
# multi-segment form `bot:{channel}:{id}` (booking s2s per #1016 — the
# bot's `external_user_id_for` emits `bot:telegram:12345`) now resolves
# too instead of 403-ing every booking write.
_EXTERNAL_USER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*(?::[A-Za-z0-9_-]{1,64})+$")


class InvalidExternalUserIDError(ValueError):
    """Raised when X-External-User-ID does not match the
    `<source>:<id>[:<id>...]` shape."""


def resolve_external_user(external_user_id: str) -> User:
    """Resolve `<source>:<id>[:<id>...]` to a User, lazily creating.

    Used by service-to-service endpoints (e.g. nutrition/internal/scan/,
    the #1016 booking surface) when the caller acts on behalf of an
    external identity that does not yet have an Ayla account. The created
    User has `is_proxy=True`, `role='client'`, `is_guest=False`. Phase C
    migration links proxy → real account by setting `User.linked_proxy_id`
    and migrating data.

    Format: a lowercase-alphanumeric source (e.g. `bot`, `formula`)
    followed by one or more colon-separated segments, each up to 64 chars
    of alphanumeric/underscore/hyphen. Both `bot:12345` (single segment)
    and `bot:telegram:12345` (channel-scoped, #1016) are valid. The whole
    string is stored as `username` directly (already unique on AbstractUser).
    """
    if not external_user_id or not _EXTERNAL_USER_ID_RE.match(external_user_id):
        raise InvalidExternalUserIDError(
            "external_user_id must match '<source>:<id>[:<id>...]', "
            f"got {external_user_id!r}"
        )
    user, _ = User.objects.get_or_create(
        username=external_user_id,
        defaults={"role": "client", "is_proxy": True, "is_guest": False},
    )
    return user


def get_or_create_walkin_client(name: str, phone: str | None = None) -> User:
    """Resolve (or stub-create) the client for a provider walk-in (#1017).

    A walk-in customer has no Ayla app account — the salon records them
    into its own diary so the bot's slot mirror stays accurate (a slot a
    walk-in occupies must show as busy or the bot double-books it).

    Behaviour:
    - If ``phone`` is given and an existing **proxy** ``User`` already
      has it, reuse that stub — a returning walk-in keeps one identity
      (and its booking history). The lookup is scoped to proxy accounts
      (``is_proxy=True``) on purpose: a phone that belongs to a real,
      registered (``is_proxy=False``) customer is **never** matched, so
      a master cannot silently attach a CONFIRMED walk-in to someone's
      real account without their consent (152-ФЗ). A real customer who
      walks in is recorded as a fresh proxy stub; they reconcile their
      walk-in history when they later claim the number via OTP (a
      separate Phase-C ``linked_proxy_id`` follow-up, out of scope here).
    - Otherwise create a proxy stub: ``is_proxy=True`` (Phase-C
      proxy→real linking applies the same as ``resolve_external_user``),
      ``is_guest=True`` (no credentials, cannot log in), ``role=client``.
      ``username`` is a unique ``walkin:<uuid>`` token (the source:id
      shape ``resolve_external_user`` uses), unusable password.

    The display name is stored on ``first_name`` for the provider's
    reference; the caller may also mirror it into ``Appointment.notes``.
    """
    from uuid import uuid4

    stub_phone = phone or None
    if phone:
        # Scoped to proxy stubs — a real (is_proxy=False) account that
        # happens to share the phone must NOT be reused (consent / 152-ФЗ).
        existing = User.objects.filter(phone=phone, is_proxy=True).first()
        if existing is not None:
            return existing
        # ``User.phone`` is unique. If the number is already held by some
        # account — necessarily a REAL one, since any proxy holding it was
        # reused above — we can neither copy it onto a new stub (it would
        # collide) nor co-opt the real account's number. Leave the stub's
        # phone NULL; the walk-in endpoint preserves the entered number in
        # ``Appointment.notes`` for the master, and Phase-C reconciles the
        # walk-in history if the customer later claims the number via OTP.
        # Trade-off: repeated walk-ins on a real-account number can't be
        # deduped (each lands a fresh NULL-phone stub) — acceptable for the
        # edge case; never touching the real account is the priority.
        if User.objects.filter(phone=phone).exists():
            stub_phone = None

    def _create_stub(stub_phone_value):
        return User.objects.create_user(
            username=f"walkin:{uuid4().hex}",
            password=None,
            first_name=(name or "Гость")[:150],
            phone=stub_phone_value,
            role="client",
            is_proxy=True,
            is_guest=True,
        )

    if stub_phone is None:
        # No phone to collide on — a fresh NULL-phone stub always succeeds
        # (NULLs are exempt from the unique constraint).
        return _create_stub(None)

    # Guard the unique-``phone`` create against a TOCTOU race: between the
    # checks above and the INSERT, a concurrent walk-in on the same number
    # may have created the holder. Retry once on collision — if it was a
    # parallel proxy stub we now reuse it; otherwise the number is taken by
    # a real account, so we fall back to a NULL-phone stub (never the real
    # account). Single try/except, no spin.
    try:
        with transaction.atomic():
            return _create_stub(stub_phone)
    except IntegrityError:
        existing = User.objects.filter(phone=phone, is_proxy=True).first()
        if existing is not None:
            return existing
        return _create_stub(None)


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

    # Q2 specialist mobility cascade — fires only for STAFF revokes.
    # Customer-revoke doesn't touch bookings; admin-revoke is rare
    # and the policy on tenant ownership is open (FOLLOW_UP #801).
    if tur.role == TenantUserRelationship.Role.STAFF:
        cascade_specialist_departure(
            specialist_user=target_user,
            tenant=tenant,
            actor=actor,
        )
    return tur


def cascade_specialist_departure(
    *, specialist_user, tenant, actor,
) -> dict:
    """Q2 cascade: when a specialist's TUR is revoked, cancel all of
    their active future bookings in the affected tenant with a
    no-fault full refund and notify the customers.

    Founder Q2 ack 2026-05-26:
        EXISTING bookings: stay in original tenant. NOT automatically
        transferred.
        NEW behavior when master leaves tenant:
          1. Tenant admin must assign replacement OR cancel
          2. Cancellation = no-fault refund к customer (full refund,
             no penalty)
          3. Customer gets push «Запись cancelled, salon обещает
             связаться»

    Scope of this implementation:

    - Selects bookings where ``appointment.specialist.user ==
      specialist_user`` AND ``appointment.tenant == tenant`` AND
      status in (PENDING, AWAITING_PAYMENT, CONFIRMED) AND
      ``start_datetime > now`` (past-start bookings stay as-is — the
      master either already started or no-show'd; not our state to
      change).
    - For each: calls ``CancelBookingService`` with a
      ``ForceFullRefundCancellationPolicy`` (always 100%, no time-
      window check). The standard ``booking.cancelled`` OutboxEvent
      fires with ``reason='specialist_departure'`` and
      ``refund_percent=100``.
    - For each booking with a PAID Payment: issues a YooKassa refund
      and marks the Payment status REFUNDED. Best-effort — failure
      logs an error and tags the row for manual ops follow-up; we do
      not roll back the cancellation if the refund fails (booking
      stays cancelled, refund happens manually).
    - Customer notification is routed through the standard
      ``handle_booking_cancelled`` outbox handler — that handler
      branches on ``reason='specialist_departure'`` to use the
      ``appointment_cancelled_specialist_departure`` template ('салон
      обещает связаться') instead of the generic 'appointment_cancelled'.

    Returns a summary dict for the caller's audit log:
        {
          "cancelled_count": int,
          "refunded_count": int,
          "refund_failures": [str, ...],  # booking_id strings
        }

    NOT in scope:
    - Follow-specialist transfer (Olya leaves Casa Bella → joins
      Studio Aura, customer wants to follow). Founder ack: POST-MVP.
    - Replacement-master assignment flow. Founder ack: separate
      admin action; this cascade runs only when admin chose 'cancel'
      path.
    """
    from appointments.application.dto import CancelBookingDTO
    from appointments.application.services.cancel_reschedule_service import (
        CancelBookingService,
    )
    from appointments.domain.policies import (
        ForceFullRefundCancellationPolicy,
    )
    from appointments.domain.value_objects import ACTIVE_BOOKING_STATUSES
    from appointments.models import Appointment
    from django.utils import timezone

    summary = {
        "cancelled_count": 0,
        "refunded_count": 0,
        "refund_failures": [],
    }

    if not hasattr(specialist_user, "specialist_profile"):
        # Target user wasn't a specialist at all (data drift or
        # mis-typed TUR). Nothing to cascade.
        logger.warning(
            "cascade_specialist_departure.no_profile user_id=%s",
            specialist_user.pk,
        )
        return summary

    now = timezone.now()
    active_statuses = [s.value for s in ACTIVE_BOOKING_STATUSES]

    candidates = list(
        Appointment.objects
        .filter(
            specialist__user=specialist_user,
            tenant=tenant,
            status__in=active_statuses,
            start_datetime__gt=now,
        )
        .order_by("start_datetime")
    )

    cancel_service = CancelBookingService(
        cancellation_policy=ForceFullRefundCancellationPolicy(),
    )

    for appointment in candidates:
        try:
            cancel_service.execute(CancelBookingDTO(
                booking_id=appointment.id,
                initiator_user_id=(
                    actor.pk if actor is not None else specialist_user.pk
                ),
                initiator_role="system",
                reason="specialist_departure",
            ))
            summary["cancelled_count"] += 1
        except Exception as exc:  # noqa: BLE001 — best-effort cascade
            logger.exception(
                "cascade_specialist_departure.cancel_failed "
                "booking_id=%s err=%s",
                appointment.id, exc,
            )
            continue

        # Issue refunds for any PAID payments on this booking.
        _refund_paid_payments_for(appointment, summary=summary)

    logger.info(
        "cascade_specialist_departure done user_id=%s tenant_id=%s "
        "cancelled=%d refunded=%d failed=%d",
        specialist_user.pk, tenant.pk,
        summary["cancelled_count"], summary["refunded_count"],
        len(summary["refund_failures"]),
    )
    return summary


def _refund_paid_payments_for(appointment, *, summary: dict) -> None:
    """Refund any PAID or PARTIALLY_REFUNDED Payment rows for this
    appointment via YooKassa.

    PARTIALLY_REFUNDED is included so a prior manual partial refund
    (e.g. customer used /payments/{id}/refund/ to claw back a portion
    earlier) does not leave the cascade under-refunding the remainder.
    We refund ``payment.amount - payment.refunded_amount`` and flip
    the row to REFUNDED. Founder Q2: "full refund, no penalty" — that
    means the customer ends up whole regardless of refund history.

    Split out from the cascade loop for readability and so a future
    Celery-task refund worker can plug in without touching the cancel
    loop. Failures are logged + tagged in ``summary['refund_failures']``
    so ops can do a one-shot manual refund; the booking stays
    cancelled regardless.
    """
    from decimal import Decimal

    from payments.exceptions import PaymentClientError, PaymentConfigError
    from payments.models import Payment
    from payments.services import YooKassaService

    refundable_statuses = [
        Payment.Status.PAID,
        Payment.Status.PARTIALLY_REFUNDED,
    ]
    candidates = list(
        Payment.objects.filter(
            appointment=appointment,
            status__in=refundable_statuses,
        )
    )
    # Skip rows with nothing left to refund (defensive — would be a
    # data-state anomaly otherwise).
    candidates = [
        p for p in candidates
        if (p.amount - (p.refunded_amount or Decimal("0"))) > 0
    ]
    if not candidates:
        return

    try:
        yk = YooKassaService()
    except PaymentConfigError as exc:
        logger.error(
            "cascade_specialist_departure.refund_skipped_no_provider "
            "booking_id=%s err=%s",
            appointment.id, exc,
        )
        summary["refund_failures"].append(str(appointment.id))
        return

    for payment in candidates:
        remaining = (
            payment.amount - (payment.refunded_amount or Decimal("0"))
        )
        try:
            yk.refund_payment(
                provider_payment_id=payment.provider_payment_id,
                amount=remaining,
                idempotency_key=(
                    f"departure-refund-{payment.id}"
                ),
            )
        except PaymentClientError as exc:
            logger.exception(
                "cascade_specialist_departure.refund_failed "
                "booking_id=%s payment_id=%s err=%s",
                appointment.id, payment.id, exc,
            )
            summary["refund_failures"].append(str(appointment.id))
            continue
        payment.refunded_amount = payment.amount
        payment.status = Payment.Status.REFUNDED
        payment.save(update_fields=["refunded_amount", "status"])
        summary["refunded_count"] += 1


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
