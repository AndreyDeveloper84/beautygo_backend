import pytest
from datetime import timedelta

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


@pytest.mark.django_db
class TestOTPService:
    def setup_method(self):
        self.service = OTPService()
        self.phone = '+79006000001'

    def test_send_otp_creates_code(self, settings):
        settings.DEBUG = True
        self.service.send_otp(self.phone)
        otp = OTPCode.objects.filter(phone=self.phone).first()
        assert otp is not None
        assert otp.code == '000000'
        assert not otp.is_used

    def test_send_otp_rate_limit(self, settings):
        settings.DEBUG = True
        self.service.send_otp(self.phone)
        with pytest.raises(RateLimitError):
            self.service.send_otp(self.phone)

    def test_send_otp_after_rate_limit_expires(self, settings):
        settings.DEBUG = True
        self.service.send_otp(self.phone)
        # Manually backdate the OTP
        otp = OTPCode.objects.get(phone=self.phone)
        otp.created_at = timezone.now() - timedelta(seconds=61)
        OTPCode.objects.filter(pk=otp.pk).update(created_at=otp.created_at)
        # Should succeed now
        self.service.send_otp(self.phone)
        assert OTPCode.objects.filter(phone=self.phone).count() == 2

    def test_verify_otp_correct(self, settings):
        settings.DEBUG = True
        self.service.send_otp(self.phone)
        result = self.service.verify_otp(self.phone, '000000')
        assert result is True
        otp = OTPCode.objects.filter(phone=self.phone).first()
        assert otp.is_used

    def test_verify_otp_wrong_code(self, settings):
        settings.DEBUG = True
        self.service.send_otp(self.phone)
        with pytest.raises(InvalidOTPError):
            self.service.verify_otp(self.phone, '999999')

    def test_verify_otp_max_attempts(self, settings):
        settings.DEBUG = True
        settings.OTP_MAX_ATTEMPTS = 3
        self.service.send_otp(self.phone)
        for _ in range(2):
            with pytest.raises(InvalidOTPError):
                self.service.verify_otp(self.phone, '999999')
        with pytest.raises(MaxAttemptsError):
            self.service.verify_otp(self.phone, '999999')

    def test_verify_otp_no_code_exists(self):
        with pytest.raises(InvalidOTPError):
            self.service.verify_otp('+79009999999', '000000')


@pytest.mark.django_db
class TestAuthService:
    def setup_method(self):
        self.service = AuthService()

    def test_register_client(self, settings):
        settings.DEBUG = True
        user = self.service.register('+79007000001', 'client')
        assert user.role == 'client'
        assert user.phone == '+79007000001'
        assert not user.is_verified

    def test_register_specialist(self, settings):
        settings.DEBUG = True
        user = self.service.register('+79007000002', 'pro')
        assert user.role == 'specialist'

    def test_register_duplicate_phone(self, settings):
        settings.DEBUG = True
        self.service.register('+79007000003', 'client')
        with pytest.raises(PhoneAlreadyRegisteredError):
            self.service.register('+79007000003', 'client')

    def test_login_success(self, settings):
        settings.DEBUG = True
        self.service.register('+79007000004', 'client')
        # Wait for rate limit
        otp = OTPCode.objects.filter(phone='+79007000004').first()
        OTPCode.objects.filter(pk=otp.pk).update(
            created_at=timezone.now() - timedelta(seconds=61),
        )
        self.service.login('+79007000004')
        assert OTPCode.objects.filter(phone='+79007000004').count() == 2

    def test_login_user_not_found(self):
        with pytest.raises(UserNotFoundError):
            self.service.login('+79009999999')

    def test_verify_and_get_tokens(self, settings):
        settings.DEBUG = True
        self.service.register('+79007000005', 'client')
        tokens = self.service.verify_and_get_tokens('+79007000005', '000000')
        assert 'access' in tokens
        assert 'refresh' in tokens
        assert tokens['user']['phone'] == '+79007000005'
        assert tokens['user']['is_verified'] is True
        # Check user is now verified in DB
        user = User.objects.get(phone='+79007000005')
        assert user.is_verified
