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
def salon_admin_link_token_check(app_configs, **kwargs):
    """DRF-2085: the salon-admin-link secret must differ from ALL THREE others.

    The ruling grants the bot's operator action exactly one identity power —
    «fresh salon administrator account + TUR + MAX link» — behind a
    credential of its own. Equal to the general Bearer, the runtime could
    mint salon administrators; equal to either provisioning secret, one
    value would carry two powers. Refuse at boot, where the misconfiguration
    is visible; ``IsSalonAdminLinkBearer`` refuses per request as well.
    """
    link = getattr(settings, "AYLA_SALON_ADMIN_LINK_TOKEN", "") or ""
    if not link:
        return []
    siblings = (
        ("AYLA_INTERNAL_API_TOKEN", "the general bot credential would mint salon administrators"),
        ("AYLA_IDENTITY_PROVISIONING_TOKEN", "one value would carry bind-external AND salon-admin linking"),
        ("AYLA_TENANT_PROVISIONING_TOKEN", "one value would carry salon creation AND salon-admin linking"),
    )
    errors = []
    for name, consequence in siblings:
        other = getattr(settings, name, "") or ""
        if other and link == other:
            errors.append(Error(
                f"AYLA_SALON_ADMIN_LINK_TOKEN equals {name} — {consequence}. "
                "Provision a DISTINCT secret (OWNER RULING 18.09, DRF-2085).",
                id="users.E004",
            ))
    return errors


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
