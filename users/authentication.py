"""Authenticators that let the Ayla service reach a human-scoped view.

Background — why this file exists at all.

``IsBotServiceWithVerifiedClient`` (``users.permissions``) already
implements the owner-approved shape of an Ayla call: the Bearer proves
*the call comes from Ayla*, ``X-External-User-ID`` names *the human on
whose behalf it is made*, and ``request.user`` becomes that human so the
ordinary tenant permissions decide what happens next. The salon *write*
surface (``tenants/appointments_api.py``) uses it as a permission class
and empties ``authentication_classes`` to do so, because
``DEFAULT_AUTHENTICATION_CLASSES`` runs the JWT authenticator, which
raises 401 on an opaque service Bearer *before* any permission runs.

That trick costs nothing on a surface whose only caller is the bot. It
cannot be repeated on the salon *read* surface: ``TenantDayView`` and the
DRF-1062 schedule views are also the salon console's endpoints, and
emptying their authenticators would take the JWT path away from the
humans they were built for.

So the same credential is expressed one layer lower, as an authenticator
that runs *before* JWT and steps aside (returns ``None``) for anything
that is not its own token. Nothing about authorisation moves: the views
keep ``IsAuthenticated + IsProApp + IsTenantAdmin`` unchanged, and the
resolved human still has to hold an active admin grant in the addressed
tenant before a single row is read.

Two boundaries this file deliberately does NOT cross:

* It is opt-in per view. It is never added to
  ``DEFAULT_AUTHENTICATION_CLASSES`` — that would make the service Bearer
  a way to act as any user on every endpoint in the project, which is the
  opposite of narrowing.
* It does not decide methods. ``ServiceCredentialIsReadOnly``
  (``users.permissions``) is what keeps a read grant from becoming a
  write grant; it is listed on every view this authenticator is attached
  to, and it is what makes attaching the authenticator to a class that
  also serves ``PUT``/``POST`` safe.
"""
from __future__ import annotations

from hmac import compare_digest
from typing import Any

from django.conf import settings
from rest_framework import authentication


class AylaServiceBearerAuthentication(authentication.BaseAuthentication):
    """``Authorization: Bearer <AYLA_INTERNAL_API_TOKEN>`` + ``X-External-User-ID``.

    Returns the resolved Ayla ``User`` — the person the bot is acting for
    — so downstream permissions (``IsTenantAdmin`` above all) authorise
    *that human*, not the service.

    Returns ``None``, never raises, whenever the request is not making
    this claim (no Bearer, wrong token, no external id, unresolvable id).
    ``None`` means "not my credential" and lets the next authenticator —
    in practice ``JWTAuthentication`` — try. Raising here would turn every
    ordinary JWT request into a 401, which is the failure mode this class
    exists to avoid.

    Consequence worth stating plainly: a request that presents a *wrong*
    service token is not rejected here, it is passed on and then rejected
    by JWT as an invalid token. The outcome is still 401; only the
    attribution of the refusal differs.
    """

    keyword = "Bearer"

    def authenticate(self, request: Any):
        expected = getattr(settings, "AYLA_INTERNAL_API_TOKEN", "") or ""
        if not expected:
            # Misconfigured deployment — never honour any bearer.
            return None

        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        prefix = f"{self.keyword} "
        if not auth_header.startswith(prefix):
            return None
        provided = auth_header[len(prefix):].strip()
        if not provided or not compare_digest(provided, expected):
            return None

        external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
        if not external_user_id:
            return None

        # Lazy import — users.services imports models, and this module is
        # imported from view modules during app loading.
        from users.services import (
            InvalidExternalUserIDError,
            resolve_external_user,
        )

        try:
            user = resolve_external_user(external_user_id)
        except InvalidExternalUserIDError:
            return None

        return (user, None)

    def authenticate_header(self, request: Any) -> str:
        # Present so an unauthenticated request to a view whose FIRST
        # authenticator is this one still renders as 401 rather than 403
        # (DRF picks the header from ``get_authenticators()[0]``).
        return f'{self.keyword} realm="api"'
