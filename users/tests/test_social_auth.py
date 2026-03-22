"""Tests for Social Auth API (DRF-55)."""

import logging
from unittest.mock import MagicMock, patch

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from users.models import SocialAccount, User
from users.social_auth import SocialUserInfo

logger = logging.getLogger(__name__)

SOCIAL_URL = '/api/v1/auth/social/{provider}/'
BIND_PHONE_URL = '/api/v1/auth/bind-phone/'


def make_social_info(provider="vk", uid="12345", **kwargs):
    """Helper to create SocialUserInfo with defaults."""
    defaults = {
        "provider": provider,
        "provider_uid": uid,
        "email": f"{uid}@example.com",
        "first_name": "Test",
        "last_name": "User",
        "phone": None,
        "extra_data": {"id": uid},
    }
    defaults.update(kwargs)
    return SocialUserInfo(**defaults)


def mock_verifiers(**overrides):
    """Create a patched PROVIDER_VERIFIERS dict with mock functions."""
    verifiers = {}
    for provider in ("vk", "google", "apple", "yandex"):
        if provider in overrides:
            verifiers[provider] = overrides[provider]
        else:
            m = MagicMock(side_effect=Exception("Not mocked"))
            verifiers[provider] = m
    return verifiers


@pytest.fixture
def client_app():
    c = APIClient()
    c.defaults['HTTP_X_APP_TYPE'] = 'client'
    return c


@pytest.fixture
def pro_app():
    c = APIClient()
    c.defaults['HTTP_X_APP_TYPE'] = 'pro'
    return c


@pytest.mark.django_db
class TestSocialAuthVK:
    URL = SOCIAL_URL.format(provider='vk')

    def test_new_user_created(self, client_app):
        mock_vk = MagicMock(return_value=make_social_info(
            provider="vk", uid="vk_001",
        ))
        verifiers = mock_verifiers(vk=mock_vk)
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers):
            response = client_app.post(
                self.URL, {'token': 'fake_vk_token'}, format='json',
            )
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert data['is_new_user'] is True
        assert data['access']
        assert data['refresh']
        assert data['user']['role'] == 'client'
        assert SocialAccount.objects.filter(
            provider='vk', provider_uid='vk_001',
        ).exists()

    def test_existing_user_login(self, client_app):
        user = User.objects.create(
            username='existing_vk', role='client',
        )
        SocialAccount.objects.create(
            user=user, provider='vk', provider_uid='vk_002',
        )
        mock_vk = MagicMock(return_value=make_social_info(
            provider="vk", uid="vk_002",
        ))
        verifiers = mock_verifiers(vk=mock_vk)
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers):
            response = client_app.post(
                self.URL, {'token': 'fake'}, format='json',
            )
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['is_new_user'] is False
        assert response.data['data']['user']['id'] == user.pk

    def test_link_by_email(self, client_app):
        user = User.objects.create(
            username='email_match', email='match@test.com', role='client',
        )
        mock_vk = MagicMock(return_value=make_social_info(
            provider="vk", uid="vk_003", email="match@test.com",
        ))
        verifiers = mock_verifiers(vk=mock_vk)
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers):
            response = client_app.post(
                self.URL, {'token': 'fake'}, format='json',
            )
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['is_new_user'] is False
        assert response.data['data']['user']['id'] == user.pk
        assert SocialAccount.objects.filter(
            user=user, provider='vk',
        ).exists()

    def test_invalid_token(self, client_app):
        from users.social_auth import SocialAuthTokenError
        mock_vk = MagicMock(side_effect=SocialAuthTokenError())
        verifiers = mock_verifiers(vk=mock_vk)
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers):
            response = client_app.post(
                self.URL, {'token': 'bad'}, format='json',
            )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.data['error']['code'] == 'SOCIAL_TOKEN_INVALID'


@pytest.mark.django_db
class TestSocialAuthGoogle:
    URL = SOCIAL_URL.format(provider='google')

    def test_new_user_with_email(self, client_app):
        mock_google = MagicMock(return_value=make_social_info(
            provider="google", uid="google_001",
            email="new@gmail.com",
            first_name="Jane", last_name="Doe",
        ))
        verifiers = mock_verifiers(google=mock_google)
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers):
            response = client_app.post(
                self.URL, {'token': 'google_id_token'}, format='json',
            )
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['is_new_user'] is True
        user = User.objects.get(
            social_accounts__provider='google',
            social_accounts__provider_uid='google_001',
        )
        assert user.email == 'new@gmail.com'
        assert user.first_name == 'Jane'


