import logging
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from users.models import DeviceToken, OTPCode, Profile, Service, User

logger = logging.getLogger(__name__)


@pytest.mark.django_db
class TestUserModel:
    def test_create_client(self):
        user = User.objects.create_user(
            username='c1', password='pass', role='client', phone='+79001000001',
        )
        logger.info("Created client user: id=%s, role=%s", user.pk, user.role)
        assert user.role == 'client'
        assert user.is_client
        assert not user.is_specialist
        assert str(user) == 'c1 (client)'

    def test_create_specialist(self):
        user = User.objects.create_user(
            username='s1', password='pass', role='specialist', phone='+79001000002',
        )
        logger.info("Created specialist user: id=%s, role=%s", user.pk, user.role)
        assert user.role == 'specialist'
        assert user.is_specialist
        assert not user.is_client

    def test_phone_unique(self):
        User.objects.create_user(
            username='u1', password='pass', role='client', phone='+79001000003',
        )
        logger.info("Attempting duplicate phone creation (should fail)")
        with pytest.raises(Exception):
            User.objects.create_user(
                username='u2', password='pass', role='client', phone='+79001000003',
            )
        logger.info("Duplicate phone correctly rejected")

    def test_is_verified_default_false(self):
        user = User.objects.create_user(
            username='u3', password='pass', role='client', phone='+79001000004',
        )
        logger.info("New user is_verified=%s", user.is_verified)
        assert user.is_verified is False


@pytest.mark.django_db
class TestServiceModel:
    def test_create_service(self, specialist_user):
        service = Service.objects.create(
            specialist=specialist_user,
            name='Стрижка',
            price=Decimal('500.00'),
            duration_minutes=60,
        )
        logger.info("Created service: id=%s, name=%s, price=%s", service.pk, service.name, service.price)
        assert str(service) == f'Стрижка — {specialist_user.username}'
        assert service.created_at is not None

    def test_cascade_delete(self, specialist_user):
        Service.objects.create(
            specialist=specialist_user, name='Test',
            price=Decimal('100'), duration_minutes=30,
        )
        assert Service.objects.count() == 1
        logger.info("Deleting specialist user — services should cascade")
        specialist_user.delete()
        logger.info("Services after delete: count=%d", Service.objects.count())
        assert Service.objects.count() == 0


@pytest.mark.django_db
class TestProfileModel:
    def test_str(self, specialist_user):
        profile = Profile.objects.get(user=specialist_user)
        logger.info("Profile str: %s", str(profile))
        assert str(profile) == f'Профиль {specialist_user.username}'

    def test_defaults(self, client_user):
        profile = Profile.objects.get(user=client_user)
        profile.full_name = 'Test'
        profile.save()
        logger.info(
            "Profile defaults: bio='%s', city='%s', experience=%d",
            profile.bio, profile.city, profile.experience_years,
        )
        assert profile.bio == ''
        assert profile.city == ''
        assert profile.experience_years == 0


@pytest.mark.django_db
class TestOTPCodeModel:
    def test_create(self):
        now = timezone.now()
        otp = OTPCode.objects.create(
            phone='+79001000001',
            code='123456',
            expires_at=now + timedelta(minutes=5),
        )
        logger.info("Created OTP: phone=%s, is_valid=%s, is_expired=%s", otp.phone, otp.is_valid, otp.is_expired)
        assert otp.is_used is False
        assert otp.attempts == 0
        assert not otp.is_expired
        assert otp.is_valid
        assert 'active' in str(otp)

    def test_expired(self):
        otp = OTPCode.objects.create(
            phone='+79001000001',
            code='123456',
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        logger.info("Expired OTP: is_expired=%s, is_valid=%s", otp.is_expired, otp.is_valid)
        assert otp.is_expired
        assert not otp.is_valid

    def test_used(self):
        otp = OTPCode.objects.create(
            phone='+79001000001',
            code='123456',
            expires_at=timezone.now() + timedelta(minutes=5),
            is_used=True,
        )
        logger.info("Used OTP: is_valid=%s, str=%s", otp.is_valid, str(otp))
        assert not otp.is_valid
        assert 'used' in str(otp)


@pytest.mark.django_db
class TestDeviceTokenModel:
    def test_create(self, client_user):
        token = DeviceToken.objects.create(
            user=client_user,
            token='fcm_test_token_123',
            app_type='client',
            platform='ios',
        )
        logger.info("Created DeviceToken: %s", str(token))
        assert token.is_active
        assert token.app_type == 'client'
        assert token.platform == 'ios'
        assert 'client' in str(token)

    def test_unique_token(self, client_user):
        DeviceToken.objects.create(
            user=client_user,
            token='unique_token',
            app_type='client',
            platform='android',
        )
        with pytest.raises(Exception):
            DeviceToken.objects.create(
                user=client_user,
                token='unique_token',
                app_type='pro',
                platform='ios',
            )

    def test_cascade_delete(self, client_user):
        DeviceToken.objects.create(
            user=client_user,
            token='cascade_token',
            app_type='client',
            platform='ios',
        )
        assert DeviceToken.objects.count() == 1
        client_user.delete()
        assert DeviceToken.objects.count() == 0
