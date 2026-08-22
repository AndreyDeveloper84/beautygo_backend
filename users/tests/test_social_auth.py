"""Tests for Social Auth API (DRF-55, DRF-1245)."""

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
class TestSocialAuthFlow:
    """Provider-agnostic login flow.

    Used to be exercised through VK; VK is disabled by DRF-1245, so the
    same assertions now run against Google — the flow under test
    (SocialAccount lookup, email linking, token error mapping) is shared
    by every provider and is not VK-specific.
    """
    URL = SOCIAL_URL.format(provider='google')

    def test_new_user_created(self, client_app):
        mock_google = MagicMock(return_value=make_social_info(
            provider="google", uid="g_001",
        ))
        verifiers = mock_verifiers(google=mock_google)
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers):
            response = client_app.post(
                self.URL, {'token': 'fake_google_token'}, format='json',
            )
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert data['is_new_user'] is True
        assert data['access_token']
        assert data['refresh_token']
        assert data['user']['role'] == 'client'
        assert SocialAccount.objects.filter(
            provider='google', provider_uid='g_001',
        ).exists()

    def test_existing_user_login(self, client_app):
        user = User.objects.create(
            username='existing_google', role='client',
        )
        SocialAccount.objects.create(
            user=user, provider='google', provider_uid='g_002',
        )
        mock_google = MagicMock(return_value=make_social_info(
            provider="google", uid="g_002",
        ))
        verifiers = mock_verifiers(google=mock_google)
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
        mock_google = MagicMock(return_value=make_social_info(
            provider="google", uid="g_003", email="match@test.com",
        ))
        verifiers = mock_verifiers(google=mock_google)
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers):
            response = client_app.post(
                self.URL, {'token': 'fake'}, format='json',
            )
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['is_new_user'] is False
        assert response.data['data']['user']['id'] == user.pk
        assert SocialAccount.objects.filter(
            user=user, provider='google',
        ).exists()

    def test_invalid_token(self, client_app):
        from users.social_auth import SocialAuthTokenError
        mock_google = MagicMock(side_effect=SocialAuthTokenError())
        verifiers = mock_verifiers(google=mock_google)
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
            SOCIAL_URL.format(provider='google'),
            {}, format='json',
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_missing_app_type(self):
        client = APIClient()
        response = client.post(
            SOCIAL_URL.format(provider='google'),
            {'token': 'x'}, format='json',
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_pro_app_creates_specialist(self, pro_app):
        mock_google = MagicMock(return_value=make_social_info(
            provider="google", uid="g_pro_001",
        ))
        verifiers = mock_verifiers(google=mock_google)
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers):
            response = pro_app.post(
                SOCIAL_URL.format(provider='google'),
                {'token': 'x'}, format='json',
            )
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['user']['role'] == 'specialist'

    def test_phone_required_when_no_phone(self, client_app):
        mock_google = MagicMock(return_value=make_social_info(
            provider="google", uid="g_nophone", phone=None,
        ))
        verifiers = mock_verifiers(google=mock_google)
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers):
            response = client_app.post(
                SOCIAL_URL.format(provider='google'),
                {'token': 'x'}, format='json',
            )
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['phone_required'] is True
        assert response.data['data']['user']['phone'] is None


@pytest.mark.django_db
class TestBindPhone:

    def test_bind_phone_success(self, client_app):
        mock_otp = MagicMock()
        mock_otp.consume_otp.return_value = True

        user = User.objects.create(
            username='social_bind', role='client', phone=None,
        )
        client_app.force_authenticate(user=user)

        with patch(
            'users.services.OTPService', return_value=mock_otp,
        ):
            response = client_app.post(
                BIND_PHONE_URL,
                {'phone': '+79001112233', 'code': '0000'},
                format='json',
            )
        assert response.status_code == status.HTTP_200_OK
        user.refresh_from_db()
        assert user.phone == '+79001112233'
        assert user.is_verified is True

    def test_bind_phone_unauthenticated(self, client_app):
        response = client_app.post(
            BIND_PHONE_URL,
            {'phone': '+79001112233', 'code': '0000'},
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
            {'phone': '+79001112233', 'code': '0000'},
            format='json',
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error']['code'] == 'PHONE_ALREADY_BOUND'


class TestGoogleAudienceEnforcement:
    """Regression tests for ln-621 #1 — Google id-tokens issued for another
    OAuth client must be rejected when GOOGLE_CLIENT_ID is configured."""

    def _fake_response(self, aud: str):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "sub": "g-user-1",
            "aud": aud,
            "email": "u@example.com",
            "given_name": "Test",
            "family_name": "User",
        }
        return resp

    def test_rejects_mismatched_aud_when_configured(self, settings):
        """If GOOGLE_CLIENT_ID is set, a token with different aud must fail."""
        from users.social_auth import SocialAuthTokenError, verify_google_token

        settings.GOOGLE_CLIENT_ID = "our-real-client.apps.googleusercontent.com"

        with patch("users.social_auth.requests.get") as mock_get:
            mock_get.return_value = self._fake_response(
                aud="attacker-client.apps.googleusercontent.com",
            )
            with pytest.raises(SocialAuthTokenError):
                verify_google_token("fake-token")

    def test_accepts_matching_aud_when_configured(self, settings):
        from users.social_auth import verify_google_token

        settings.GOOGLE_CLIENT_ID = "our-real-client.apps.googleusercontent.com"

        with patch("users.social_auth.requests.get") as mock_get:
            mock_get.return_value = self._fake_response(
                aud="our-real-client.apps.googleusercontent.com",
            )
            info = verify_google_token("fake-token")

        assert info.provider == "google"
        assert info.provider_uid == "g-user-1"


