"""Tests for DRF-194 — Portfolio CRUD on /specialists/me/portfolio/.

Covers happy paths, ownership, the 30-photo cap, MIME and size
validation, and that the public detail endpoint surfaces the same rows.
We use SimpleUploadedFile + a tiny inline JPEG so we don't depend on
any fixture file living in the repo.
"""
from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient

from users.models import SpecialistPortfolio, SpecialistProfile, User
from users.portfolio_api import (
    ALLOWED_CONTENT_TYPES,
    MAX_FILE_BYTES,
    MAX_PHOTOS_PER_SPECIALIST,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username='portspec',
        password='testpass123',
        role='specialist',
        phone='+79990003333',
    )


@pytest.fixture
def specialist_profile(specialist_user):
    profile = specialist_user.specialist_profile
    profile.display_name = 'Portfolio Master'
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.save(update_fields=['display_name', 'status'])
    return profile


@pytest.fixture
def other_specialist(db):
    user = User.objects.create_user(
        username='otherspec',
        password='testpass123',
        role='specialist',
        phone='+79990004444',
    )
    profile = user.specialist_profile
    profile.display_name = 'Other Master'
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.save(update_fields=['display_name', 'status'])
    return profile


@pytest.fixture
def pro_client(specialist_user, specialist_profile):
    client = APIClient()
    client.defaults['HTTP_X_APP_TYPE'] = 'pro'
    client.force_authenticate(user=specialist_user)
    return client


@pytest.fixture
def client_app(db):
    user = User.objects.create_user(
        username='portclient',
        password='testpass123',
        role='client',
        phone='+79990005555',
    )
    client = APIClient()
    client.defaults['HTTP_X_APP_TYPE'] = 'client'
    client.force_authenticate(user=user)
    return client


def _make_image(content_type: str = "image/jpeg", size_bytes: int = 1024) -> SimpleUploadedFile:
    """Build a tiny in-memory image with the requested content type."""
    fmt_map = {
        "image/jpeg": ("JPEG", "test.jpg"),
        "image/png": ("PNG", "test.png"),
        "image/webp": ("WEBP", "test.webp"),
    }
    pil_format, filename = fmt_map.get(content_type, ("JPEG", "test.jpg"))
    img = Image.new('RGB', (10, 10), color='red')
    buf = io.BytesIO()
    img.save(buf, format=pil_format)
    payload = buf.getvalue()
    if size_bytes > len(payload):
        # Pad — only used to test the size limit. Pillow won't decode the
        # padded bytes but the validation runs before any decode.
        payload = payload + b'\x00' * (size_bytes - len(payload))
    return SimpleUploadedFile(filename, payload, content_type=content_type)


PORTFOLIO_URL = '/api/v1/specialists/me/portfolio/'


# ---------------------------------------------------------------------------
# POST — upload
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPortfolioUpload:
    def test_upload_success_returns_201_with_image_url(self, pro_client, specialist_profile):
        resp = pro_client.post(
            PORTFOLIO_URL,
            {'image': _make_image()},
            format='multipart',
        )
        assert resp.status_code == 201
        body = resp.json()['data']
        assert 'id' in body
        assert body['image_url']  # non-empty URL
        assert body['sort_order'] == 0
        assert specialist_profile.portfolio.count() == 1

    @pytest.mark.parametrize("content_type", sorted(ALLOWED_CONTENT_TYPES))
    def test_upload_accepts_all_allowed_types(self, pro_client, content_type):
        resp = pro_client.post(
            PORTFOLIO_URL,
            {'image': _make_image(content_type=content_type)},
            format='multipart',
        )
        assert resp.status_code == 201

    def test_upload_rejects_unsupported_mime(self, pro_client):
        bad = SimpleUploadedFile("evil.svg", b"<svg/>", content_type="image/svg+xml")
        resp = pro_client.post(PORTFOLIO_URL, {'image': bad}, format='multipart')
        assert resp.status_code == 400
        assert resp.json()['error']['code'] == 'VALIDATION_ERROR'

    def test_upload_rejects_oversized_file(self, pro_client):
        oversized = _make_image(size_bytes=MAX_FILE_BYTES + 1)
        resp = pro_client.post(
            PORTFOLIO_URL, {'image': oversized}, format='multipart',
        )
        assert resp.status_code == 400
        assert 'too large' in resp.json()['error']['message'].lower()

    def test_upload_rejects_missing_image_field(self, pro_client):
        resp = pro_client.post(PORTFOLIO_URL, {}, format='multipart')
        assert resp.status_code == 400
        assert resp.json()['error']['code'] == 'VALIDATION_ERROR'

    def test_upload_rejects_at_30_photos(self, pro_client, specialist_profile):
        # Seed 30 portfolio rows with no real file — the model's image
        # field accepts an empty value during bulk_create. The view
        # checks count() before doing any S3 work.
        SpecialistPortfolio.objects.bulk_create([
            SpecialistPortfolio(specialist=specialist_profile, image='dummy.jpg')
            for _ in range(MAX_PHOTOS_PER_SPECIALIST)
        ])
        resp = pro_client.post(
            PORTFOLIO_URL, {'image': _make_image()}, format='multipart',
        )
        assert resp.status_code == 400
        assert resp.json()['error']['code'] == 'PORTFOLIO_LIMIT_EXCEEDED'


