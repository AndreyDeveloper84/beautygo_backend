"""Tests for /api/v1/health/ + /api/v1/health/ready/.

The endpoints exist because:

- A loadbalancer poll sees the process boot before migrations apply →
  serves traffic against an inconsistent schema → 500-fest. Readiness
  must distinguish "live" from "ready".
- A degraded DB or cache should pull the box out of rotation; "true if
  Django is up" is not enough.

Tests pin both the happy path and the degraded paths via mocks so we
catch the regression where a deploy ships with broken health checks
and no one notices until the alerting silence does.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

LIVE_URL = "/api/v1/health/"
READY_URL = "/api/v1/health/ready/"


@pytest.fixture
def anon():
    return APIClient()


@pytest.mark.django_db
class TestLiveness:
    def test_returns_200_with_envelope(self, anon):
        response = anon.get(LIVE_URL)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert "version" in body
        assert "timestamp" in body
        assert body["checks"]["db"]["ok"] is True
        assert body["checks"]["cache"]["ok"] is True

    def test_returns_503_when_db_down(self, anon):
        with patch(
            "djangoProject.health._check_db",
            return_value=(False, "error: OperationalError"),
        ):
            response = anon.get(LIVE_URL)
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unhealthy"
        assert body["checks"]["db"]["ok"] is False

    def test_returns_503_when_cache_down(self, anon):
        with patch(
            "djangoProject.health._check_cache",
            return_value=(False, "round-trip failed"),
        ):
            response = anon.get(LIVE_URL)
        assert response.status_code == 503
        assert response.json()["checks"]["cache"]["ok"] is False


@pytest.mark.django_db
class TestReadiness:
    def test_returns_200_when_migrations_applied(self, anon):
        response = anon.get(READY_URL)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["checks"]["migrations"]["ok"] is True

    def test_returns_503_when_migrations_pending(self, anon):
        with patch(
            "djangoProject.health._check_migrations",
            return_value=(False, "3 unapplied"),
        ):
            response = anon.get(READY_URL)
        assert response.status_code == 503
        assert response.json()["checks"]["migrations"]["ok"] is False
