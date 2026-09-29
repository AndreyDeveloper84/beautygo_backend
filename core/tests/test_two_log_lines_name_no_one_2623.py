"""DRF-2623: two log lines that carried a person's words into Sentry breadcrumbs.

A salon's name (often its owner's) and a push's title/body (the client's and
the master's names, ``notifications/templates.py``) have no shape
``redact_pii`` can recognise; logged at INFO they became breadcrumbs of the
next Sentry event.

Each pair must differ in BOTH halves: the line still NAMES what it is about
(which salon; which notification to which device) — a line cut out entirely
would pass «no name in it» and lose the post-mortem — and it does NOT carry
the words.
"""

from __future__ import annotations

import logging

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from notifications.services.push import PushService, reset_app_state_for_tests
from tenants.models import Tenant

OWNER_SALON = "Салон Марии Петровой"
REQUESTED = "Студия Анны Смирновой"
CLIENT = "Ольга Кузнецова"
MASTER = "Ирина Волкова"
PROVISIONING = "test-tenant-provisioning-2623"  # pragma: allowlist secret


def _capture(caplog, name: str):
    """Attach caplog to the logger itself: app loggers may not propagate."""
    lg = logging.getLogger(name)
    lg.addHandler(caplog.handler)
    lg.setLevel(logging.INFO)
    return lg


@pytest.mark.django_db
def test_the_salon_is_named_by_its_slug_not_by_its_name(settings, caplog):
    settings.AYLA_TENANT_PROVISIONING_TOKEN = PROVISIONING
    settings.AYLA_IDENTITY_PROVISIONING_TOKEN = "test-identity-2623"  # pragma: allowlist secret
    settings.AYLA_INTERNAL_API_TOKEN = "test-general-2623"  # pragma: allowlist secret
    Tenant.objects.create(slug="salon-2623", name=OWNER_SALON, city="Пенза")
    lg = _capture(caplog, "tenants.internal_api")
    try:
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {PROVISIONING}")
        resp = client.post(
            "/api/v1/internal/tenants/",
            {"slug": "salon-2623", "name": REQUESTED, "city": "Пенза"},
            format="json",
        )
    finally:
        lg.removeHandler(caplog.handler)
    assert resp.status_code == 409, resp.content
    # one line, however many handlers saw it (attached + propagated)
    lines = sorted({r.getMessage() for r in caplog.records if "tenants.ensure.slug_taken" in r.getMessage()})
    assert len(lines) == 1, lines
    assert "slug=salon-2623" in lines[0]  # which salon — the post-mortem survives
    assert OWNER_SALON not in lines[0] and REQUESTED not in lines[0]


@override_settings(FIREBASE_CREDENTIALS_JSON="", FIREBASE_CREDENTIALS_PATH="")
def test_the_stub_push_says_which_notification_not_what_it_said(caplog):
    reset_app_state_for_tests()
    lg = _capture(caplog, "notifications.services.push")
    try:
        ok = PushService().send(
            token="device-token-2623-abcdef",
            title="Новая запись",
            body=f"{CLIENT} на маникюр к {MASTER}, 10:00",
            data={"notification_id": "n-2623", "template_id": "booking_new_master", "deep_link": "x"},
        )
    finally:
        lg.removeHandler(caplog.handler)
        reset_app_state_for_tests()
    assert ok is True
    lines = sorted({r.getMessage() for r in caplog.records if r.getMessage().startswith("push.stub")})
    assert len(lines) == 1, lines
    line = lines[0]
    # which notification, to which device — enough to rebuild the text from the row
    assert "template=booking_new_master" in line and "notification=n-2623" in line
    assert "token=device-toke" in line
    # and not what it said
    assert CLIENT not in line and MASTER not in line and "Новая запись" not in line