# ---------------------------------------------------------------------------
# GET — list
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPortfolioList:
    def test_list_returns_only_own_items(
        self, pro_client, specialist_profile, other_specialist,
    ):
        SpecialistPortfolio.objects.create(
            specialist=specialist_profile, image='mine.jpg', sort_order=0,
        )
        SpecialistPortfolio.objects.create(
            specialist=other_specialist, image='theirs.jpg', sort_order=0,
        )
        resp = pro_client.get(PORTFOLIO_URL)
        assert resp.status_code == 200
        items = resp.json()['data']['items']
        assert len(items) == 1

    def test_list_respects_sort_order(self, pro_client, specialist_profile):
        SpecialistPortfolio.objects.create(
            specialist=specialist_profile, image='c.jpg', sort_order=2,
        )
        SpecialistPortfolio.objects.create(
            specialist=specialist_profile, image='a.jpg', sort_order=0,
        )
        SpecialistPortfolio.objects.create(
            specialist=specialist_profile, image='b.jpg', sort_order=1,
        )
        resp = pro_client.get(PORTFOLIO_URL)
        items = resp.json()['data']['items']
        assert [i['sort_order'] for i in items] == [0, 1, 2]


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPortfolioDelete:
    def test_delete_own_returns_204_and_removes_row(
        self, pro_client, specialist_profile,
    ):
        item = SpecialistPortfolio.objects.create(
            specialist=specialist_profile, image='mine.jpg',
        )
        # Mock storage delete — the test backend doesn't have to round-trip
        # an actual file deletion to validate the view contract.
        with patch.object(item.image.storage, 'delete') as mock_del:
            resp = pro_client.delete(f'{PORTFOLIO_URL}{item.id}/')
        assert resp.status_code == 204
        assert not SpecialistPortfolio.objects.filter(id=item.id).exists()
        mock_del.assert_called_once()

    def test_delete_others_returns_404(
        self, pro_client, other_specialist,
    ):
        item = SpecialistPortfolio.objects.create(
            specialist=other_specialist, image='theirs.jpg',
        )
        resp = pro_client.delete(f'{PORTFOLIO_URL}{item.id}/')
        assert resp.status_code == 404
        # Row still exists — we didn't accidentally cascade
        assert SpecialistPortfolio.objects.filter(id=item.id).exists()


# ---------------------------------------------------------------------------
# PATCH — reorder
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPortfolioReorder:
    def test_patch_updates_sort_order(self, pro_client, specialist_profile):
        item = SpecialistPortfolio.objects.create(
            specialist=specialist_profile, image='mine.jpg', sort_order=0,
        )
        resp = pro_client.patch(
            f'{PORTFOLIO_URL}{item.id}/',
            {'sort_order': 5},
            format='json',
        )
        assert resp.status_code == 200
        item.refresh_from_db()
        assert item.sort_order == 5

    def test_patch_others_returns_404(self, pro_client, other_specialist):
        item = SpecialistPortfolio.objects.create(
            specialist=other_specialist, image='theirs.jpg',
        )
        resp = pro_client.patch(
            f'{PORTFOLIO_URL}{item.id}/',
            {'sort_order': 5},
            format='json',
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Auth + permission gates
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPortfolioAuthGates:
    def test_unauthenticated_returns_401(self):
        client = APIClient()
        client.defaults['HTTP_X_APP_TYPE'] = 'pro'
        resp = client.get(PORTFOLIO_URL)
        assert resp.status_code == 401

    def test_client_app_user_blocked(self, client_app):
        # A client-role user hitting /me/portfolio/ via the Pro endpoint
        # is rejected — IsProApp + IsSpecialist gate.
        resp = client_app.get(PORTFOLIO_URL)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Public detail card (DRF-61) surfaces portfolio
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPublicDetailIncludesPortfolio:
    def test_specialist_detail_returns_portfolio_list(
        self, client_app, specialist_profile,
    ):
        SpecialistPortfolio.objects.create(
            specialist=specialist_profile, image='one.jpg', sort_order=0,
        )
        SpecialistPortfolio.objects.create(
            specialist=specialist_profile, image='two.jpg', sort_order=1,
        )
        resp = client_app.get(f'/api/v1/specialists/{specialist_profile.id}/')
        assert resp.status_code == 200
        # SpecialistViewSet returns DRF-default body (not the success_response
        # envelope used by hand-written views) — assert against raw JSON.
        body = resp.json()
        assert 'portfolio' in body
        assert len(body['portfolio']) == 2
        # Ordering preserved (sort_order asc, then created_at)
        assert [item['sort_order'] for item in body['portfolio']] == [0, 1]
