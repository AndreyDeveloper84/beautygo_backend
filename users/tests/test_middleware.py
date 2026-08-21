import logging

import pytest
from django.urls import reverse
from rest_framework import status

from users import middleware as middleware_module
from users.middleware import (
    ACKNOWLEDGED_EXCLUSION_DIVERGENCE,
    TenantContextMiddleware,
)
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


class TestSalonExclusionIsNarrow:
    """DRF-1231 — the exemption covers the booking flow and stops there.

    Excluding a prefix does not «stop requiring X-App-Type» on it: the
    middleware sets ``request.app_type = None``, and ``IsProApp`` reads
    that attribute, so every view under an excluded prefix that still
    declares ``IsProApp`` becomes permanently unsatisfiable — for the
    bot, for a future Pro App, for everyone.

    Eleven routes live under ``/api/v1/tenants/me/``. DRF-1231 needed two
    of them. Excluding the shared parent would have quietly bricked the
    other nine — the day journal, closures, master schedules and access
    revocation — none of which anyone asked to change, and the breakage
    would only surface the first time someone pointed a working client at
    them. Hence two narrow prefixes instead of one broad one; this test
    is what keeps the next person from "simplifying" them back.
    """

    def test_the_booking_flow_is_exempt(self):
        excluded = middleware_module.EXCLUDED_PATH_PREFIXES
        assert "/api/v1/tenants/me/appointments/" in excluded
        assert "/api/v1/tenants/me/customers/" in excluded

    def test_the_rest_of_the_salon_surface_is_not(self):
        excluded = middleware_module.EXCLUDED_PATH_PREFIXES
        assert "/api/v1/tenants/me/" not in excluded
        assert "/api/v1/tenants/" not in excluded

        # Spot-check the routes that still rely on IsProApp being
        # satisfiable, i.e. the ones a broad exemption would have killed.
        for path in (
            "/api/v1/tenants/me/day/",
            "/api/v1/tenants/me/closures/",
            "/api/v1/tenants/me/masters/00000000-0000-0000-0000-000000000000/schedule/",
        ):
            assert not any(path.startswith(p) for p in excluded), path


class TestExclusionListDivergence:
    """DRF-1115 — AppTypeMiddleware and TenantContextMiddleware each keep
    their own list of excluded path prefixes: AppTypeMiddleware's is the
    module-level ``EXCLUDED_PATH_PREFIXES`` in users/middleware.py,
    TenantContextMiddleware's is its own class attribute of the same
    name. They're allowed to diverge (different headers, different
    legitimate exemptions) but not *silently* — every prefix in one list
    and not the other must be named in ACKNOWLEDGED_EXCLUSION_DIVERGENCE
    with a reason. This is the mechanization: it doesn't require the
    lists to match, it requires every mismatch to have been looked at by
    a human.

    No DB needed — this only touches the module-level tuples/dict.
    """

    def test_divergence_is_fully_acknowledged(self):
        app_type_only = set(middleware_module.EXCLUDED_PATH_PREFIXES) - set(
            TenantContextMiddleware.EXCLUDED_PATH_PREFIXES
        )
        tenant_only = set(TenantContextMiddleware.EXCLUDED_PATH_PREFIXES) - set(
            middleware_module.EXCLUDED_PATH_PREFIXES
        )
        diverged = app_type_only | tenant_only
        acknowledged = set(ACKNOWLEDGED_EXCLUSION_DIVERGENCE)

        unacknowledged = diverged - acknowledged
        assert not unacknowledged, (
            f"New/unacknowledged divergence between AppTypeMiddleware's and "
            f"TenantContextMiddleware's EXCLUDED_PATH_PREFIXES: {unacknowledged}. "
            "Either this is a legitimate, deliberate difference — add it to "
            "users.middleware.ACKNOWLEDGED_EXCLUSION_DIVERGENCE with a reason "
            "— or one of the two lists is missing an entry it should have."
        )

    def test_no_stale_acknowledgement(self):
        # An entry that stopped diverging (someone fixed the list, or
        # removed the prefix from both) should be deleted from the
        # registry — otherwise it silently hides the NEXT divergence at
        # the same prefix.
        app_type = set(middleware_module.EXCLUDED_PATH_PREFIXES)
        tenant = set(TenantContextMiddleware.EXCLUDED_PATH_PREFIXES)
        diverged = app_type ^ tenant

        stale = set(ACKNOWLEDGED_EXCLUSION_DIVERGENCE) - diverged
        assert not stale, (
            f"ACKNOWLEDGED_EXCLUSION_DIVERGENCE entries no longer diverge: "
            f"{stale}. Delete them from users.middleware."
        )


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
