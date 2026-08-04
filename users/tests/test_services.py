import logging
from datetime import timedelta

import pytest
from django.utils import timezone

from users.models import OTPCode, User
from users.services import (
    AuthService,
    BindTargetNotFoundError,
    IdentityBindingConflictError,
    InvalidExternalUserIDError,
    InvalidOTPError,
    MaxAttemptsError,
    OTPService,
    PhoneAlreadyRegisteredError,
    RateLimitError,
    UserNotFoundError,
    bind_external_identity,
    resolve_external_user,
)

logger = logging.getLogger(__name__)


@pytest.mark.django_db
class TestOTPService:
    def setup_method(self):
        self.service = OTPService()
        self.phone = '+79006000001'

    def test_send_otp_creates_code(self, settings):
        settings.DEBUG = True

        logger.info("Sending OTP to %s", self.phone)
        self.service.send_otp(self.phone)
        otp = OTPCode.objects.filter(phone=self.phone).first()
        logger.info("OTP created: code=%s, is_used=%s", otp.code, otp.is_used)
        assert otp is not None
        assert otp.code == '0000'
        assert not otp.is_used

    def test_send_otp_rate_limit(self, settings):
        settings.DEBUG = True

        self.service.send_otp(self.phone)
        logger.info("Sending second OTP immediately (should hit rate limit)")
        with pytest.raises(RateLimitError):
            self.service.send_otp(self.phone)
        logger.info("RateLimitError raised as expected")

    def test_send_otp_after_rate_limit_expires(self, settings):
        settings.DEBUG = True
        self.service.send_otp(self.phone)
        # Manually backdate the OTP
        otp = OTPCode.objects.get(phone=self.phone)
        otp.created_at = timezone.now() - timedelta(seconds=61)
        OTPCode.objects.filter(pk=otp.pk).update(created_at=otp.created_at)
        logger.info("Backdated OTP, sending again after rate limit window")
        # Should succeed now
        self.service.send_otp(self.phone)
        count = OTPCode.objects.filter(phone=self.phone).count()
        logger.info("OTP count after resend: %d", count)
        assert count == 2

    def test_verify_otp_correct(self, settings):
        settings.DEBUG = True

        self.service.send_otp(self.phone)
        logger.info("Verifying OTP with correct code")
        result = self.service.consume_otp(self.phone, '0000')
        assert result is True
        otp = OTPCode.objects.filter(phone=self.phone).first()
        logger.info("OTP after verify: is_used=%s", otp.is_used)
        assert otp.is_used

    def test_verify_otp_wrong_code(self, settings):
        settings.DEBUG = True

        self.service.send_otp(self.phone)
        logger.info("Verifying OTP with wrong code (should fail)")
        with pytest.raises(InvalidOTPError):
            self.service.consume_otp(self.phone, '9999')
        logger.info("InvalidOTPError raised as expected")

    def test_verify_otp_max_attempts(self, settings):

        settings.DEBUG = True
        settings.OTP_MAX_ATTEMPTS = 3
        self.service.send_otp(self.phone)
        logger.info("Testing max attempts (limit=%d)", settings.OTP_MAX_ATTEMPTS)
        for i in range(2):
            with pytest.raises(InvalidOTPError):
                self.service.consume_otp(self.phone, '9999')
            logger.info("Attempt %d: InvalidOTPError", i + 1)
        with pytest.raises(MaxAttemptsError):
            self.service.consume_otp(self.phone, '9999')
        logger.info("Attempt 3: MaxAttemptsError raised")

    def test_verify_otp_no_code_exists(self):
        logger.info("Verifying OTP for phone with no code")
        with pytest.raises(InvalidOTPError):
            self.service.consume_otp('+79009999999', '0000')
        logger.info("InvalidOTPError raised as expected")


