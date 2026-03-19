import pytest

from users.models import User
from users.serializers import (
    PhoneSerializer,
    RegisterSerializer,
    VerifyOTPSerializer,
)


@pytest.mark.django_db
class TestRegisterSerializer:
    """Legacy RegisterSerializer tests."""

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
            phone='+79003000001',
        )
        output = RegisterSerializer(user).data
        assert 'password' not in output

    def test_duplicate_username_rejected(self):
        User.objects.create_user(
            username='existing', password='pass', role='client',
            phone='+79003000002',
        )
        data = {'username': 'existing', 'password': 'pass123', 'role': 'client', 'phone': '+79003000003'}
        serializer = RegisterSerializer(data=data)
        assert not serializer.is_valid()
        assert 'username' in serializer.errors


class TestPhoneSerializer:
    def test_valid_phone_plus7(self):
        serializer = PhoneSerializer(data={'phone': '+79001234567'})
        assert serializer.is_valid()
        assert serializer.validated_data['phone'] == '+79001234567'

    def test_valid_phone_8_normalizes(self):
        serializer = PhoneSerializer(data={'phone': '89001234567'})
        assert serializer.is_valid()
        assert serializer.validated_data['phone'] == '+79001234567'

    def test_valid_phone_with_spaces(self):
        serializer = PhoneSerializer(data={'phone': '+7 900 123 45 67'})
        assert serializer.is_valid()
        assert serializer.validated_data['phone'] == '+79001234567'

    def test_invalid_phone_too_short(self):
        serializer = PhoneSerializer(data={'phone': '+7900123'})
        assert not serializer.is_valid()

    def test_invalid_phone_wrong_prefix(self):
        serializer = PhoneSerializer(data={'phone': '+19001234567'})
        assert not serializer.is_valid()


class TestVerifyOTPSerializer:
    def test_valid(self):
        serializer = VerifyOTPSerializer(data={'phone': '+79001234567', 'code': '123456'})
        assert serializer.is_valid()

    def test_non_numeric_code(self):
        serializer = VerifyOTPSerializer(data={'phone': '+79001234567', 'code': 'abcdef'})
        assert not serializer.is_valid()
        assert 'code' in serializer.errors

    def test_code_too_short(self):
        serializer = VerifyOTPSerializer(data={'phone': '+79001234567', 'code': '12'})
        assert not serializer.is_valid()
