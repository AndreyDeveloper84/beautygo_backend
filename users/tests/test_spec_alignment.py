"""Tests for DRF-207 API Contract Alignment (DRF-208, DRF-209).

Covers the move of:
- SpecialistReviewsView from /api/v1/reviews/specialists/{id}/
  to /api/v1/specialists/{id}/reviews/ (DRF-208).
- MasterMeView from /api/v1/auth/masters/me/
  to /api/v1/specialists/me/ (DRF-209).

Both legacy paths stay live as deprecated aliases and MUST return
`Deprecation: true` + `Sunset` headers.
"""
from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from users.models import SpecialistProfile, User

SPEC_ME_URL = '/api/v1/specialists/me/'
LEGACY_MASTER_ME_URL = '/api/v1/auth/masters/me/'


def _pro_client() -> APIClient:
    c = APIClient()
    c.defaults['HTTP_X_APP_TYPE'] = 'pro'
    return c


def _client_app() -> APIClient:
    c = APIClient()
    c.defaults['HTTP_X_APP_TYPE'] = 'client'
    return c


# ---------------------------------------------------------------------------
# DRF-209 — /api/v1/specialists/me/ GET/PATCH/POST
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSpecialistMe:
    @pytest.fixture
    def specialist(self) -> User:
        user = User.objects.create_user(
            username='spec_me', role='specialist',
            phone='+79990400001', is_verified=True,
        )
        profile = user.specialist_profile
        profile.display_name = 'Мастер Елена'
        profile.status = SpecialistProfile.ProfileStatus.ACTIVE
        profile.save()
        return user

    @pytest.fixture
    def auth_client(self, specialist: User) -> APIClient:
        c = _pro_client()
        c.force_authenticate(user=specialist)
        return c

    def test_get_returns_profile(self, auth_client, specialist):
        resp = auth_client.get(SPEC_ME_URL)
        assert resp.status_code == 200
        assert resp.json()['data']['display_name'] == 'Мастер Елена'

    def test_patch_updates_profile(self, auth_client, specialist):
        resp = auth_client.patch(
            SPEC_ME_URL, {'bio': 'Новое био'}, format='json',
        )
        assert resp.status_code == 200
        assert resp.json()['data']['bio'] == 'Новое био'
        specialist.specialist_profile.refresh_from_db()
        assert specialist.specialist_profile.bio == 'Новое био'

    def test_unauthenticated_returns_401(self):
        resp = _pro_client().get(SPEC_ME_URL)
        assert resp.status_code in (401, 403)

    def test_new_path_does_not_emit_deprecation_header(self, auth_client):
        resp = auth_client.get(SPEC_ME_URL)
        assert resp.headers.get('Deprecation') is None


@pytest.mark.django_db
class TestLegacyMasterMeDeprecation:
    """Legacy /api/v1/auth/masters/me/ still works but must signal deprecation."""

    @pytest.fixture
    def specialist(self) -> User:
        user = User.objects.create_user(
            username='spec_legacy', role='specialist',
            phone='+79990400002', is_verified=True,
        )
        user.specialist_profile.display_name = 'Legacy Мастер'
        user.specialist_profile.status = SpecialistProfile.ProfileStatus.ACTIVE
        user.specialist_profile.save()
        return user

    def test_legacy_path_still_returns_profile(self, specialist):
        c = _pro_client()
        c.force_authenticate(user=specialist)
        resp = c.get(LEGACY_MASTER_ME_URL)
        assert resp.status_code == 200
        assert resp.json()['data']['display_name'] == 'Legacy Мастер'

    def test_legacy_path_emits_deprecation_headers(self, specialist):
        c = _pro_client()
        c.force_authenticate(user=specialist)
        resp = c.get(LEGACY_MASTER_ME_URL)
        assert resp.headers.get('Deprecation') == 'true'
        assert 'May 2026' in resp.headers.get('Sunset', '')


# ---------------------------------------------------------------------------
# DRF-208 — /api/v1/specialists/{id}/reviews/ direct path
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSpecialistReviewsDirectPath:
    @pytest.fixture
    def specialist(self) -> User:
        user = User.objects.create_user(
            username='rev_direct', role='specialist',
            phone='+79990400003', is_verified=True,
        )
        user.specialist_profile.display_name = 'Review Master'
        user.specialist_profile.status = SpecialistProfile.ProfileStatus.ACTIVE
        user.specialist_profile.is_available = True
        user.specialist_profile.save()
        return user

    def test_direct_path_returns_empty_list(self, specialist):
        url = f'/api/v1/specialists/{specialist.specialist_profile.pk}/reviews/'
        resp = _client_app().get(url)
        assert resp.status_code == 200
        assert resp.json()['data'] == []

    def test_direct_path_unknown_specialist_returns_404(self):
        url = f'/api/v1/specialists/{uuid.uuid4()}/reviews/'
        resp = _client_app().get(url)
        assert resp.status_code == 404

    def test_direct_path_does_not_emit_deprecation_header(self, specialist):
        url = f'/api/v1/specialists/{specialist.specialist_profile.pk}/reviews/'
        resp = _client_app().get(url)
        assert resp.headers.get('Deprecation') is None

    def test_legacy_reviews_path_emits_deprecation_headers(self, specialist):
        url = f'/api/v1/reviews/specialists/{specialist.specialist_profile.pk}/'
        resp = _client_app().get(url)
        assert resp.status_code == 200
        assert resp.headers.get('Deprecation') == 'true'
        assert 'May 2026' in resp.headers.get('Sunset', '')
