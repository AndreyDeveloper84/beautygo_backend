"""Auth-related DRF serializers — DRF-242.8 tenant claim refresh.

The base ``TokenRefreshSerializer`` from rest_framework_simplejwt copies
custom claims forward unchanged when minting a new access token. That's
fine for stable claims (user_id, role) but wrong for ``tenant_id`` —
admins can move a user between tenants without forcing a re-login, so
the claim has to be re-resolved against the live row on every refresh.

This module overrides the refresh path with a serializer that re-queries
``User.tenant_id`` and stamps the new value into the rotating tokens.
The view in ``users/urls.py`` (``TokenRefreshView``) wires it via
``serializer_class``.
"""
from __future__ import annotations

import logging

from rest_framework_simplejwt.serializers import TokenRefreshSerializer

from .models import User

logger = logging.getLogger(__name__)


class TenantAwareTokenRefreshSerializer(TokenRefreshSerializer):
    """Re-resolve ``tenant_id`` on every refresh.

    Why not let the base serializer copy the claim forward? Because:

    1. Admin can reassign ``User.tenant`` (e.g. legal/billing decision)
       without revoking outstanding refresh tokens. The next refresh
       must reflect the move, otherwise the user keeps a stale tenant
       in their access token until the refresh window expires (90d).

    2. ``IsTenantMember`` compares ``request.tenant`` to the current
       user row, but ``TenantContextMiddleware`` falls back to the JWT
       ``tenant_id`` claim when the header is missing. A stale claim
       there bypasses the live tenant check entirely.

    The serializer keeps the rest of the rotation behaviour (refresh
    token blacklisting, sliding expiry) by deferring to ``super()``
    first, then patching the ``access`` value with a freshly minted
    token whose ``tenant_id`` claim reflects the current row.

    On a missing user (deleted between issue and refresh) we return
    the parent's payload unchanged — simplejwt has already validated
    the refresh signature, so the request will simply fail downstream
    with the next request that requires an existing user.
    """

    def validate(self, attrs):
        data = super().validate(attrs)

        # Pull user_id from the freshly-minted access token. We can't
        # re-decode ``attrs["refresh"]`` here — with
        # ``BLACKLIST_AFTER_ROTATION=True`` (this project's setting) the
        # parent's ``super().validate()`` has already blacklisted the
        # incoming refresh, so a fresh ``RefreshToken(...)`` call would
        # raise TokenError("Token is blacklisted"). The new access has
        # the same user_id and isn't blacklisted yet.
        from rest_framework_simplejwt.tokens import AccessToken

        new_access = AccessToken(data["access"])
        user_id = new_access.get("user_id")
        if user_id is None:
            return data

        try:
            user = User.objects.only("id", "tenant_id").get(pk=user_id)
        except User.DoesNotExist:
            logger.info("token.refresh.user_missing user_id=%s", user_id)
            return data

        new_access["tenant_id"] = (
            str(user.tenant_id) if user.tenant_id else None
        )
        data["access"] = str(new_access)
        return data
