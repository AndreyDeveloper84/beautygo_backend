"""Salon administrator's own token from a MAX Mini App signature (DRF-2607).

OWNER RULING 29.09: «(а) — подпись MAX, служебный ключ в записи не
участвует». The salon console could not change a master's schedule at all:
every write route answered through the bot's service credential, and that
credential is read-only by construction (``ServiceCredentialIsReadOnly``).
A linked administrator had no way to obtain a token of their own — their
password is unusable (``salon_admin_linking``), there is no MAX login here,
and JWTs came only from anonymous and social sign-in.

The principle, in the owner's words (DRF-2606 acceptance, «Связанный
DRF-2607»): **валидность сама по себе ещё не означает полномочие.** A
cryptographically correct signature of the CLIENT bot must not authorise a
request where the salon-master one is expected. The owner's matrix:

======================================  ======
presented                               result
======================================  ======
salon-master bot signature              ACCEPT
client bot signature                    REJECT
wrong signature                         REJECT
no signature at all                     REJECT — and NOT «the old way»: no
                                        fallback to the service credential
======================================  ======

This module is that way, and nothing wider:

1. **Who is it** — the catalog verifies the Mini App ``initData`` itself,
   with the key of the SALON-MASTER bot and no other
   (``settings.MAX_SALON_BOT_TOKEN``). There are two MAX bots; the shared
   ``MAX_BOT_WEB_APP`` names the CLIENT one, and a lookup «by bot» through
   it answers for the wrong audience. Only one key is ever tried here, so a
   payload signed by the client bot fails as a forgery would.
2. **How fresh** — ``auth_date`` no older than
   :data:`AUTH_DATE_MAX_AGE_SECONDS` (10 minutes) and no further in the
   future than :data:`AUTH_DATE_FUTURE_SKEW_SECONDS`. Narrower than the
   bot's read window (60 min) on purpose: the bot sees the same ``initData``
   (``MaxInitData`` header), so every component it passed through could mint
   write tokens for as long as the window lasts. Repeated exchange inside
   the window is allowed — no single-use, so a slow administrator is not
   punished after their token expires; one who idles past ten minutes
   reopens the Mini App (main window's decision, 29.09).
3. **What it gets** — :class:`SalonAdminAccessToken`: its own token type
   (so the project-wide ``JWTAuthentication`` refuses it everywhere), minutes
   of life, and bound to ONE tenant: :class:`SalonAdminTokenAuthentication`
   refuses it when ``X-Tenant`` names another salon.
4. **What it may do** — nothing by itself. Authentication answers «who»;
   the views keep ``IsTenantAdminOrPlatformAdmin`` unchanged, so a token of a
   person without an active ``role=admin`` link in THAT salon writes nothing.

Revocation: dropping the ``role=admin`` link takes effect on the next
request (the permission is checked every time); unlinking the MAX identity
does not recall a token already issued — it lives out its minutes.

Whose token (DRF-2607 review + the catalog census of 29.09):

* an identity that IS the account — including a proxy ``bot:max:<id>``
  nobody linked, which is exactly the pilot administrator of formula-tela —
  gets a token when it holds an active ``role=admin`` link in this salon;
* an identity that reaches an account THROUGH ``linked_user`` gets one only
  when that link was made by a proof-carrying door
  (:data:`PROOF_CARRYING_INITIATORS`); the s2s ``bind-external`` door
  (``internal_api``) has no proof of ownership, and a link with no audit
  row is not proven either. Census: 20 proxy rows, 0 with ``linked_user`` —
  the rule cuts off nobody today;
* a platform / staff / superuser / deleted account gets nothing (DRF-1987).
  Consequence named: the admin of ``ayla-marketplace`` is staff+superuser
  and gets no MAX token.

What it deliberately is not: there is no debug bypass of the signature, no
fallback key, and the service Bearer plays no part — the exchange view has
no authenticators at all, and on the write views the service credential is
still held to reading by ``ServiceCredentialIsReadOnly`` whatever else the
request carries.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl

from django.conf import settings
from rest_framework import permissions, serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import Token

from .response import error_response, success_response

logger = logging.getLogger(__name__)

#: Freshness of the signature presented for a WRITE token (see module doc).
AUTH_DATE_MAX_AGE_SECONDS = 10 * 60
AUTH_DATE_FUTURE_SKEW_SECONDS = 300

#: Minutes, not the project's 15-minute access lifetime and never a refresh:
#: a new token needs a fresh signature, i.e. the person in the Mini App.
TOKEN_LIFETIME = timedelta(minutes=10)

#: How the person proved who they are — written into the token and the journal.
AMR_MAX_INIT_DATA = "max_init_data"

#: The source under which ``salon_admin_linking`` binds a MAX identity.
EXTERNAL_SOURCE = "bot:max"

#: Binding doors that carry proof of who owns the MAX identity. Not
#: ``internal_api`` — the s2s ``bind-external`` door binds a caller-named pair.
PROOF_CARRYING_INITIATORS = frozenset(
    {
        "bot_salon_admin_link",
        "admin_link_salon_admin",
        "admin_link_solo_master",
        "bot_specialist_identity_link",
    }
)


class InitDataError(Exception):
    """Base: the signature does not prove who this is."""


class InitDataMalformed(InitDataError):
    pass


class InitDataBadSignature(InitDataError):
    pass


class InitDataStale(InitDataError):
    pass


class InitDataFromFuture(InitDataError):
    pass


class InitDataNotConfigured(InitDataError):
    """No salon-master bot key: the server can verify nothing and says so."""


@dataclass(frozen=True)
class VerifiedMaxUser:
    user_id: str
    auth_date: int

    @property
    def external_user_id(self) -> str:
        return f"{EXTERNAL_SOURCE}:{self.user_id}"


def _expected_hash(token: str, data_check_string: str) -> str:
    """MAX / Telegram WebApp two-stage HMAC."""
    secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_salon_init_data(raw: str, *, now: int | None = None) -> VerifiedMaxUser:
    """Verify ``initData`` with the salon-master bot key — the only key tried."""
    if not raw:
        raise InitDataMalformed("empty initData")
    key = getattr(settings, "MAX_SALON_BOT_TOKEN", "") or ""
    if not key:
        raise InitDataNotConfigured("MAX_SALON_BOT_TOKEN is not configured")

    params: dict[str, str] = {}
    for k, v in parse_qsl(raw, keep_blank_values=True):
        if k in params:
            # a second ``hash`` is how a forged payload fools a naive parser
            raise InitDataMalformed(f"duplicate key {k!r}")
        params[k] = v

    received = params.pop("hash", "")
    if not received:
        raise InitDataMalformed("missing hash")
    data_check_string = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    if not hmac.compare_digest(_expected_hash(key, data_check_string), received):
        raise InitDataBadSignature("signature mismatch")

    # Freshness is judged only on a payload whose auth_date is signed.
    try:
        auth_date = int(params.get("auth_date", ""))
    except ValueError as exc:
        raise InitDataMalformed("auth_date is not an integer") from exc
    current = int(time.time()) if now is None else now
    if current - auth_date > AUTH_DATE_MAX_AGE_SECONDS:
        raise InitDataStale("auth_date too old")
    if auth_date - current > AUTH_DATE_FUTURE_SKEW_SECONDS:
        raise InitDataFromFuture("auth_date in the future")

    try:
        user = json.loads(params.get("user", ""))
    except ValueError as exc:
        raise InitDataMalformed("user is not JSON") from exc
    user_id = str(user.get("id", "")) if isinstance(user, dict) else ""
    if not user_id:
        raise InitDataMalformed("user.id missing")
    return VerifiedMaxUser(user_id=user_id, auth_date=auth_date)


class SalonAdminAccessToken(Token):
    """Its own type: ``JWTAuthentication`` (``AccessToken`` only) refuses it."""

    token_type = "salon_admin_access"
    lifetime = TOKEN_LIFETIME


def issue_salon_admin_token(user: Any, tenant: Any) -> SalonAdminAccessToken:
    token = SalonAdminAccessToken.for_user(user)
    token["tenant_id"] = str(tenant.pk)
    token["amr"] = AMR_MAX_INIT_DATA
    return token


class SalonAdminTokenAuthentication(JWTAuthentication):
    """Accepts :class:`SalonAdminAccessToken` only, and only for its tenant.

    Any other bearer — an ordinary JWT, the service token, garbage — is not
    this credential: ``None``, and the next authenticator decides. A salon
    token presented for another salon is refused outright: it IS this
    credential, used out of its binding.
    """

    def authenticate(self, request: Any):
        header = self.get_header(request)
        if header is None:
            return None
        raw = self.get_raw_token(header)
        if raw is None:
            return None
        try:
            token = SalonAdminAccessToken(raw)
        except TokenError:
            return None
        tenant = getattr(request, "tenant", None)
        if tenant is None or str(tenant.pk) != str(token.get("tenant_id", "")):
            raise AuthenticationFailed("Token is bound to another salon.", code="token_tenant_mismatch")
        return self.get_user(token), token


def auth_method(request: Any) -> str:
    """What the journal says about HOW the actor proved who they are."""
    from .authentication import AylaServiceBearerAuthentication

    authenticator = getattr(request, "successful_authenticator", None)
    if isinstance(authenticator, SalonAdminTokenAuthentication):
        return AMR_MAX_INIT_DATA
    if isinstance(authenticator, AylaServiceBearerAuthentication):
        return "service_bearer"
    return "jwt"


def _link_initiator(external_user_id: str, proxy: Any) -> str | None:
    """Who made the proxy → account link: the latest ``created`` audit row."""
    from analytics import event_catalogue
    from analytics.models import AnalyticsEvent

    row = (
        AnalyticsEvent.objects.filter(
            event_name=event_catalogue.EXTERNAL_IDENTITY_BOUND,
            payload__external_user_id=external_user_id,
            payload__target_user_id=str(proxy.linked_user_id),
            payload__result="created",
        )
        .order_by("-created_at")
        .first()
    )
    return (row.payload or {}).get("initiator") if row is not None else None


def _account_for(external_user_id: str) -> tuple[Any, str | None]:
    """(account, refusal code). The account is the identity itself, or the
    one its link points at when that link was made by a door with proof."""
    from users.models import User
    from users.services import is_valid_external_user_id

    if not is_valid_external_user_id(external_user_id):
        return None, "IDENTITY_NOT_LINKED"
    row = User.objects.select_related("linked_user").filter(username=external_user_id).first()
    if row is None:
        return None, "IDENTITY_NOT_LINKED"
    account = row
    if row.is_proxy and row.linked_user_id is not None:
        if _link_initiator(external_user_id, row) not in PROOF_CARRYING_INITIATORS:
            return None, "LINK_NOT_PROVEN"
        account = row.linked_user
    if not account.is_active or account.deleted_at is not None:
        return None, "IDENTITY_NOT_LINKED"
    if account.is_platform_admin or account.is_staff or account.is_superuser:
        # DRF-1987: a MAX identity never leads into a platform account.
        # ``IsTenantAdminOrPlatformAdmin`` lets a platform admin into EVERY
        # salon — through this door that would be a write anywhere.
        return None, "IDENTITY_NOT_ELIGIBLE"
    return account, None


class _ExchangeSerializer(serializers.Serializer):
    init_data = serializers.CharField(max_length=8192)


_REFUSAL = {
    InitDataMalformed: ("INIT_DATA_MALFORMED", 400),
    InitDataBadSignature: ("INIT_DATA_BAD_SIGNATURE", 401),
    InitDataStale: ("INIT_DATA_STALE", 401),
    InitDataFromFuture: ("INIT_DATA_STALE", 401),
    InitDataNotConfigured: ("NOT_CONFIGURED", 503),
}


class MaxSalonAdminTokenView(APIView):
    """POST /api/v1/auth/max/salon-admin/token/ — ``init_data`` → token.

    ``X-Tenant`` names the salon the token is for. No authenticators: no
    bearer of any kind takes part in the exchange.
    """

    authentication_classes: list = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            return error_response("TENANT_REQUIRED", "X-Tenant is required.", status_code=400)

        serializer = _ExchangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            verified = verify_salon_init_data(serializer.validated_data["init_data"])
        except InitDataError as exc:
            code, status = _REFUSAL[type(exc)]
            logger.info("auth.max_salon_admin.refused reason=%s tenant=%s", code, tenant.pk)
            return error_response(code, "The Mini App signature was not accepted.", status_code=status)

        from users.models import TenantUserRelationship

        user, refusal = _account_for(verified.external_user_id)
        if refusal is None and not TenantUserRelationship.objects.filter(
            user=user, tenant=tenant, role=TenantUserRelationship.Role.ADMIN, is_active=True
        ).exists():
            # Issued only to someone who administers THIS salon now. Every
            # write still asks ``IsTenantAdmin`` again — a grant revoked
            # after issuance takes effect on the next request.
            refusal = "NO_SALON_ROLE"
        if refusal is not None:
            logger.info("auth.max_salon_admin.refused reason=%s tenant=%s", refusal, tenant.pk)
            return error_response(refusal, "This MAX account cannot sign in to this salon.", status_code=403)

        token = issue_salon_admin_token(user, tenant)
        logger.info("auth.max_salon_admin.issued actor=%s tenant=%s", user.pk, tenant.pk)
        return success_response(
            {
                "access_token": str(token),
                "token_type": SalonAdminAccessToken.token_type,
                "expires_in": int(TOKEN_LIFETIME.total_seconds()),
                "tenant": tenant.slug,
            }
        )
