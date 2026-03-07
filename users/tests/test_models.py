import pytest
from decimal import Decimal

from users.models import User, Service, Profile


@pytest.mark.django_db
class TestUserModel:
    def test_create_client(self):
        user = User.objects.create_user(username='c1', password='pass', role='client')
        assert user.role == 'client'
        assert str(user) == 'c1 (client)'

    def test_create_specialist(self):
        user = User.objects.create_user(username='s1', password='pass', role='specialist')
        assert user.role == 'specialist'
        assert str(user) == 's1 (specialist)'

    def test_phone_optional(self):
        user = User.objects.create_user(username='u1', password='pass', role='client')
        assert user.phone is None


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
