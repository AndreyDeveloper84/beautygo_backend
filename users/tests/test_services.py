import logging
from datetime import timedelta

import pytest
from django.utils import timezone

from users.models import OTPCode, User
from users.services import (
    AuthService,
    InvalidOTPError,
    MaxAttemptsError,
    OTPService,
    PhoneAlreadyRegisteredError,
    RateLimitError,
    UserNotFoundError,
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
        assert otp.code == '000000'
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
        result = self.service.verify_otp(self.phone, '000000')
        assert result is True
        otp = OTPCode.objects.filter(phone=self.phone).first()
        logger.info("OTP after verify: is_used=%s", otp.is_used)
        assert otp.is_used

    def test_verify_otp_wrong_code(self, settings):
        settings.DEBUG = True
        self.service.send_otp(self.phone)
        logger.info("Verifying OTP with wrong code (should fail)")
        with pytest.raises(InvalidOTPError):
            self.service.verify_otp(self.phone, '999999')
        logger.info("InvalidOTPError raised as expected")

    def test_verify_otp_max_attempts(self, settings):
        settings.DEBUG = True
        settings.OTP_MAX_ATTEMPTS = 3
        self.service.send_otp(self.phone)
        logger.info("Testing max attempts (limit=%d)", settings.OTP_MAX_ATTEMPTS)
        for i in range(2):
            with pytest.raises(InvalidOTPError):
                self.service.verify_otp(self.phone, '999999')
            logger.info("Attempt %d: InvalidOTPError", i + 1)
        with pytest.raises(MaxAttemptsError):
            self.service.verify_otp(self.phone, '999999')
        logger.info("Attempt 3: MaxAttemptsError raised")

    def test_verify_otp_no_code_exists(self):
        logger.info("Verifying OTP for phone with no code")
        with pytest.raises(InvalidOTPError):
            self.service.verify_otp('+79009999999', '000000')
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
        tokens = self.service.verify_and_get_tokens('+79007000005', '000000')
        logger.info("Tokens received: keys=%s", list(tokens.keys()))
        assert 'access' in tokens
        assert 'refresh' in tokens
        assert tokens['user']['phone'] == '+79007000005'
        assert tokens['user']['is_verified'] is True
        # Check user is now verified in DB
        user = User.objects.get(phone='+79007000005')
        logger.info("User in DB: is_verified=%s", user.is_verified)
        assert user.is_verified
