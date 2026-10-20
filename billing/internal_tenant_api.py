"""DRF-2984 C3 — the cabinet's billing surface for the bot (contract §6).

``/api/v1/internal/billing/tenants/{tenant_id}/…`` — Bearer
``AYLA_INTERNAL_API_TOKEN``. The account is the tenant's: the plan follows
``Tenant.kind``, the masters are counted here, the bot sends neither.

Second factor (В-8) on the money decisions — card-setup, cancel,
convert-to-salon: ``X-External-User-ID`` must name an admin of this tenant
(:class:`billing.permissions.IsInternalBearerForTenantAdminSubject`).

With ``BILLING_ENABLED`` off every door answers 404 ``BILLING_DISABLED``
before touching a row; with ``BILLING_CHARGES_ENABLED`` off card-setup and
pay-debt answer 503 ``BILLING_CHARGES_DISABLED`` (C1).
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from uuid import UUID

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.charges import (
    AlreadySalon,
    NoDebtError,
    NoTenantAdmin,
    PriceMismatch,
    _offer_version,
    cancel_effective_on,
    cancel_subscription,
    convert_to_salon,
    ensure_tenant_subscription,
    find_outstanding_invoice,
    pay_debt,
    start_card_setup,
)
from billing.flags import BillingChargesDisabled, billing_enabled
from billing.internal_api import _charges_disabled_response, _is_https_url
from billing.models import SpecialistSubscription
from billing.permissions import IsInternalBearerForTenantAdminSubject
from billing.pricing import period_price
from billing.yookassa import BillingPaymentClientError, BillingPaymentConfigError
from users.permissions import IsInternalBearer
from users.response import error_response, success_response

logger = logging.getLogger(__name__)


def _disabled() -> Response:
    return error_response(
        "BILLING_DISABLED", "Billing is switched off.", status_code=404,
    )


def _tenant(tenant_id: UUID):
    from tenants.models import Tenant

    return Tenant.objects.filter(pk=tenant_id).first()


def _tenant_not_found() -> Response:
    return error_response("TENANT_NOT_FOUND", "Tenant not found.", status_code=404)


def _no_subscription() -> Response:
    return error_response(
        "SUBSCRIPTION_NOT_FOUND", "The tenant has no billing account.",
        status_code=404,
    )


def _money(value) -> str | None:
    return None if value is None else f"{value:.2f}"


def _date(value) -> str | None:
    return None if value is None else value.isoformat()


def _subscription_payload(subscription: SpecialistSubscription) -> dict:
    price = period_price(subscription)
    canceled = subscription.status == SpecialistSubscription.Status.CANCELED
    return {
        "status": subscription.status,
        "tariff": subscription.tariff.code,
        "trial_ends_at": _date(subscription.trial_ends_at),
        "current_period_end": _date(subscription.current_period_end),
        "frozen_at": (
            subscription.frozen_at.isoformat() if subscription.frozen_at else None
        ),
        "frozen_reason": subscription.frozen_reason or None,
        "cancel_effective_at": (
            _date(cancel_effective_on(subscription)) if canceled else None
        ),
        "pending_tariff": (
            subscription.pending_tariff.code if subscription.pending_tariff_id else None
        ),
        "pending_tariff_from": _date(subscription.pending_tariff_from),
        "card": (
            {"last4": subscription.card_last4, "brand": subscription.card_brand}
            if subscription.payment_method_id else None
        ),
        "masters_count": price.masters_count,
        "extra_masters_amount": _money(price.extra_masters_amount),
        "price_preview": _money(price.total),
    }


def _idempotency_key(request: Request) -> str:
    value = (request.data or {}).get("idempotency_key")
    return value.strip() if isinstance(value, str) else ""


class _TenantBillingView(APIView):
    authentication_classes: list = []
    permission_classes = [IsInternalBearer]


class _TenantAdminBillingView(APIView):
    authentication_classes: list = []
    permission_classes = [IsInternalBearerForTenantAdminSubject]
    subject_url_kwarg = "tenant_id"


class TenantSubscriptionView(_TenantBillingView):
    """POST — open the cabinet account (onboarding); idempotent."""

    @extend_schema(
        operation_id="internal_billing_tenant_subscription",
        tags=["internal", "billing"],
        responses={
            200: OpenApiResponse(description="Already open — same answer"),
            201: OpenApiResponse(description="Opened"),
            404: OpenApiResponse(description="BILLING_DISABLED / TENANT_NOT_FOUND"),
            409: OpenApiResponse(description="NO_TENANT_ADMIN"),
        },
    )
    def post(self, request: Request, tenant_id: UUID) -> Response:
        if not billing_enabled():
            return _disabled()
        if not _idempotency_key(request):
            return error_response("VALIDATION_ERROR", "idempotency_key is required.")
        tenant = _tenant(tenant_id)
        if tenant is None:
            return _tenant_not_found()
        try:
            subscription, created = ensure_tenant_subscription(tenant)
        except NoTenantAdmin:
            return error_response(
                "NO_TENANT_ADMIN", "The tenant has no active admin to pay.",
                status_code=409,
            )
        payload = _subscription_payload(subscription)
        return success_response(payload, status_code=201 if created else 200)


class TenantBillingStatusView(_TenantBillingView):
    """GET — the billing screen."""

    @extend_schema(
        operation_id="internal_billing_tenant_status",
        tags=["internal", "billing"],
        responses={
            200: OpenApiResponse(description="Status; subscription null when none"),
            404: OpenApiResponse(description="BILLING_DISABLED / TENANT_NOT_FOUND"),
        },
    )
    def get(self, request: Request, tenant_id: UUID) -> Response:
        if not billing_enabled():
            return _disabled()
        tenant = _tenant(tenant_id)
        if tenant is None:
            return _tenant_not_found()
        subscription = (
            SpecialistSubscription.objects
            .filter(tenant=tenant).select_related("tariff", "pending_tariff").first()
        )
        return success_response({
            "tenant_id": str(tenant.pk),
            "subscription": (
                _subscription_payload(subscription) if subscription else None
            ),
        })


class TenantCardSetupView(_TenantAdminBillingView):
    """POST {return_url, consent_version} — bind the card (second factor)."""

    @extend_schema(
        operation_id="internal_billing_tenant_card_setup",
        tags=["internal", "billing"],
        responses={
            200: OpenApiResponse(description="{confirmation_url}"),
            400: OpenApiResponse(description="Validation error"),
            404: OpenApiResponse(description="BILLING_DISABLED / not an admin of it"),
            409: OpenApiResponse(description="CONSENT_VERSION_MISMATCH / NO_TENANT_ADMIN"),
            503: OpenApiResponse(description="BILLING_CHARGES_DISABLED"),
        },
    )
    def post(self, request: Request, tenant_id: UUID) -> Response:
        if not billing_enabled():
            return _disabled()
        data = request.data or {}
        return_url = data.get("return_url") or ""
        if not _is_https_url(return_url):
            return error_response(
                "VALIDATION_ERROR", "return_url must be an https URL.",
            )
        # The catalog records its own offer version; the bot's value is
        # checked, not trusted. Empty is accepted until the offer exists (В-9).
        consent_version = data.get("consent_version") or ""
        if consent_version and consent_version != _offer_version():
            return error_response(
                "CONSENT_VERSION_MISMATCH",
                "The offer version the user agreed to is not the current one.",
                status_code=409,
            )
        tenant = request.billing_tenant
        try:
            subscription, _ = ensure_tenant_subscription(tenant)
            result = start_card_setup(
                user=subscription.user, tariff_code=subscription.tariff.code,
                tenant=tenant, return_url=return_url,
            )
        except NoTenantAdmin:
            return error_response(
                "NO_TENANT_ADMIN", "The tenant has no active admin to pay.",
                status_code=409,
            )
        except BillingChargesDisabled:
            return _charges_disabled_response()
        except BillingPaymentConfigError as exc:
            logger.error("billing.tenant_card_setup.config_error: %s", exc)
            return error_response(
                "PAYMENT_PROVIDER_ERROR", "Payment provider not configured.",
                status_code=503,
            )
        except BillingPaymentClientError as exc:
            logger.error("billing.tenant_card_setup.client_error: %s", exc)
            return error_response(
                "PAYMENT_PROVIDER_ERROR", "Payment provider error.", status_code=502,
            )
        return success_response({"confirmation_url": result.confirmation_url})


class TenantPayDebtView(_TenantBillingView):
    """POST {return_url} — collect the debt of a frozen/past-due account."""

    @extend_schema(
        operation_id="internal_billing_tenant_pay_debt",
        tags=["internal", "billing"],
        responses={
            200: OpenApiResponse(description="Payment started / charged"),
            404: OpenApiResponse(description="BILLING_DISABLED / not found"),
            409: OpenApiResponse(description="NO_DEBT"),
            503: OpenApiResponse(description="BILLING_CHARGES_DISABLED"),
        },
    )
    def post(self, request: Request, tenant_id: UUID) -> Response:
        if not billing_enabled():
            return _disabled()
        tenant = _tenant(tenant_id)
        if tenant is None:
            return _tenant_not_found()
        subscription = SpecialistSubscription.objects.filter(tenant=tenant).first()
        if subscription is None:
            return _no_subscription()
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
        if return_url and not _is_https_url(return_url):
            return error_response(
                "VALIDATION_ERROR", "return_url must be an https URL.",
            )
        try:
            result = pay_debt(subscription=subscription, return_url=return_url)
        except NoDebtError:
            return error_response(
                "NO_DEBT", "Subscription has no outstanding debt.", status_code=409,
            )
        except BillingChargesDisabled:
            return _charges_disabled_response()
        except BillingPaymentConfigError as exc:
            logger.error("billing.tenant_pay_debt.config_error: %s", exc)
            return error_response(
                "PAYMENT_PROVIDER_ERROR", "Payment provider not configured.",
                status_code=503,
            )
        except BillingPaymentClientError as exc:
            logger.error("billing.tenant_pay_debt.client_error: %s", exc)
            return error_response(
                "PAYMENT_PROVIDER_ERROR", "Payment provider error.", status_code=502,
            )
        subscription.refresh_from_db(fields=["status"])
        return success_response({
            "payment_id": str(result.payment_id),
            "invoice_id": str(result.invoice_id),
            "confirmation_url": result.confirmation_url or None,
            "amount": f"{result.amount:.2f}",
            "status": result.status,
            "subscription_status": subscription.status,
        })


class TenantCancelView(_TenantAdminBillingView):
    """POST {idempotency_key, reason?} — stop renewal (second factor)."""

    @extend_schema(
        operation_id="internal_billing_tenant_cancel",
        tags=["internal", "billing"],
        responses={
            200: OpenApiResponse(description="{status: canceled, effective_at}"),
            404: OpenApiResponse(description="BILLING_DISABLED / not found"),
        },
    )
    def post(self, request: Request, tenant_id: UUID) -> Response:
        if not billing_enabled():
            return _disabled()
        if not _idempotency_key(request):
            return error_response("VALIDATION_ERROR", "idempotency_key is required.")
        subscription = SpecialistSubscription.objects.filter(
            tenant=request.billing_tenant,
        ).first()
        if subscription is None:
            return _no_subscription()
        effective_on = cancel_subscription(subscription)
        logger.info(
            "billing.tenant_cancel tenant=%s effective_at=%s",
            request.billing_tenant.pk, effective_on,
        )
        return success_response({
            "status": SpecialistSubscription.Status.CANCELED,
            "effective_at": effective_on.isoformat(),
        })


class TenantConvertToSalonView(_TenantAdminBillingView):
    """POST {idempotency_key, confirmed_price} — solo → salon (В-2)."""

    @extend_schema(
        operation_id="internal_billing_tenant_convert_to_salon",
        tags=["internal", "billing"],
        responses={
            200: OpenApiResponse(description="{tariff: salon, effective_from}"),
            400: OpenApiResponse(description="Validation error"),
            404: OpenApiResponse(description="BILLING_DISABLED / not found"),
            409: OpenApiResponse(description="PRICE_MISMATCH / ALREADY_SALON"),
        },
    )
    def post(self, request: Request, tenant_id: UUID) -> Response:
        from django.utils import timezone

        if not billing_enabled():
            return _disabled()
        if not _idempotency_key(request):
            return error_response("VALIDATION_ERROR", "idempotency_key is required.")
        raw = (request.data or {}).get("confirmed_price")
        try:
            confirmed = Decimal(str(raw)) if isinstance(raw, str) else None
        except InvalidOperation:
            confirmed = None
        if confirmed is None:
            return error_response(
                "VALIDATION_ERROR", 'confirmed_price must be a money string ("990.00").',
            )
        try:
            subscription = convert_to_salon(
                request.billing_tenant, confirmed_price=confirmed,
            )
        except PriceMismatch as exc:
            return error_response(
                "PRICE_MISMATCH", "The confirmed price is not the current salon price.",
                details={"current_price": str(exc)}, status_code=409,
            )
        except AlreadySalon:
            return error_response(
                "ALREADY_SALON", "The cabinet is already a salon.", status_code=409,
            )
        effective_from = (
            subscription.pending_tariff_from
            if subscription is not None and subscription.pending_tariff_from
            else timezone.localdate()
        )
        return success_response({
            "tariff": "salon", "effective_from": effective_from.isoformat(),
        })
