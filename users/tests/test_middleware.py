import logging

import pytest
from django.urls import reverse
from rest_framework import status

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
