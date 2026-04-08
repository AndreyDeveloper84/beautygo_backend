"""Tests for DRF-173: Auth v2 — Anonymous JWT + OTP + session merge + onboarding."""
from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from users.models import AnonymousSession, User

ANON_URL = '/api/v1/auth/anonymous/'
ONBOARDING_URL = '/api/v1/auth/onboarding/'
VERIFY_OTP_URL = '/api/v1/auth/verify-otp/'
REQUEST_OTP_URL = '/api/v1/auth/request-otp/'
USER_ME_URL = '/api/v1/auth/users/me/'


def _client(app_type='client'):
    c = APIClient()
    c.defaults['HTTP_X_APP_TYPE'] = app_type
    return c


# ---------------------------------------------------------------------------
# POST /auth/anonymous/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestAnonymousAuth:
    def test_creates_anonymous_user_and_session(self):
        """First call creates guest user, AnonymousSession, returns JWT."""
        device_id = str(uuid.uuid4())
        resp = _client().post(ANON_URL, {
            'device_id': device_id,
            'platform': 'ios',
        }, format='json')

        assert resp.status_code == 201
        data = resp.json()['data']
        assert 'access' in data
        assert 'refresh' in data
        assert data['is_anonymous'] is True

        assert AnonymousSession.objects.filter(device_id=device_id).exists()
        session = AnonymousSession.objects.get(device_id=device_id)
        assert session.user.is_guest is True
        assert session.platform == 'ios'

    def test_idempotent_same_device(self):
        """Same device_id returns same guest user (no new user created)."""
        device_id = str(uuid.uuid4())
        _client().post(ANON_URL, {'device_id': device_id, 'platform': 'android'}, format='json')
        _client().post(ANON_URL, {'device_id': device_id, 'platform': 'android'}, format='json')

        assert AnonymousSession.objects.filter(device_id=device_id).count() == 1
        assert User.objects.filter(
            anonymous_session__device_id=device_id,
        ).count() == 1

    def test_different_devices_different_users(self):
        """Different device_ids get different guest users."""
        _client().post(ANON_URL, {'device_id': str(uuid.uuid4()), 'platform': 'ios'}, format='json')
        _client().post(ANON_URL, {'device_id': str(uuid.uuid4()), 'platform': 'ios'}, format='json')
        assert AnonymousSession.objects.count() == 2

    def test_missing_device_id_returns_400(self):
        resp = _client().post(ANON_URL, {'platform': 'ios'}, format='json')
        assert resp.status_code == 400

    def test_invalid_platform_returns_400(self):
        resp = _client().post(ANON_URL, {
            'device_id': str(uuid.uuid4()),
            'platform': 'windows',
        }, format='json')
        assert resp.status_code == 400

    def test_access_token_works_for_auth_endpoints(self):
        """Access token from anonymous session can authenticate requests."""
        device_id = str(uuid.uuid4())
        resp = _client().post(ANON_URL, {'device_id': device_id, 'platform': 'ios'}, format='json')
        access = resp.json()['data']['access']

        c = _client()
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        me_resp = c.get(USER_ME_URL)
        # Guest user is authenticated (200) but is_guest
        assert me_resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /auth/onboarding/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestOnboarding:
    @pytest.fixture
    def verified_user(self, db):
        return User.objects.create_user(
            username='onboarduser', role='client',
            phone='+79990005555', is_verified=True,
        )

    @pytest.fixture
    def auth_client(self, verified_user):
        c = _client()
        c.force_authenticate(user=verified_user)
        return c

    def test_onboarding_saves_name_and_flags(self, auth_client, verified_user):
        resp = auth_client.post(ONBOARDING_URL, {
            'first_name': 'Анна',
            'last_name': 'Иванова',
        }, format='json')
        assert resp.status_code == 200
        data = resp.json()['data']
        assert data['first_name'] == 'Анна'
        assert data['onboarding_completed'] is True

        verified_user.refresh_from_db()
        assert verified_user.onboarding_completed is True
        assert verified_user.first_name == 'Анна'

    def test_onboarding_saves_location(self, auth_client, verified_user):
        resp = auth_client.post(ONBOARDING_URL, {
            'first_name': 'Мария',
            'lat': '55.751244',
            'lon': '37.618423',
        }, format='json')
        assert resp.status_code == 200

        from users.models import Profile
        profile = Profile.objects.get(user=verified_user)
        assert profile.default_location_lat is not None

    def test_onboarding_without_last_name(self, auth_client, verified_user):
        resp = auth_client.post(ONBOARDING_URL, {'first_name': 'Ольга'}, format='json')
        assert resp.status_code == 200

    def test_onboarding_short_name_returns_400(self, auth_client):
        resp = auth_client.post(ONBOARDING_URL, {'first_name': 'A'}, format='json')
        assert resp.status_code == 400

    def test_onboarding_requires_auth(self):
        resp = _client().post(ONBOARDING_URL, {'first_name': 'Test'}, format='json')
        # 401 or 403 depending on middleware
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /auth/verify-otp/ — anonymous_token merge + onboarding_completed
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestVerifyOTPv2:
    def test_verify_returns_onboarding_completed(self, settings):
        settings.DEBUG = True
        settings.SMS_ENABLED = False
        phone = '+79990006666'

        # Register
        c = _client()
        c.post('/api/v1/auth/request-otp/', {'phone': phone}, format='json')

        resp = c.post(VERIFY_OTP_URL, {'phone': phone, 'code': '0000'}, format='json')
        assert resp.status_code == 200
        data = resp.json()['data']
        assert 'onboarding_completed' in data['user']
        assert data['user']['onboarding_completed'] is False

    def test_verify_with_valid_anonymous_token_merges_session(self, settings):
        """Valid anonymous_token provided → guest user deleted after merge."""
        settings.DEBUG = True
        settings.SMS_ENABLED = False

        # Create anonymous session
        device_id = str(uuid.uuid4())
        anon_resp = _client().post(ANON_URL, {'device_id': device_id, 'platform': 'ios'}, format='json')
        assert anon_resp.status_code == 201
        anon_refresh = anon_resp.json()['data']['refresh']
        anon_user_id = AnonymousSession.objects.get(device_id=device_id).user.id

        # Register real user
        phone = '+79990007777'
        _client().post(REQUEST_OTP_URL, {'phone': phone}, format='json')

        # Verify OTP with anonymous_token
        resp = _client().post(VERIFY_OTP_URL, {
            'phone': phone,
            'code': '0000',
            'anonymous_token': anon_refresh,
        }, format='json')
        assert resp.status_code == 200

        # Anonymous user and session should be deleted
        assert not User.objects.filter(id=anon_user_id).exists()
        assert not AnonymousSession.objects.filter(device_id=device_id).exists()

    def test_verify_with_invalid_anonymous_token_still_succeeds(self, settings):
        """Invalid anonymous_token is silently ignored — auth still succeeds."""
        settings.DEBUG = True
        settings.SMS_ENABLED = False
        phone = '+79990008888'
        _client().post(REQUEST_OTP_URL, {'phone': phone}, format='json')

        resp = _client().post(VERIFY_OTP_URL, {
            'phone': phone,
            'code': '0000',
            'anonymous_token': 'invalid.token.here',
        }, format='json')
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# DELETE /users/me — otp_code support
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestDeleteAccountV2:
    @pytest.fixture
    def active_user(self, db):
        return User.objects.create_user(
            username='deleteme', role='client',
            phone='+79990009999', is_verified=True,
        )

    def test_delete_without_otp_still_works(self, active_user):
        """Existing behavior: no otp_code required."""
        c = _client()
        c.force_authenticate(user=active_user)
        resp = c.delete(USER_ME_URL, {'reason': 'testing'}, format='json')
        assert resp.status_code == 200

    def test_delete_with_wrong_otp_returns_error(self, active_user, settings):
        """Wrong otp_code returns auth error."""
        settings.DEBUG = True
        settings.SMS_ENABLED = False

        # Send OTP first
        from users.services import OTPService
        OTPService().send_otp(active_user.phone)

        c = _client()
        c.force_authenticate(user=active_user)
        resp = c.delete(USER_ME_URL, {
            'otp_code': '9999',  # wrong code
        }, format='json')
        assert resp.status_code == 400

    def test_delete_with_correct_otp_succeeds(self, active_user, settings):
        """Correct otp_code allows deletion."""
        settings.DEBUG = True
        settings.SMS_ENABLED = False

        from users.services import OTPService
        OTPService().send_otp(active_user.phone)

        c = _client()
        c.force_authenticate(user=active_user)
        resp = c.delete(USER_ME_URL, {'otp_code': '0000'}, format='json')
        assert resp.status_code == 200
