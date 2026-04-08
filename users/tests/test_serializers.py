import logging

import pytest

from users.models import User
from users.serializers import (
    PhoneSerializer,
    RegisterSerializer,
    VerifyOTPSerializer,
)

logger = logging.getLogger(__name__)


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
        is_valid = serializer.is_valid()
        logger.info("RegisterSerializer valid=%s, errors=%s", is_valid, serializer.errors)
        assert is_valid, serializer.errors
        user = serializer.save()
        logger.info("Created user: username=%s, role=%s", user.username, user.role)
        assert user.username == 'newuser'
        assert user.check_password('strongpass123')
        assert user.role == 'client'

    def test_password_write_only(self):
        user = User.objects.create_user(
            username='u1', password='pass123', role='client',
            phone='+79003000001',
        )
        output = RegisterSerializer(user).data
        logger.info("Serialized fields: %s", list(output.keys()))
        assert 'password' not in output

    def test_duplicate_username_rejected(self):
        User.objects.create_user(
            username='existing', password='pass', role='client',
            phone='+79003000002',
        )
        data = {'username': 'existing', 'password': 'pass123', 'role': 'client', 'phone': '+79003000003'}
        serializer = RegisterSerializer(data=data)
        is_valid = serializer.is_valid()
        logger.info("Duplicate username valid=%s, errors=%s", is_valid, serializer.errors)
        assert not is_valid
        assert 'username' in serializer.errors


class TestPhoneSerializer:
    def test_valid_phone_plus7(self):
        serializer = PhoneSerializer(data={'phone': '+79001234567'})
        logger.info("Phone +79001234567: valid=%s", serializer.is_valid())
        assert serializer.is_valid()
        assert serializer.validated_data['phone'] == '+79001234567'

    def test_valid_phone_8_normalizes(self):
        serializer = PhoneSerializer(data={'phone': '89001234567'})
        is_valid = serializer.is_valid()
        normalized = serializer.validated_data.get('phone') if is_valid else None
        logger.info("Phone 89001234567: valid=%s, normalized=%s", is_valid, normalized)
        assert is_valid
        assert normalized == '+79001234567'

    def test_valid_phone_with_spaces(self):
        serializer = PhoneSerializer(data={'phone': '+7 900 123 45 67'})
        is_valid = serializer.is_valid()
        normalized = serializer.validated_data.get('phone') if is_valid else None
        logger.info("Phone with spaces: valid=%s, normalized=%s", is_valid, normalized)
        assert is_valid
        assert normalized == '+79001234567'

    def test_invalid_phone_too_short(self):
        serializer = PhoneSerializer(data={'phone': '+7900123'})
        is_valid = serializer.is_valid()
        logger.info("Short phone: valid=%s, errors=%s", is_valid, serializer.errors)
        assert not is_valid

    def test_invalid_phone_wrong_prefix(self):
        serializer = PhoneSerializer(data={'phone': '+19001234567'})
        is_valid = serializer.is_valid()
        logger.info("Wrong prefix phone: valid=%s, errors=%s", is_valid, serializer.errors)
        assert not is_valid


class TestVerifyOTPSerializer:
    def test_valid(self):
        serializer = VerifyOTPSerializer(data={'phone': '+79001234567', 'code': '1234'})
        logger.info("Valid OTP data: valid=%s", serializer.is_valid())
        assert serializer.is_valid()

    def test_non_numeric_code(self):
        serializer = VerifyOTPSerializer(data={'phone': '+79001234567', 'code': 'abcdef'})
        is_valid = serializer.is_valid()
        logger.info("Non-numeric code: valid=%s, errors=%s", is_valid, serializer.errors)
        assert not is_valid
        assert 'code' in serializer.errors

    def test_code_too_short(self):
        serializer = VerifyOTPSerializer(data={'phone': '+79001234567', 'code': '12'})
        is_valid = serializer.is_valid()
        logger.info("Short code: valid=%s, errors=%s", is_valid, serializer.errors)
        assert not is_valid
