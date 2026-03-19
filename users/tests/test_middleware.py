import pytest
from django.test import RequestFactory
from django.urls import reverse
from rest_framework import status


@pytest.mark.django_db
class TestAppTypeMiddleware:
    def test_missing_header_returns_403(self, api_client):
        """Request without X-App-Type header returns 403."""
        api_client.defaults.pop('HTTP_X_APP_TYPE', None)
        url = reverse('login')
        response = api_client.post(url, {'phone': '+79001234567'})
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()['error']['code'] == 'APP_TYPE_MISSING'

    def test_invalid_header_returns_403(self, api_client):
        """Request with invalid X-App-Type value returns 403."""
        url = reverse('login')
        response = api_client.post(url, {'phone': '+79001234567'}, HTTP_X_APP_TYPE='invalid')
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()['error']['code'] == 'APP_TYPE_INVALID'

    def test_client_header_passes(self, api_client):
        """Request with X-App-Type: client passes middleware."""
        url = reverse('login')
        response = api_client.post(url, {'phone': '+79009999999'}, HTTP_X_APP_TYPE='client')
        # Should not be 403 (will be 404 because user not found, which is fine)
        assert response.status_code != status.HTTP_403_FORBIDDEN

    def test_pro_header_passes(self, api_client):
        """Request with X-App-Type: pro passes middleware."""
        url = reverse('login')
        response = api_client.post(url, {'phone': '+79009999999'}, HTTP_X_APP_TYPE='pro')
        assert response.status_code != status.HTTP_403_FORBIDDEN

    def test_admin_bypasses_middleware(self, api_client):
        """Admin paths bypass the middleware."""
        api_client.defaults.pop('HTTP_X_APP_TYPE', None)
        response = api_client.get('/admin/login/')
        # Should not be 403 (could be 200 or redirect, but not 403)
        assert response.status_code != status.HTTP_403_FORBIDDEN

    def test_health_bypasses_middleware(self, api_client):
        """Health check path bypasses middleware."""
        api_client.defaults.pop('HTTP_X_APP_TYPE', None)
        response = api_client.get('/api/v1/health/')
        assert response.status_code != status.HTTP_403_FORBIDDEN

    def test_docs_bypasses_middleware(self, api_client):
        """API docs paths bypass middleware."""
        api_client.defaults.pop('HTTP_X_APP_TYPE', None)
        response = api_client.get('/api/docs/')
        assert response.status_code != status.HTTP_403_FORBIDDEN
