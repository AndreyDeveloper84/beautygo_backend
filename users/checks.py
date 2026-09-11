"""Django system checks for the users app."""
from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, register


@register("security")
def identity_provisioning_token_check(app_configs, **kwargs):
    """The whole provisioning-only trust boundary rests on
    ``AYLA_IDENTITY_PROVISIONING_TOKEN`` being a DIFFERENT secret than
    the general bot service token. If ops sets them equal (tempting —
    the bot already carries a Bearer), the bot runtime credential would
    pass the identity-binding endpoint: arbitrary external identity →
    arbitrary client account on every s2s surface. Refuse to stay
    silent about that misconfiguration (the runtime permission also
    fails closed on it — see IsIdentityProvisioningBearer).
    """
    provisioning = getattr(settings, "AYLA_IDENTITY_PROVISIONING_TOKEN", "") or ""
    general = getattr(settings, "AYLA_INTERNAL_API_TOKEN", "") or ""
    if provisioning and general and provisioning == general:
        return [Error(
            "AYLA_IDENTITY_PROVISIONING_TOKEN equals "
            "AYLA_INTERNAL_API_TOKEN — the general bot credential would "
            "pass POST /api/v1/internal/users/bind-external/. Provision "
            "two DISTINCT secrets (or leave the provisioning token empty "
            "to keep the endpoint disabled).",
            id="users.E001",
        )]
    return []


@register("security")
def tenant_provisioning_token_check(app_configs, **kwargs):
    """DRF-1695 (C1): the tenant-provisioning secret must differ from BOTH
    the general bot Bearer and the identity-provisioning secret.

    Equal to the general Bearer → the bot runtime credential could create
    salons. Equal to the identity secret → the one value the bot holds for
    salons would also open ``bind-external``, i.e. the exact grant §151
    forbids. Either way the split of powers collapses silently; refuse at
    boot, where the misconfiguration is visible, not per request.
    """
    tenant = getattr(settings, "AYLA_TENANT_PROVISIONING_TOKEN", "") or ""
    if not tenant:
        return []
    errors = []
    general = getattr(settings, "AYLA_INTERNAL_API_TOKEN", "") or ""
    identity = getattr(settings, "AYLA_IDENTITY_PROVISIONING_TOKEN", "") or ""
    if general and tenant == general:
        errors.append(Error(
            "AYLA_TENANT_PROVISIONING_TOKEN equals AYLA_INTERNAL_API_TOKEN — the "
            "general bot credential would create tenants. Provision a DISTINCT "
            "secret.",
            id="users.E002",
        ))
    if identity and tenant == identity:
        errors.append(Error(
            "AYLA_TENANT_PROVISIONING_TOKEN equals AYLA_IDENTITY_PROVISIONING_TOKEN "
            "— the salon-creation secret the bot holds would also open "
            "POST /api/v1/internal/users/bind-external/ (OPEN_DECISIONS §151). "
            "Provision a DISTINCT secret.",
            id="users.E003",
        ))
    return errors
