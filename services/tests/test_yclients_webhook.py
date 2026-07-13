"""S3-CAL.3 YClients busy webhook ingress — TDD (mocked payloads).

Inbound-only. Behind EXTERNAL_BUSY_ENABLED (inert when off). Verifies an
HMAC-SHA256 signature (secret in env), resolves company_id->tenant /
staff_id->specialist, and idempotently upserts ExternalBusyInterval. Does not
touch appointments-write. Live round-trip is coordinated at license activation.
Spec: docs/CATALOG_EXTERNAL_BUSY_S3CAL_DESIGN_2026-07.md §6.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from rest_framework.test import APIClient

from services.models import ExternalBusyInterval
from tenants.models import Tenant
from users.models import SpecialistProfile, User

WEBHOOK_URL = "/api/v1/internal/catalog/yclients/busy-webhook/"
SECRET = "test-yclients-webhook-secret"


@pytest.fixture(autouse=True)
def _enable(settings):
    settings.EXTERNAL_BUSY_ENABLED = True
    settings.YCLIENTS_WEBHOOK_SECRET = SECRET


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="ycw-t", name="YCW Tenant")


@pytest.fixture
def specialist(db, tenant):
    u = User.objects.create_user(
        username="ycw_spec", password="x", role="specialist",
        phone="+79995707100",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.yclients_company_id = "884045"
    p.yclients_staff_id = "9100"
    p.save()
    return p


def _payload(*, record_id=55501, staff_id=9100, company_id=884045,
             dt="2026-08-01T10:00:00+03:00", length=3600,
             status="create", deleted=False):
    return {
        "company_id": company_id,
        "resource": "record",
        "status": status,
        "resource_id": record_id,
        "data": {
            "id": record_id,
            "staff_id": staff_id,
            "datetime": dt,
            "seance_length": length,
            "deleted": deleted,
        },
    }


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(payload, *, sign=True, secret=SECRET):
    body = json.dumps(payload)
    raw = body.encode()
    headers = {}
    if sign:
        headers["HTTP_X_YCLIENTS_SIGNATURE"] = _sign(raw, secret)
    return APIClient().post(
        WEBHOOK_URL, data=body, content_type="application/json", **headers,
    )


@pytest.mark.django_db
class TestYClientsBusyWebhook:
    def test_valid_webhook_creates_interval(self, specialist, tenant):
        r = _post(_payload())
        assert r.status_code == 200, r.content
        rows = ExternalBusyInterval.objects.all()
        assert rows.count() == 1
        b = rows.first()
        assert b.source == "yclients"
        assert b.external_id == "55501"
        assert b.tenant_id == tenant.id
        assert b.specialist_id == specialist.id
        # 10:00 +03:00 -> 07:00Z, 3600s -> 08:00Z
        assert b.start_at.isoformat() == "2026-08-01T07:00:00+00:00"
        assert b.end_at.isoformat() == "2026-08-01T08:00:00+00:00"

    def test_idempotent_redelivery(self, specialist):
        _post(_payload())
        _post(_payload())  # same record id
        assert ExternalBusyInterval.objects.count() == 1

    def test_update_moves_interval(self, specialist):
        _post(_payload(dt="2026-08-01T10:00:00+03:00"))
        _post(_payload(status="update", dt="2026-08-01T12:00:00+03:00"))
        assert ExternalBusyInterval.objects.count() == 1
        b = ExternalBusyInterval.objects.first()
        assert b.start_at.isoformat() == "2026-08-01T09:00:00+00:00"

    def test_delete_removes_interval(self, specialist):
        _post(_payload())
        assert ExternalBusyInterval.objects.count() == 1
        r = _post(_payload(status="delete", deleted=True))
        assert r.status_code == 200
        assert ExternalBusyInterval.objects.count() == 0

    def test_invalid_signature_rejected(self, specialist):
        r = _post(_payload(), secret="wrong-secret")
        assert r.status_code == 401
        assert ExternalBusyInterval.objects.count() == 0

    def test_missing_signature_rejected(self, specialist):
        r = _post(_payload(), sign=False)
        assert r.status_code == 401
        assert ExternalBusyInterval.objects.count() == 0

    def test_unknown_staff_noop(self, specialist):
        r = _post(_payload(staff_id=99999))
        # acknowledged (no retry storm) but nothing written
        assert r.status_code in (200, 202)
        assert ExternalBusyInterval.objects.count() == 0

    def test_flag_off_is_inert(self, settings, specialist):
        settings.EXTERNAL_BUSY_ENABLED = False
        r = _post(_payload())
        assert r.status_code == 200
        assert ExternalBusyInterval.objects.count() == 0
