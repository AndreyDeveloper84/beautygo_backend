import logging

import pytest
from django.urls import reverse
from rest_framework import status

from users.models import User

logger = logging.getLogger(__name__)


@pytest.mark.django_db
class TestAppTypeMiddleware:
    def test_missing_header_returns_403(self, api_client):
        """Request without X-App-Type header returns 403."""
        api_client.defaults.pop('HTTP_X_APP_TYPE', None)
        url = reverse('login')
        logger.info("POST %s without X-App-Type header", url)
        response = api_client.post(url, {'phone': '+79001234567'})
        logger.info("Response %s: %s", response.status_code, response.json())
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()['error']['code'] == 'APP_TYPE_MISSING'

    def test_invalid_header_returns_403(self, api_client):
        """Request with invalid X-App-Type value returns 403."""
        url = reverse('login')
        logger.info("POST %s with X-App-Type=invalid", url)
        response = api_client.post(url, {'phone': '+79001234567'}, HTTP_X_APP_TYPE='invalid')
        logger.info("Response %s: %s", response.status_code, response.json())
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()['error']['code'] == 'APP_TYPE_INVALID'

    def test_client_header_passes(self, api_client):
        """Request with X-App-Type: client passes middleware."""
        url = reverse('login')
        logger.info("POST %s with X-App-Type=client", url)
        response = api_client.post(url, {'phone': '+79009999999'}, HTTP_X_APP_TYPE='client')
        logger.info("Response %s (expected != 403)", response.status_code)
        assert response.status_code != status.HTTP_403_FORBIDDEN

    def test_pro_header_passes(self, api_client):
        """Request with X-App-Type: pro passes middleware."""
        url = reverse('login')
        logger.info("POST %s with X-App-Type=pro", url)
        response = api_client.post(url, {'phone': '+79009999999'}, HTTP_X_APP_TYPE='pro')
        logger.info("Response %s (expected != 403)", response.status_code)
        assert response.status_code != status.HTTP_403_FORBIDDEN

    def test_admin_bypasses_middleware(self, api_client):
        """Admin paths bypass the middleware."""
        api_client.defaults.pop('HTTP_X_APP_TYPE', None)
        logger.info("GET /admin/login/ without X-App-Type (should bypass)")
        response = api_client.get('/admin/login/')
        logger.info("Response %s (expected != 403)", response.status_code)
        assert response.status_code != status.HTTP_403_FORBIDDEN

    def test_health_bypasses_middleware(self, api_client):
        """Health check path bypasses middleware."""
        api_client.defaults.pop('HTTP_X_APP_TYPE', None)
        logger.info("GET /api/v1/health/ without X-App-Type (should bypass)")
        response = api_client.get('/api/v1/health/')
        logger.info("Response %s (expected != 403)", response.status_code)
        assert response.status_code != status.HTTP_403_FORBIDDEN

    def test_docs_bypasses_middleware(self, api_client):
        """API docs paths bypass middleware."""
        api_client.defaults.pop('HTTP_X_APP_TYPE', None)
        logger.info("GET /api/docs/ without X-App-Type (should bypass)")
        response = api_client.get('/api/docs/')
        logger.info("Response %s (expected != 403)", response.status_code)
        assert response.status_code != status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
class TestEndpointRestrictions:
    """Test that endpoints are restricted by X-App-Type."""

    def test_client_app_can_access_client_endpoint(self, api_client):
        """X-App-Type: client can access /clients/me/."""
        user = User.objects.create_user(
            username='cl1', password='pass', role='client',
            phone='+79003000001',
        )
        api_client.force_authenticate(user=user)
        response = api_client.get(
            '/api/v1/auth/clients/me/', HTTP_X_APP_TYPE='client',
        )
        logger.info("client app → /clients/me/ → %s", response.status_code)
        assert response.status_code == status.HTTP_200_OK

    def test_pro_app_cannot_access_client_endpoint(self, api_client):
        """X-App-Type: pro cannot access /clients/me/."""
        user = User.objects.create_user(
            username='cl2', password='pass', role='client',
            phone='+79003000002',
        )
        api_client.force_authenticate(user=user)
        response = api_client.get(
            '/api/v1/auth/clients/me/', HTTP_X_APP_TYPE='pro',
        )
        logger.info("pro app → /clients/me/ → %s", response.status_code)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_pro_app_can_access_services(self, api_client):
        """X-App-Type: pro can access /services/."""
        user = User.objects.create_user(
            username='sp1', password='pass', role='specialist',
            phone='+79003000003',
        )
        api_client.force_authenticate(user=user)
        response = api_client.get(
            '/api/v1/services/', HTTP_X_APP_TYPE='pro',
        )
        logger.info("pro app → /services/ → %s", response.status_code)
        assert response.status_code == status.HTTP_200_OK

    def test_client_app_cannot_access_services(self, api_client):
        """X-App-Type: client cannot access /services/ (pro only)."""
        user = User.objects.create_user(
            username='sp2', password='pass', role='specialist',
            phone='+79003000004',
        )
        api_client.force_authenticate(user=user)
        response = api_client.get(
            '/api/v1/services/', HTTP_X_APP_TYPE='client',
        )
        logger.info("client app → /services/ → %s", response.status_code)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_shared_endpoint_works_with_client(self, api_client):
        """Auth endpoints work with X-App-Type: client."""
        response = api_client.post(
            '/api/v1/auth/login/',
            {'phone': '+79009999999'},
            HTTP_X_APP_TYPE='client',
        )
        logger.info("client app → /login/ → %s", response.status_code)
        assert response.status_code != status.HTTP_403_FORBIDDEN

    def test_shared_endpoint_works_with_pro(self, api_client):
        """Auth endpoints work with X-App-Type: pro."""
        response = api_client.post(
            '/api/v1/auth/login/',
            {'phone': '+79009999999'},
            HTTP_X_APP_TYPE='pro',
        )
        logger.info("pro app → /login/ → %s", response.status_code)
        assert response.status_code != status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
class TestRequestIDMiddleware:
    """Every request gets an id; the id propagates to logs and to the
    response header so external traces can correlate with our log lines.
    Goes hand-in-hand with the structured-logging config in settings."""

    HEALTH_URL = '/api/v1/health/'

    def test_generates_uuid_when_header_absent(self, api_client):
        response = api_client.get(self.HEALTH_URL)
        request_id = response.headers.get('X-Request-ID')
        assert request_id, "Response missing X-Request-ID header"
        # 32-char hex (uuid4().hex). Loose check: hex-only, 32 chars.
        assert len(request_id) == 32
        assert all(c in '0123456789abcdef' for c in request_id)

    def test_respects_provided_header(self, api_client):
        provided = 'mobile-trace-1234567890'
        response = api_client.get(
            self.HEALTH_URL, HTTP_X_REQUEST_ID=provided,
        )
        assert response.headers.get('X-Request-ID') == provided

    def test_get_request_id_returns_sentinel_outside_request(self):
        """Outside of an active request the helper must not raise — it
        returns the '-' sentinel so the LOGGING formatter is safe in
        management commands and startup."""
        from core.log_filters import (
            NO_REQUEST_SENTINEL,
            clear_request_id,
            get_request_id,
        )

        clear_request_id()
        assert get_request_id() == NO_REQUEST_SENTINEL

    def test_each_request_gets_distinct_id(self, api_client):
        first = api_client.get(self.HEALTH_URL).headers['X-Request-ID']
        second = api_client.get(self.HEALTH_URL).headers['X-Request-ID']
        assert first != second