@pytest.mark.django_db
class TestAuthService:
    def setup_method(self):
        self.service = AuthService()

    def test_register_client(self, settings):
        settings.DEBUG = True
        logger.info("Registering client +79007000001")
        user = self.service.register('+79007000001', 'client')
        logger.info("Registered: id=%s, role=%s, is_verified=%s", user.pk, user.role, user.is_verified)
        assert user.role == 'client'
        assert user.phone == '+79007000001'
        assert not user.is_verified

    def test_register_specialist(self, settings):
        settings.DEBUG = True
        logger.info("Registering specialist via app_type=pro")
        user = self.service.register('+79007000002', 'pro')
        logger.info("Registered: role=%s", user.role)
        assert user.role == 'specialist'

    def test_register_duplicate_phone(self, settings):
        settings.DEBUG = True
        self.service.register('+79007000003', 'client')
        logger.info("Attempting duplicate registration (should fail)")
        with pytest.raises(PhoneAlreadyRegisteredError):
            self.service.register('+79007000003', 'client')
        logger.info("PhoneAlreadyRegisteredError raised as expected")

    def test_login_success(self, settings):
        settings.DEBUG = True
        self.service.register('+79007000004', 'client')
        # Wait for rate limit
        otp = OTPCode.objects.filter(phone='+79007000004').first()
        OTPCode.objects.filter(pk=otp.pk).update(
            created_at=timezone.now() - timedelta(seconds=61),
        )
        logger.info("Login after rate limit window")
        self.service.login('+79007000004')
        count = OTPCode.objects.filter(phone='+79007000004').count()
        logger.info("OTP count after login: %d", count)
        assert count == 2

    def test_login_user_not_found(self):
        logger.info("Login with non-existent phone (should fail)")
        with pytest.raises(UserNotFoundError):
            self.service.login('+79009999999')
        logger.info("UserNotFoundError raised as expected")

    def test_verify_and_get_tokens(self, settings):
        settings.DEBUG = True
        logger.info("Register + verify flow for +79007000005")
        self.service.register('+79007000005', 'client')
        tokens = self.service.verify_and_get_tokens('+79007000005', '0000')
        logger.info("Tokens received: keys=%s", list(tokens.keys()))
        assert 'access_token' in tokens
        assert 'refresh_token' in tokens
        assert tokens['user']['phone'] == '+79007000005'
        assert tokens['user']['is_verified'] is True
        # Check user is now verified in DB
        user = User.objects.get(phone='+79007000005')
        logger.info("User in DB: is_verified=%s", user.is_verified)
        assert user.is_verified


@pytest.mark.django_db
class TestResolveExternalUser:
    """X-External-User-ID resolution (#1016 sign-off): the booking s2s
    surface sends a channel-scoped `bot:{channel}:{id}`, so the resolver
    must accept multi-segment ids while keeping the legacy single-segment
    `bot:{id}` (nutrition/payments) valid."""

    def test_single_segment_legacy_form(self):
        user = resolve_external_user("bot:12345")
        assert user.username == "bot:12345"
        assert user.is_proxy is True
        assert user.role == "client"

    def test_multi_segment_channel_scoped_form(self):
        """`bot:telegram:12345` (#1016 §2) resolves instead of 403-ing."""
        user = resolve_external_user("bot:telegram:12345")
        assert user.username == "bot:telegram:12345"
        assert user.is_proxy is True

    def test_idempotent_same_id_returns_same_user(self):
        a = resolve_external_user("bot:telegram:777")
        b = resolve_external_user("bot:telegram:777")
        assert a.pk == b.pk

    @pytest.mark.parametrize("bad", [
        "",            # empty
        "bot",         # no segment
        "bot:",        # trailing colon, empty segment
        "Bot:12345",   # uppercase source
        ":12345",      # missing source
        "bot::12345",  # empty middle segment
    ])
    def test_invalid_forms_rejected(self, bad):
        with pytest.raises(InvalidExternalUserIDError):
            resolve_external_user(bad)


def _real_user(username="real-customer", phone="+79997770001"):
    return User.objects.create_user(
        username=username, password="x", role="client", phone=phone,
        is_proxy=False,
    )


