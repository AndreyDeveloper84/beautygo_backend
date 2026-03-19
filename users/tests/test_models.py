import pytest
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from users.models import OTPCode, Profile, Service, User


@pytest.mark.django_db
class TestUserModel:
    def test_create_client(self):
        user = User.objects.create_user(
            username='c1', password='pass', role='client', phone='+79001000001',
        )
        assert user.role == 'client'
        assert user.is_client
        assert not user.is_specialist
        assert str(user) == 'c1 (client)'

    def test_create_specialist(self):
        user = User.objects.create_user(
            username='s1', password='pass', role='specialist', phone='+79001000002',
        )
        assert user.role == 'specialist'
        assert user.is_specialist
        assert not user.is_client

    def test_phone_unique(self):
        User.objects.create_user(
            username='u1', password='pass', role='client', phone='+79001000003',
        )
        with pytest.raises(Exception):
            User.objects.create_user(
                username='u2', password='pass', role='client', phone='+79001000003',
            )

    def test_is_verified_default_false(self):
        user = User.objects.create_user(
            username='u3', password='pass', role='client', phone='+79001000004',
        )
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
        assert str(service) == f'Стрижка — {specialist_user.username}'
        assert service.created_at is not None

    def test_cascade_delete(self, specialist_user):
        Service.objects.create(
            specialist=specialist_user, name='Test',
            price=Decimal('100'), duration_minutes=30,
        )
        assert Service.objects.count() == 1
        specialist_user.delete()
        assert Service.objects.count() == 0


@pytest.mark.django_db
class TestProfileModel:
    def test_str(self, specialist_user):
        profile = Profile.objects.get(user=specialist_user)
        assert str(profile) == f'Профиль {specialist_user.username}'

    def test_defaults(self, client_user):
        profile = Profile.objects.create(user=client_user, full_name='Test')
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
        assert otp.is_expired
        assert not otp.is_valid

    def test_used(self):
        otp = OTPCode.objects.create(
            phone='+79001000001',
            code='123456',
            expires_at=timezone.now() + timedelta(minutes=5),
            is_used=True,
        )
        assert not otp.is_valid
        assert 'used' in str(otp)
