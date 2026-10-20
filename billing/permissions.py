"""Second factor for the cabinet's money decisions (DRF-2984 C3, В-8).

card-setup, cancel and convert-to-salon change what a salon pays. The
runtime bearer alone is the bot; ``X-External-User-ID`` must name a
person who administers THIS tenant — the same rule as
``IsInternalBearerForSalonSubject``, keyed by the tenant UUID in the URL
instead of a slug.
"""
from __future__ import annotations

import logging
from typing import Any

from users.permissions import IsInternalBearerForSubject, _publish_verdict

logger = logging.getLogger(__name__)


class IsInternalBearerForTenantAdminSubject(IsInternalBearerForSubject):
    """URL names a tenant (``tenant_id``) the named actor administers.

    A tenant the actor does not administer, an inactive one, an unknown
    UUID — all answer 404, never 403: a foreign tenant id is not
    confirmed (same rule as the salon subject gate).
    """

    def _subject_verdict(
        self, request: Any, *, purpose: str, subject_id: str, actor: Any,
        external_user_id: str,
    ) -> bool:
        from rest_framework.exceptions import NotFound

        from tenants.models import Tenant
        from users.models import TenantUserRelationship

        tenant = Tenant.objects.filter(pk=subject_id).first()
        administers = tenant is not None and TenantUserRelationship.objects.filter(
            user=actor,
            tenant=tenant,
            role=TenantUserRelationship.Role.ADMIN,
            is_active=True,
        ).exists()
        if not administers:
            logger.warning(
                "internal.subject_authz.tenant_admin_mismatch path=%s tenant=%s actor=%s",
                request.path, subject_id, actor.pk,
            )
            _publish_verdict(
                request, purpose=purpose, actor=actor, actor_named=True,
                allowed=False, reason="tenant_admin_mismatch",
            )
            raise NotFound("Tenant not found.")
        request.billing_tenant = tenant
        _publish_verdict(request, purpose=purpose, actor=actor, actor_named=True, allowed=True)
        return True
