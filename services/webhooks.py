"""S3-CAL.3 YClients busy webhook ingress (#1044 / EPIC #317, G-CalendarSync).

Inbound-only. The ONLY YClients-coupled module in S3-CAL — everything
downstream (provider, slot guard, recheck) is source-agnostic. Behind
``EXTERNAL_BUSY_ENABLED`` (inert when off). Verifies an HMAC-SHA256 signature
over the raw body (secret in env), resolves ``company_id``->tenant /
``staff_id``->specialist (via ``SpecialistProfile.yclients_*``), and idempotently
upserts ``ExternalBusyInterval``. Never touches appointments-write.

NB: the exact YClients signature scheme is not publicly specified; this uses the
industry-standard HMAC-SHA256-in-a-header pattern and is isolated in
``verify_yclients_signature`` so it can be adjusted against the live spec when
pilot license 884045 is active (live round-trip is coordinated then).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import timedelta, timezone as dt_timezone

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import permissions
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import SpecialistProfile

from .models import ExternalBusyInterval

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "X-YClients-Signature"
_DELETE_STATUSES = {"delete", "deleted"}


def verify_yclients_signature(request: Request) -> bool:
    """Constant-time HMAC-SHA256 check over the raw body. Fails closed."""
    secret = getattr(settings, "YCLIENTS_WEBHOOK_SECRET", "")
    if not secret:
        return False
    provided = request.headers.get(_SIGNATURE_HEADER, "")
    if not provided:
        return False
    expected = hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


class YClientsBusyWebhookView(APIView):
    """POST /api/v1/internal/catalog/yclients/busy-webhook/ — inbound busy sync."""

    authentication_classes: list = []
    permission_classes = [permissions.AllowAny]

    def post(self, request: Request) -> Response:
        # Inert when the feature is off — acknowledge without processing so a
        # misfired delivery does not error-storm YClients retries.
        if not getattr(settings, "EXTERNAL_BUSY_ENABLED", False):
            return Response({"status": "disabled"}, status=200)

        # Read request.body (for the signature) BEFORE request.data so DRF
        # caches the raw stream rather than raising RawPostDataException.
        if not verify_yclients_signature(request):
            return Response({"detail": "invalid signature"}, status=401)

        payload = request.data if isinstance(request.data, dict) else {}
        data = payload.get("data") or {}
        company_id = str(payload.get("company_id", ""))
        staff_id = str(data.get("staff_id", ""))
        external_id = str(data.get("id", "") or payload.get("resource_id", ""))

        specialist = (
            SpecialistProfile.objects
            .filter(yclients_staff_id=staff_id, yclients_company_id=company_id)
            .select_related("tenant")
            .first()
        )
        if specialist is None or specialist.tenant_id is None:
            logger.warning(
                "yclients_webhook.unresolved staff=%s company=%s", staff_id, company_id,
            )
            return Response({"status": "ignored"}, status=200)

        status_val = str(payload.get("status", "")).lower()
        if status_val in _DELETE_STATUSES or bool(data.get("deleted")):
            ExternalBusyInterval.objects.filter(
                source=ExternalBusyInterval.Source.YCLIENTS,
                external_id=external_id,
                tenant=specialist.tenant,
            ).delete()
            return Response({"status": "deleted"}, status=200)

        start_at = parse_datetime(str(data.get("datetime", "")))
        length = int(data.get("seance_length") or 0)
        if start_at is None or start_at.tzinfo is None or length <= 0:
            return Response({"detail": "invalid interval"}, status=400)

        start_utc = start_at.astimezone(dt_timezone.utc)
        end_utc = start_utc + timedelta(seconds=length)

        ExternalBusyInterval.objects.update_or_create(
            source=ExternalBusyInterval.Source.YCLIENTS,
            external_id=external_id,
            tenant=specialist.tenant,
            defaults={
                "specialist": specialist,
                "start_at": start_utc,
                "end_at": end_utc,
                "raw_payload": payload,
                "received_at": timezone.now(),
            },
        )
        return Response({"status": "ok"}, status=200)
