"""Tests for Social Auth API (DRF-55)."""

import logging
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient

from users.models import SocialAccount, User
from users.social_auth import (
    SocialAuthService,
    SocialProviderDisabledError,
    SocialUserInfo,
)

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
    """VK happy-path behavior with the W0-D1 containment gate explicitly
    lifted. By default VK is rejected fail-closed — see
    TestSocialAuthContainment. W0-D2 owns verified re-enable."""

    URL = SOCIAL_URL.format(provider='vk')

    @pytest.fixture(autouse=True)
    def _lift_containment_gate(self, settings):
        settings.SOCIAL_AUTH_DISABLED_PROVIDERS = ()

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
        assert data['access_token']
        assert data['refresh_token']
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
    """Yandex happy-path behavior with the W0-D1 containment gate
    explicitly lifted. By default Yandex is rejected fail-closed — see
    TestSocialAuthContainment. W0-D2 owns verified re-enable."""

    URL = SOCIAL_URL.format(provider='yandex')

    @pytest.fixture(autouse=True)
    def _lift_containment_gate(self, settings):
        settings.SOCIAL_AUTH_DISABLED_PROVIDERS = ()

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

    @override_settings(SOCIAL_AUTH_DISABLED_PROVIDERS=())
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

    @override_settings(SOCIAL_AUTH_DISABLED_PROVIDERS=())
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
class TestSocialAuthContainment:
    """W0-D1 / AY-01 — VK and Yandex are disabled fail-closed until
    app-ownership verification lands (W0-D2). The gate must run before
    the provider verifier, any SocialAccount lookup, email/phone
    matching, and user creation."""

    def _post(self, client, provider, token):
        return client.post(
            SOCIAL_URL.format(provider=provider),
            {'token': token},
            format='json',
        )

    def _assert_rejected_fail_closed(
        self, response, provider, mock_verifier, mock_http, mock_find, token,
    ):
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data['error']['code'] == 'SOCIAL_PROVIDER_DISABLED'
        # No outbound provider request, no account lookup/linking.
        mock_verifier.assert_not_called()
        mock_http.assert_not_called()
        mock_find.assert_not_called()
        # No user or SocialAccount created.
        assert User.objects.count() == 0
        assert SocialAccount.objects.count() == 0
        # Provider token is never echoed back.
        assert token not in response.content.decode()

    def test_vk_rejected_fail_closed(self, client_app):
        mock_vk = MagicMock()
        verifiers = mock_verifiers(vk=mock_vk)
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers), \
                patch('users.social_auth.requests.get') as mock_http, \
                patch.object(
                    SocialAuthService, '_find_or_create_user',
                ) as mock_find:
            response = self._post(client_app, 'vk', 'fake_vk_token')

        self._assert_rejected_fail_closed(
            response, 'vk', mock_vk, mock_http, mock_find, 'fake_vk_token',
        )

    def test_yandex_rejected_fail_closed(self, client_app):
        mock_yandex = MagicMock()
        verifiers = mock_verifiers(yandex=mock_yandex)
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers), \
                patch('users.social_auth.requests.get') as mock_http, \
                patch.object(
                    SocialAuthService, '_find_or_create_user',
                ) as mock_find:
            response = self._post(client_app, 'yandex', 'fake_ya_token')

        self._assert_rejected_fail_closed(
            response, 'yandex', mock_yandex, mock_http, mock_find,
            'fake_ya_token',
        )

    def test_google_still_enabled_by_default(self, client_app):
        """Google is NOT in the default denylist — verifier is reached."""
        mock_google = MagicMock(return_value=make_social_info(
            provider="google", uid="google_containment",
            email="containment@gmail.com",
        ))
        verifiers = mock_verifiers(google=mock_google)
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers):
            response = self._post(client_app, 'google', 'google_id_token')

        assert response.status_code == status.HTTP_200_OK
        mock_google.assert_called_once()

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
        """The takeover path the containment gate closes (DRF-1245).

        ``_find_or_create_user`` matches an incoming social identity
        against an existing User by email and then by phone. For VK the
        phone arrives from the ``mobile_phone`` profile field, for Yandex
        from ``default_phone`` / ``default_email`` — all three are values
        the provider account holder types in themselves, with no proof of
        ownership. Setting one to a victim's phone or email handed the
        attacker the victim's account and a full-privilege JWT.

        The fail-closed assertions above run against an empty User table,
        so this is the case they cannot cover: a victim already exists.
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
            response = self._post(client_app, provider, 'attacker_token')

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert 'access_token' not in (response.data.get('data') or {})
        assert not SocialAccount.objects.filter(user=victim).exists()
        mock_verifier.assert_not_called()

    def test_disabled_is_distinguishable_from_unknown_provider(
        self, client_app,
    ):
        """A contained provider is a policy answer, a typo is a 400.

        Collapsing the two would tell a client integrating VK that its
        URL is wrong and send it round a debugging loop instead of to the
        supported OTP flow.
        """
        unknown = self._post(client_app, 'facebook', 'x')
        disabled = self._post(client_app, 'vk', 'x')

        assert unknown.status_code == status.HTTP_400_BAD_REQUEST
        assert unknown.data['error']['code'] == 'INVALID_PROVIDER'
        assert disabled.status_code == status.HTTP_403_FORBIDDEN
        assert disabled.data['error']['code'] == 'SOCIAL_PROVIDER_DISABLED'

    def test_apple_still_enabled_by_default(self, client_app):
        """Apple is NOT in the default denylist — verifier is reached."""
        mock_apple = MagicMock(return_value=make_social_info(
            provider="apple", uid="apple_containment",
        ))
        verifiers = mock_verifiers(apple=mock_apple)
        with patch('users.social_auth.PROVIDER_VERIFIERS', verifiers):
            response = self._post(client_app, 'apple', 'apple_jwt')

        assert response.status_code == status.HTTP_200_OK
        mock_apple.assert_called_once()


@pytest.mark.django_db
class TestSocialAuthDisabledProvidersConfig:
    """Configuration safety for SOCIAL_AUTH_DISABLED_PROVIDERS (W0-D1)."""

    def _authenticate(self, provider):
        SocialAuthService().authenticate(
            provider=provider, token='token', app_type='client',
        )

    def test_default_setting_disables_vk_yandex(self, settings):
        assert 'vk' in settings.SOCIAL_AUTH_DISABLED_PROVIDERS
        assert 'yandex' in settings.SOCIAL_AUTH_DISABLED_PROVIDERS

    def test_explicit_list_disables_providers(self, settings):
        settings.SOCIAL_AUTH_DISABLED_PROVIDERS = ('vk', 'yandex')
        with pytest.raises(SocialProviderDisabledError):
            self._authenticate('vk')
        with pytest.raises(SocialProviderDisabledError):
            self._authenticate('yandex')

    def test_case_and_whitespace_normalized(self, settings):
        settings.SOCIAL_AUTH_DISABLED_PROVIDERS = (' VK ',)
        with pytest.raises(SocialProviderDisabledError):
            self._authenticate('vk')
        settings.SOCIAL_AUTH_DISABLED_PROVIDERS = ('YaNdEx',)
        with pytest.raises(SocialProviderDisabledError):
            self._authenticate('yandex')


class TestSocialAuthDisabledProvidersEnvParsing:
    """Env-backed parsing in djangoProject.settings.base is fail-closed:
    the ('vk', 'yandex') baseline survives absent, empty, and malformed
    env values, and the env can never remove the baseline (W0-D1;
    W0-D2 owns verified re-enable)."""

    ENV_VAR = 'SOCIAL_AUTH_DISABLED_PROVIDERS'

    def _reload_base(self, monkeypatch, value):
        import importlib
        import sys

        if value is None:
            monkeypatch.delenv(self.ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(self.ENV_VAR, value)
        sys.modules.pop('djangoProject.settings.base', None)
        return importlib.import_module('djangoProject.settings.base')

    def test_absent_env_keeps_baseline(self, monkeypatch):
        module = self._reload_base(monkeypatch, None)
        assert 'vk' in module.SOCIAL_AUTH_DISABLED_PROVIDERS
        assert 'yandex' in module.SOCIAL_AUTH_DISABLED_PROVIDERS

    def test_empty_env_keeps_baseline(self, monkeypatch):
        module = self._reload_base(monkeypatch, '')
        assert 'vk' in module.SOCIAL_AUTH_DISABLED_PROVIDERS
        assert 'yandex' in module.SOCIAL_AUTH_DISABLED_PROVIDERS

    def test_malformed_env_keeps_baseline(self, monkeypatch):
        module = self._reload_base(monkeypatch, '  , ;;; ,, ')
        assert 'vk' in module.SOCIAL_AUTH_DISABLED_PROVIDERS
        assert 'yandex' in module.SOCIAL_AUTH_DISABLED_PROVIDERS

    def test_env_cannot_remove_baseline(self, monkeypatch):
        # Even an env naming only other providers must NOT re-enable
        # vk/yandex; it can only EXTEND the denylist.
        module = self._reload_base(monkeypatch, 'google')
        assert 'vk' in module.SOCIAL_AUTH_DISABLED_PROVIDERS
        assert 'yandex' in module.SOCIAL_AUTH_DISABLED_PROVIDERS
        assert 'google' in module.SOCIAL_AUTH_DISABLED_PROVIDERS

    def test_env_entries_normalized(self, monkeypatch):
        module = self._reload_base(monkeypatch, ' VK ,  Yandex ')
        providers = module.SOCIAL_AUTH_DISABLED_PROVIDERS
        assert 'vk' in providers
        assert 'yandex' in providers
        assert all(p == p.strip().lower() for p in providers)