@pytest.mark.django_db
class TestResolveExternalUserBinding:
    """E2E-BOT-02B: resolve_external_user follows the Phase C
    ``linked_user`` binding deterministically — bound identity resolves
    the real account, unbound identity keeps its isolated proxy."""

    def test_unbound_proxy_resolves_to_isolated_proxy(self):
        user = resolve_external_user("bot:max:unbound")
        assert user.is_proxy is True
        assert user.username == "bot:max:unbound"
        assert user.linked_user_id is None

    def test_bound_proxy_resolves_to_real_account(self):
        real = _real_user()
        bind_external_identity("bot:max:bound", real.pk)
        resolved = resolve_external_user("bot:max:bound")
        assert resolved.pk == real.pk
        assert resolved.is_proxy is False

    def test_resolution_is_deterministic_across_calls(self):
        real = _real_user()
        bind_external_identity("bot:max:det", real.pk)
        first = resolve_external_user("bot:max:det")
        second = resolve_external_user("bot:max:det")
        assert first.pk == second.pk == real.pk

    def test_binding_does_not_touch_other_external_ids(self):
        real = _real_user()
        bind_external_identity("bot:max:one", real.pk)
        other = resolve_external_user("bot:max:two")
        assert other.is_proxy is True
        assert other.pk != real.pk

    def test_real_user_without_binding_resolves_normally(self):
        """A real account whose username happens to match the external-id
        shape is returned as-is (no linked_user hop for non-proxy rows)."""
        real = _real_user(username="bot:max:real")
        resolved = resolve_external_user("bot:max:real")
        assert resolved.pk == real.pk


@pytest.mark.django_db
class TestBindExternalIdentity:
    """bind_external_identity — the write half of E2E-BOT-02B."""

    def test_bind_creates_proxy_and_links(self):
        real = _real_user()
        proxy, created = bind_external_identity("bot:max:new", real.pk)
        assert created is True
        assert proxy.is_proxy is True
        assert proxy.linked_user_id == real.pk

    def test_bind_existing_unbound_proxy(self):
        pre = resolve_external_user("bot:max:pre")
        proxy, created = bind_external_identity("bot:max:pre", _real_user().pk)
        assert created is False
        assert proxy.pk == pre.pk
        assert proxy.linked_user_id is not None

    def test_rebind_same_target_is_idempotent(self):
        real = _real_user()
        bind_external_identity("bot:max:idem", real.pk)
        proxy, created = bind_external_identity("bot:max:idem", real.pk)
        assert created is False
        assert proxy.linked_user_id == real.pk

    def test_rebind_different_target_conflicts(self):
        first = _real_user(username="real-a", phone="+79997770002")
        second = _real_user(username="real-b", phone="+79997770003")
        bind_external_identity("bot:max:conflict", first.pk)
        with pytest.raises(IdentityBindingConflictError):
            bind_external_identity("bot:max:conflict", second.pk)
        # Failed rebind must not move the original binding.
        assert resolve_external_user("bot:max:conflict").pk == first.pk

    def test_target_must_be_real_account(self):
        other_proxy = resolve_external_user("bot:max:target-proxy")
        with pytest.raises(BindTargetNotFoundError):
            bind_external_identity("bot:max:to-proxy", other_proxy.pk)

    def test_target_soft_deleted_rejected(self):
        real = _real_user()
        real.is_active = False
        real.deleted_at = timezone.now()
        real.save(update_fields=["is_active", "deleted_at"])
        with pytest.raises(BindTargetNotFoundError):
            bind_external_identity("bot:max:to-deleted", real.pk)

    def test_target_unknown_id_rejected(self):
        from uuid import uuid4
        with pytest.raises(BindTargetNotFoundError):
            bind_external_identity("bot:max:to-missing", uuid4())

    def test_invalid_external_id_rejected(self):
        with pytest.raises(InvalidExternalUserIDError):
            bind_external_identity("not-an-external-id", _real_user().pk)

    def test_binding_to_soft_deleted_target_is_void(self):
        """Fail-closed: if the bound real account is later deactivated /
        soft-deleted (152-ФЗ delete flow), resolution reverts to the
        isolated proxy instead of the anonymized identity."""
        real = _real_user()
        bind_external_identity("bot:max:void", real.pk)
        real.is_active = False
        real.deleted_at = timezone.now()
        real.save(update_fields=["is_active", "deleted_at"])
        resolved = resolve_external_user("bot:max:void")
        assert resolved.is_proxy is True
        assert resolved.pk != real.pk
