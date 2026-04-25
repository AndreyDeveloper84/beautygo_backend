"""Tests for DRF-220 deprecation of /auth/register + /auth/login.

The endpoints stay live (back-compat for mobile builds in the field)
but every response must announce its deprecation and every call must
land in the logs so we can measure traffic before removal.
"""
import logging

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient


@pytest.mark.django_db
class TestDeprecationHeaders:
    """Both legacy endpoints carry Deprecation: true + Sunset on every
    response, regardless of the underlying status code."""

    def setup_method(self):
        self.client = APIClient(headers={"X-App-Type": "client"})

    def test_register_response_carries_deprecation_headers(self):
        # Empty body — view returns 4xx, but headers must still be present
        resp = self.client.post(reverse("register"), {}, format="json")
        assert resp["Deprecation"] == "true"
        assert "Sunset" in resp
        # Sunset is an HTTP-date — sanity-check it parses as one
        assert "GMT" in resp["Sunset"]

    def test_register_success_path_carries_headers(self):
        resp = self.client.post(
            reverse("register"),
            {"phone": "+79991234567"},
            format="json",
        )
        # Could be 201 if registration succeeded or 400 if validation
        # bumped — either way the deprecation tag is mandatory.
        assert resp.status_code in (
            status.HTTP_201_CREATED,
            status.HTTP_400_BAD_REQUEST,
        )
        assert resp["Deprecation"] == "true"
        assert "Sunset" in resp

    def test_login_response_carries_deprecation_headers(self):
        resp = self.client.post(reverse("login"), {}, format="json")
        assert resp["Deprecation"] == "true"
        assert "Sunset" in resp


@pytest.mark.django_db
class TestDeprecationLogging:
    """Every call to a legacy endpoint emits a structured warning so
    ops can measure remaining traffic before flipping to a 410 Gone."""

    def setup_method(self):
        self.client = APIClient(headers={"X-App-Type": "client"})

    def test_register_logs_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="core.deprecation"):
            self.client.post(reverse("register"), {}, format="json")
        records = [
            r for r in caplog.records
            if r.name == "core.deprecation"
            and "deprecated_endpoint_called" in r.getMessage()
        ]
        assert len(records) == 1
        assert "/auth/register/" in records[0].getMessage()

    def test_login_logs_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="core.deprecation"):
            self.client.post(reverse("login"), {}, format="json")
        records = [
            r for r in caplog.records
            if r.name == "core.deprecation"
            and "deprecated_endpoint_called" in r.getMessage()
        ]
        assert len(records) == 1
        assert "/auth/login/" in records[0].getMessage()


@pytest.mark.django_db
class TestCanonicalEndpointsUntagged:
    """The new /request-otp/ + /verify-otp/ endpoints must NOT carry
    the deprecation headers — sanity-check the mixin only fires on
    legacy aliases."""

    def setup_method(self):
        self.client = APIClient(headers={"X-App-Type": "client"})

    def test_request_otp_clean_of_deprecation_header(self):
        resp = self.client.post(
            reverse("request-otp"),
            {"phone": "+79991234567"},
            format="json",
        )
        assert resp.get("Deprecation") is None
        assert resp.get("Sunset") is None

    def test_verify_otp_clean_of_deprecation_header(self):
        # Empty body → 400, but no deprecation header expected
        resp = self.client.post(reverse("verify-otp"), {}, format="json")
        assert resp.get("Deprecation") is None
        assert resp.get("Sunset") is None