@pytest.mark.django_db
class TestSocialAuthApple:
    URL = SOCIAL_URL.format(provider='apple')

    def test_apple_with_name_from_request(self, client_app):
        mock_apple = MagicMock(return_value=make_social_info(
            provider="apple", uid="apple_001",
            first_name="", last_name="",
        ))
        verifiers = mock_verifiers(apple=mock_apple)
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers):
            response = client_app.post(
                self.URL,
                {
                    'token': 'apple_jwt',
                    'first_name': 'Maria',
                    'last_name': 'Ivanova',
                },
                format='json',
            )
        assert response.status_code == status.HTTP_200_OK
        user = User.objects.get(
            social_accounts__provider='apple',
            social_accounts__provider_uid='apple_001',
        )
        assert user.first_name == 'Maria'
        assert user.last_name == 'Ivanova'


@pytest.mark.django_db
class TestSocialAuthYandex:
    URL = SOCIAL_URL.format(provider='yandex')

    def test_yandex_with_phone(self, client_app):
        mock_yandex = MagicMock(return_value=make_social_info(
            provider="yandex", uid="yandex_001",
            phone="+79001234567",
        ))
        verifiers = mock_verifiers(yandex=mock_yandex)
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers):
            response = client_app.post(
                self.URL, {'token': 'ya_token'}, format='json',
            )
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert data['phone_required'] is False
        assert data['user']['phone'] == '+79001234567'


@pytest.mark.django_db
class TestSocialAuthGeneral:

    def test_invalid_provider(self, client_app):
        response = client_app.post(
            SOCIAL_URL.format(provider='facebook'),
            {'token': 'x'}, format='json',
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error']['code'] == 'INVALID_PROVIDER'

    def test_missing_token(self, client_app):
        response = client_app.post(
            SOCIAL_URL.format(provider='vk'),
            {}, format='json',
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_missing_app_type(self):
        client = APIClient()
        response = client.post(
            SOCIAL_URL.format(provider='vk'),
            {'token': 'x'}, format='json',
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_pro_app_creates_specialist(self, pro_app):
        mock_vk = MagicMock(return_value=make_social_info(
            provider="vk", uid="vk_pro_001",
        ))
        verifiers = mock_verifiers(vk=mock_vk)
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers):
            response = pro_app.post(
                SOCIAL_URL.format(provider='vk'),
                {'token': 'x'}, format='json',
            )
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['user']['role'] == 'specialist'

    def test_phone_required_when_no_phone(self, client_app):
        mock_vk = MagicMock(return_value=make_social_info(
            provider="vk", uid="vk_nophone", phone=None,
        ))
        verifiers = mock_verifiers(vk=mock_vk)
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers):
            response = client_app.post(
                SOCIAL_URL.format(provider='vk'),
                {'token': 'x'}, format='json',
            )
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['phone_required'] is True
        assert response.data['data']['user']['phone'] is None


@pytest.mark.django_db
class TestBindPhone:

    def test_bind_phone_success(self, client_app):
        mock_otp = MagicMock()
        mock_otp.verify_otp.return_value = True

        user = User.objects.create(
            username='social_bind', role='client', phone=None,
        )
        client_app.force_authenticate(user=user)

        with patch(
            'users.services.OTPService', return_value=mock_otp,
        ):
            response = client_app.post(
                BIND_PHONE_URL,
                {'phone': '+79001112233', 'code': '000000'},
                format='json',
            )
        assert response.status_code == status.HTTP_200_OK
        user.refresh_from_db()
        assert user.phone == '+79001112233'
        assert user.is_verified is True

    def test_bind_phone_unauthenticated(self, client_app):
        response = client_app.post(
            BIND_PHONE_URL,
            {'phone': '+79001112233', 'code': '000000'},
            format='json',
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_bind_phone_already_has_phone(self, client_app):
        user = User.objects.create(
            username='has_phone', role='client', phone='+79009999999',
        )
        client_app.force_authenticate(user=user)
        response = client_app.post(
            BIND_PHONE_URL,
            {'phone': '+79001112233', 'code': '000000'},
            format='json',
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error']['code'] == 'PHONE_ALREADY_BOUND'
