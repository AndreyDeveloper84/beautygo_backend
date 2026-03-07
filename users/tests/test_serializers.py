import pytest

from users.models import User
from users.serializers import RegisterSerializer


@pytest.mark.django_db
class TestRegisterSerializer:
    def test_valid_registration(self):
        data = {
            'username': 'newuser',
            'password': 'strongpass123',
            'email': 'new@test.com',
            'phone': '+71234567890',
            'role': 'client',
        }
        serializer = RegisterSerializer(data=data)
        assert serializer.is_valid(), serializer.errors
        user = serializer.save()
        assert user.username == 'newuser'
        assert user.check_password('strongpass123')
        assert user.role == 'client'

    def test_password_write_only(self):
        user = User.objects.create_user(
            username='u1', password='pass123', role='client',
        )
        output = RegisterSerializer(user).data
        assert 'password' not in output

    def test_duplicate_username_rejected(self):
        User.objects.create_user(username='existing', password='pass', role='client')
        data = {'username': 'existing', 'password': 'pass123', 'role': 'client'}
        serializer = RegisterSerializer(data=data)
        assert not serializer.is_valid()
        assert 'username' in serializer.errors
