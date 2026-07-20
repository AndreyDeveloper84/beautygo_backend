"""C2 — internal billing status endpoint (PILOT_CONTRACTS §3).

``GET /api/v1/internal/billing/specialists/{specialist_id}/status/`` —
Bearer ``AYLA_INTERNAL_API_TOKEN``. ``specialist_id`` is the Ayla **User
UUID** (AMD-005); resolution user → SpecialistProfile happens here.

URL mounting is W1-owned (djangoProject/urls.py, P3 — done); tests run
against the canonical settings.
"""
from __future__ import annotations

import logging
from uuid import UUID

from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.charges import (
    NoDebtError,
    find_outstanding_invoice,
    pay_debt,
    start_card_setup,
)
from billing.services import build_billing_status, resolve_billing_account
from billing.yookassa import BillingPaymentClientError, BillingPaymentConfigError
from users.models import SpecialistProfile
from users.permissions import IsInternalBearer
from users.response import error_response, success_response

logger = logging.getLogger(__name__)


class BillingSpecialistStatusView(APIView):
    """C2: subscription status + pending fees + last invoice for a specialist."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]

    @extend_schema(
        operation_id="internal_billing_specialist_status",
        tags=["internal", "billing"],
        responses={
            200: inline_serializer(
                name="InternalBillingStatus",
                fields={"data": serializers.DictField()},
            ),
            401: OpenApiResponse(description="Missing / invalid bearer token"),
            404: OpenApiResponse(description="Specialist does not exist"),
        },
        description=(
            "Billing status for the bot/miniapp (C2). 200 always for an "
            "existing specialist — no account yields status=none with "
            "null fields and zeroed fees. 404 only when the specialist "
            "does not exist (SPECIALIST_NOT_FOUND)."
        ),
    )
    def get(self, request: Request, specialist_id: UUID) -> Response:
        specialist = (
            SpecialistProfile.objects
            .filter(user_id=specialist_id)
            .select_related("user")
            .first()
        )
        if specialist is None:
            # C2: 404 ONLY for a non-existent specialist; an existing
            # specialist without billing data is 200 with nulls.
            logger.info(
                "internal.billing.specialist_not_found user_id=%s request_id=%s",
                specialist_id, getattr(request, "request_id", "-"),
            )
            return error_response(
                "SPECIALIST_NOT_FOUND", "Specialist not found.", status_code=404,
            )
        return success_response(build_billing_status(specialist))


class BillingCardSetupView(APIView):
    """Start card binding for a specialist (D7): first payment with
    save_payment_method:true → confirmation_url for the miniapp (W4).

    Body: {"tariff": "solo"|"salon", "return_url": "<https url>"}.
    Salon tariff binds the tenant's account (specialist's current
    tenant). Idempotent per day (see billing.charges.start_card_setup).
    """

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]

    @extend_schema(
        operation_id="internal_billing_card_setup",
        tags=["internal", "billing"],
        request=inline_serializer(
            name="InternalBillingCardSetupRequest",
            fields={
                "tariff": serializers.ChoiceField(choices=["solo", "salon"]),
                "return_url": serializers.CharField(),
            },
        ),
        responses={
            200: inline_serializer(
                name="InternalBillingCardSetupResponse",
                fields={"data": serializers.DictField()},
            ),
            400: OpenApiResponse(description="Validation error"),
            401: OpenApiResponse(description="Missing / invalid bearer token"),
            404: OpenApiResponse(description="Specialist does not exist"),
        },
    )
    def post(self, request: Request, specialist_id: UUID) -> Response:
        specialist = (
            SpecialistProfile.objects
            .filter(user_id=specialist_id)
            .select_related("user", "tenant")
            .first()
        )
        if specialist is None:
            return error_response(
                "SPECIALIST_NOT_FOUND", "Specialist not found.", status_code=404,
            )

        tariff_code = (request.data or {}).get("tariff")
        return_url = (request.data or {}).get("return_url") or ""
        if tariff_code not in ("solo", "salon") or not return_url:
            return error_response(
                "VALIDATION_ERROR",
                "Body must include tariff (solo|salon) and return_url.",
            )

        tenant = None
        if tariff_code == "salon":
            tenant = specialist.tenant
            if tenant is None:
                return error_response(
                    "VALIDATION_ERROR",
                    "Salon tariff requires the specialist to belong to a tenant.",
                )

        try:
            result = start_card_setup(
                user=specialist.user,
                tariff_code=tariff_code,
                tenant=tenant,
                return_url=return_url,
            )
        except BillingPaymentConfigError as exc:
            logger.error("billing.card_setup.config_error: %s", exc)
            return error_response(
                "PAYMENT_PROVIDER_ERROR", "Payment provider not configured.",
                status_code=503,
            )
        except BillingPaymentClientError as exc:
            logger.error("billing.card_setup.client_error: %s", exc)
            return error_response(
                "PAYMENT_PROVIDER_ERROR", "Payment provider error.",
                status_code=502,
            )
        return success_response({
            "subscription_id": str(result.subscription_id),
            "invoice_id": str(result.invoice_id),
            "confirmation_url": result.confirmation_url,
        })


class BillingPayDebtView(APIView):
    """One-shot debt collection for a past_due subscription (W4 CTA).

    POST body: {"return_url": "<https url>"} — required only when the
    subscription has NO saved payment method (redirect re-binding).
    With a saved method the charge is instant (confirmation_url null).
    409 NO_DEBT when there is no outstanding invoice.
    """

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]

    @extend_schema(
        operation_id="internal_billing_pay_debt",
        tags=["internal", "billing"],
        request=inline_serializer(
            name="InternalBillingPayDebtRequest",
            fields={"return_url": serializers.CharField(required=False)},
        ),
        responses={
            200: inline_serializer(
                name="InternalBillingPayDebtResponse",
                fields={"data": serializers.DictField()},
            ),
            400: OpenApiResponse(description="return_url required (no saved card)"),
            401: OpenApiResponse(description="Missing / invalid bearer token"),
            404: OpenApiResponse(description="Specialist does not exist"),
            409: OpenApiResponse(description="No outstanding debt (NO_DEBT)"),
        },
    )
    def post(self, request: Request, specialist_id: UUID) -> Response:
        specialist = (
            SpecialistProfile.objects
            .filter(user_id=specialist_id)
            .select_related("user", "tenant")
            .first()
        )
        if specialist is None:
            return error_response(
                "SPECIALIST_NOT_FOUND", "Specialist not found.", status_code=404,
            )
        subscription = resolve_billing_account(
            user_id=specialist.user_id, tenant_id=specialist.tenant_id,
        )
        if subscription is None:
            return error_response(
                "NO_DEBT", "No billing account — no debt.", status_code=409,
            )

        # Debt first (409), input validation second — a client asking to
        # pay a non-existent debt should not need a return_url.
        if find_outstanding_invoice(subscription) is None:
            return error_response(
                "NO_DEBT", "Subscription has no outstanding debt.", status_code=409,
            )
        return_url = (request.data or {}).get("return_url") or ""
        if not subscription.payment_method_id and not return_url:
            return error_response(
                "VALIDATION_ERROR",
                "return_url is required when no payment method is saved.",
            )

        try:
            result = pay_debt(subscription=subscription, return_url=return_url)
        except NoDebtError:
            return error_response(
                "NO_DEBT", "Subscription has no outstanding debt.", status_code=409,
            )
        except BillingPaymentConfigError as exc:
            logger.error("billing.pay_debt.config_error: %s", exc)
            return error_response(
                "PAYMENT_PROVIDER_ERROR", "Payment provider not configured.",
                status_code=503,
            )
        except BillingPaymentClientError as exc:
            logger.error("billing.pay_debt.client_error: %s", exc)
            return error_response(
                "PAYMENT_PROVIDER_ERROR", "Payment provider error.",
                status_code=502,
            )

        subscription.refresh_from_db(fields=["status"])
        return success_response({
            "payment_id": str(result.payment_id),
            "invoice_id": str(result.invoice_id),
            "provider_payment_id": result.provider_payment_id,
            "confirmation_url": result.confirmation_url or None,
            "amount": f"{result.amount:.2f}",
            "status": result.status,
            "subscription_status": subscription.status,
        })