class TestProdOAuthFailFast:
    """Regression tests for ln-621 #1 — prod settings must refuse to import
    without the OAuth audience env vars configured."""

    def _reload_prod(self, monkeypatch, **env_overrides):
        """Reload djangoProject.settings.prod with env overrides."""
        import importlib
        import sys

        # Ensure the target env is applied before the module runs its
        # top-level fail-fast check.
        for key, value in env_overrides.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)

        # Base must also be reloaded — it reads env once at import time and
        # then prod.py inherits its values via `from .base import *`.
        sys.modules.pop("djangoProject.settings.prod", None)
        sys.modules.pop("djangoProject.settings.base", None)
        return importlib.import_module("djangoProject.settings.prod")

    def test_raises_when_google_client_id_missing(self, monkeypatch):
        from django.core.exceptions import ImproperlyConfigured

        with pytest.raises(ImproperlyConfigured, match="GOOGLE_CLIENT_ID"):
            self._reload_prod(
                monkeypatch,
                DJANGO_SECRET_KEY="test-secret",
                GOOGLE_CLIENT_ID=None,
                APPLE_CLIENT_ID="apple-id",
                YOOKASSA_WEBHOOK_ALLOWED_IPS="185.71.76.0/27",
                AYLA_INTERNAL_API_TOKEN="bearer-token",
            )

    def test_raises_when_apple_client_id_missing(self, monkeypatch):
        from django.core.exceptions import ImproperlyConfigured

        with pytest.raises(ImproperlyConfigured, match="APPLE_CLIENT_ID"):
            self._reload_prod(
                monkeypatch,
                DJANGO_SECRET_KEY="test-secret",
                GOOGLE_CLIENT_ID="google-id",
                APPLE_CLIENT_ID=None,
                YOOKASSA_WEBHOOK_ALLOWED_IPS="185.71.76.0/27",
                AYLA_INTERNAL_API_TOKEN="bearer-token",
            )

    def test_raises_when_yookassa_webhook_ips_missing(self, monkeypatch):
        from django.core.exceptions import ImproperlyConfigured

        with pytest.raises(
            ImproperlyConfigured, match="YOOKASSA_WEBHOOK_ALLOWED_IPS",
        ):
            self._reload_prod(
                monkeypatch,
                DJANGO_SECRET_KEY="test-secret",
                GOOGLE_CLIENT_ID="google-id",
                APPLE_CLIENT_ID="apple-id",
                YOOKASSA_WEBHOOK_ALLOWED_IPS=None,
                AYLA_INTERNAL_API_TOKEN="bearer-token",
            )

    def test_imports_when_all_required_set(self, monkeypatch):
        # A5 added AYLA_INTERNAL_API_TOKEN to the required tuple — pass
        # it here so this golden-path test does not become a false
        # positive for the new gate. Dedicated coverage for the token
        # itself lives in djangoProject/tests/test_prod_required_env.py.
        module = self._reload_prod(
            monkeypatch,
            DJANGO_SECRET_KEY="test-secret",
            GOOGLE_CLIENT_ID="google-id",
            APPLE_CLIENT_ID="apple-id",
            YOOKASSA_WEBHOOK_ALLOWED_IPS="185.71.76.0/27",
            AYLA_INTERNAL_API_TOKEN="bearer-token",
        )
        assert module.GOOGLE_CLIENT_ID == "google-id"
        assert module.APPLE_CLIENT_ID == "apple-id"
        assert module.YOOKASSA_WEBHOOK_ALLOWED_IPS == ["185.71.76.0/27"]


@pytest.mark.django_db
class TestDisabledProviders:
    """DRF-1245 — VK and Yandex login must be unreachable.

    Neither provider offers a way to prove that the ``access_token`` the
    mobile client posts was minted for *our* OAuth application: VK
    ``users.get`` and Yandex ``login.yandex.ru/info`` both answer for a
    token from any application and never name the issuer. Google pins
    ``aud`` (see TestGoogleAudienceEnforcement) and Apple verifies audience
    + issuer on a signed JWT, so only VK and Yandex are affected.
    """

    @pytest.mark.parametrize('provider', ['vk', 'yandex'])
    def test_login_rejected(self, client_app, provider):
        response = client_app.post(
            SOCIAL_URL.format(provider=provider),
            {'token': 'anything'}, format='json',
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data['error']['code'] == 'PROVIDER_DISABLED'

    @pytest.mark.parametrize('provider', ['vk', 'yandex'])
    def test_no_account_is_created(self, client_app, provider):
        users_before = User.objects.count()
        client_app.post(
            SOCIAL_URL.format(provider=provider),
            {'token': 'anything'}, format='json',
        )
        assert User.objects.count() == users_before
        assert not SocialAccount.objects.filter(provider=provider).exists()

    @pytest.mark.parametrize('provider', ['vk', 'yandex'])
    def test_provider_api_is_never_contacted(self, client_app, provider):
        """The rejection happens before any outbound call.

        Matters beyond tidiness: it means a disabled provider cannot be
        used to make the backend fan out requests to a third-party host.
        """
        with patch('users.social_auth.requests') as mock_requests:
            response = client_app.post(
                SOCIAL_URL.format(provider=provider),
                {'token': 'anything'}, format='json',
            )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        mock_requests.get.assert_not_called()

    @pytest.mark.parametrize('provider', ['vk', 'yandex'])
    def test_guard_holds_even_if_a_verifier_is_wired_back_in(
        self, client_app, provider,
    ):
        """Defence in depth.

        Dropping VK/Yandex out of ``PROVIDER_VERIFIERS`` is not on its own
        the control — someone restoring the dict entry must not silently
        reopen the door. The explicit ``DISABLED_PROVIDERS`` check runs
        first, so a fully wired verifier is still unreachable.
        """
        mock_verifier = MagicMock(return_value=make_social_info(
            provider=provider, uid=f'{provider}_wired',
        ))
        verifiers = mock_verifiers(**{provider: mock_verifier})
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers):
            response = client_app.post(
                SOCIAL_URL.format(provider=provider),
                {'token': 'anything'}, format='json',
            )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data['error']['code'] == 'PROVIDER_DISABLED'
        mock_verifier.assert_not_called()

    @pytest.mark.parametrize(
        'provider,contact_field,contact_value',
        [
            ('vk', 'phone', '+79001234567'),
            ('yandex', 'phone', '+79007654321'),
            ('yandex', 'email', 'victim@example.com'),
        ],
    )
    def test_cannot_adopt_an_existing_account_via_self_declared_contact(
        self, client_app, provider, contact_field, contact_value,
    ):
        """The takeover path this ticket closes.

        ``_find_or_create_user`` matches an incoming social identity
        against an existing User by email and by phone. For VK the phone
        comes from the ``mobile_phone`` profile field and for Yandex from
        ``default_phone`` / ``default_email`` — all three are values the
        provider account holder types in themselves, with no ownership
        check. Setting one to a victim's phone or email was enough to be
        handed the victim's account and a full-privilege JWT.
        """
        victim = User.objects.create(
            username='victim', role='client',
            **{contact_field: contact_value},
        )
        mock_verifier = MagicMock(return_value=make_social_info(
            provider=provider, uid=f'{provider}_attacker',
            **{contact_field: contact_value},
        ))
        verifiers = mock_verifiers(**{provider: mock_verifier})
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers):
            response = client_app.post(
                SOCIAL_URL.format(provider=provider),
                {'token': 'attacker_token'}, format='json',
            )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert 'access_token' not in (response.data.get('data') or {})
        assert not SocialAccount.objects.filter(user=victim).exists()

    def test_registry_and_disabled_set_are_explicit(self):
        """Pins the registry so re-enabling is a deliberate, reviewed act.

        Adding "vk" or "yandex" back to ``PROVIDER_VERIFIERS`` requires a
        real app-binding step first — VK ``secure.checkToken`` with an
        ``app_id`` assertion, Yandex the authorization-code exchange —
        plus a decision on identity merging by unverified contacts. This
        test fails loudly if the dict grows without that work.
        """
        from users.social_auth import DISABLED_PROVIDERS, PROVIDER_VERIFIERS

        assert set(PROVIDER_VERIFIERS) == {'google', 'apple'}
        assert DISABLED_PROVIDERS == frozenset({'vk', 'yandex'})
        assert not (set(PROVIDER_VERIFIERS) & DISABLED_PROVIDERS)

    def test_unknown_provider_is_distinguishable_from_disabled(
        self, client_app,
    ):
        """A disabled provider is a policy answer, a typo is a 400.

        Collapsing the two would tell a client integrating VK that its URL
        is wrong, sending it round a debugging loop instead of to the
        supported OTP flow.
        """
        unknown = client_app.post(
            SOCIAL_URL.format(provider='facebook'),
            {'token': 'x'}, format='json',
        )
        disabled = client_app.post(
            SOCIAL_URL.format(provider='vk'),
            {'token': 'x'}, format='json',
        )
        assert unknown.status_code == status.HTTP_400_BAD_REQUEST
        assert unknown.data['error']['code'] == 'INVALID_PROVIDER'
        assert disabled.status_code == status.HTTP_403_FORBIDDEN
        assert disabled.data['error']['code'] == 'PROVIDER_DISABLED'
