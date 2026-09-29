"""DRF-2607: a salon administrator's own token from a MAX Mini App signature.

The owner's matrix (DRF-2606 acceptance, «Связанный DRF-2607») — every row
is a pair with the ACCEPT row, on the same person and the same payload:

======================================  ======
presented                               result
======================================  ======
salon-master bot signature              ACCEPT
client bot signature                    REJECT
wrong signature                         REJECT
no signature at all                     REJECT — and no fallback to the service key
======================================  ======

«Валидность сама по себе ещё не означает полномочие»: the client bot's
signature below is cryptographically correct — with ITS key — and is refused.

Every request goes through the real authentication stack; nothing uses
``force_authenticate``, which skips exactly the layer under test.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import timedelta
from urllib.parse import urlencode

import pytest
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from tenants.models import Tenant
from users.max_salon_admin_auth import (
    AUTH_DATE_FUTURE_SKEW_SECONDS,
    AUTH_DATE_MAX_AGE_SECONDS,
    TOKEN_LIFETIME,
    SalonAdminAccessToken,
    issue_salon_admin_token,
)
from users.models import SpecialistProfile, TenantUserRelationship, User

#: Test keys, not real ones. Two bots, two keys — the whole point.
SALON_BOT_KEY = "salon-master-bot-key-under-test"  # pragma: allowlist secret
CLIENT_BOT_KEY = "client-bot-key-under-test"  # pragma: allowlist secret
SERVICE_TOKEN = "ayla-service-token-under-test"  # pragma: allowlist secret

MAX_ID = "26070001"
EXCHANGE = "/api/v1/auth/max/salon-admin/token/"


@pytest.fixture(autouse=True)
def _keys(settings):
    settings.MAX_SALON_BOT_TOKEN = SALON_BOT_KEY
    settings.AYLA_INTERNAL_API_TOKEN = SERVICE_TOKEN


def _signed(key: str | None, *, max_id: str = MAX_ID, auth_date: int | None = None, tamper: bool = False) -> str:
    """A Mini App ``initData`` signed the way MAX signs it — with ``key``."""
    params = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "q-2607",
        "user": json.dumps({"id": int(max_id), "first_name": "Админ"}, separators=(",", ":")),
    }
    if key is not None:
        dcs = "\n".join(f"{k}={params[k]}" for k in sorted(params))
        secret = hmac.new(b"WebAppData", key.encode(), hashlib.sha256).digest()
        params["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if tamper:
        # signed for one person, presented as another
        params["user"] = json.dumps({"id": int(max_id) + 1, "first_name": "Админ"}, separators=(",", ":"))
    return urlencode(params)


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="s2607-a", name="Salon A")


@pytest.fixture
def other_salon(db):
    return Tenant.objects.create(slug="s2607-b", name="Salon B")


def _person(username: str, phone: str) -> User:
    return User.objects.create_user(username=username, password="x", role="client", phone=phone)


def _grant(user: User, tenant: Tenant) -> None:
    TenantUserRelationship.objects.create(
        user=user, tenant=tenant, role=TenantUserRelationship.Role.ADMIN, is_active=True
    )


@pytest.fixture
def admin(salon, other_salon):
    """Administers BOTH salons — so a token bound to A is tested against a
    salon where the person really is an admin: only the binding can refuse."""
    user = _person(f"bot:max:{MAX_ID}", "+79995260701")
    _grant(user, salon)
    _grant(user, other_salon)
    return user


@pytest.fixture
def master(salon):
    user = User.objects.create_user(username="m2607", password="x", role="specialist", phone="+79995260709")
    user.tenant = salon
    user.save(update_fields=["tenant"])
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = salon
    profile.timezone = "Europe/Moscow"
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.save()
    return profile


def _client(*, tenant: Tenant | None, bearer: str | None = None, **headers) -> APIClient:
    client = APIClient()
    client.defaults["HTTP_X_APP_TYPE"] = "pro"
    if tenant is not None:
        client.defaults["HTTP_X_TENANT"] = tenant.slug
    if bearer is not None:
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    client.defaults.update(headers)
    return client


def _exchange(init_data: str | None, *, tenant: Tenant):
    body = {} if init_data is None else {"init_data": init_data}
    return _client(tenant=tenant).post(EXCHANGE, body, format="json")


def _time_off_url(master) -> str:
    return f"/api/v1/tenants/me/masters/{master.id}/time-off/"


def _time_off_body() -> dict:
    start = (time.time() // 3600 + 24 * 30) * 3600  # a month ahead, on the hour
    from datetime import datetime, timezone

    begin = datetime.fromtimestamp(start, tz=timezone.utc)
    return {
        "start_at": begin.isoformat(),
        "end_at": (begin + timedelta(hours=3)).isoformat(),
        "reason": "отгул",
    }


# --- 1. The matrix, at the exchange ----------------------------------------


@pytest.mark.django_db
class TestTheOwnersMatrix:
    def test_salon_master_signature_is_accepted(self, admin, salon):
        resp = _exchange(_signed(SALON_BOT_KEY), tenant=salon)
        assert resp.status_code == 200, resp.content
        token = SalonAdminAccessToken(resp.data["data"]["access_token"])
        assert (token["user_id"], token["tenant_id"]) == (str(admin.pk), str(salon.pk))

    def test_client_bot_signature_is_rejected(self, admin, salon):
        """Valid with the client bot's key — and still no: validity is not authority."""
        resp = _exchange(_signed(CLIENT_BOT_KEY), tenant=salon)
        assert (resp.status_code, resp.data["error"]["code"]) == (401, "INIT_DATA_BAD_SIGNATURE")

    def test_wrong_signature_is_rejected(self, admin, salon):
        resp = _exchange(_signed(SALON_BOT_KEY, tamper=True), tenant=salon)
        assert (resp.status_code, resp.data["error"]["code"]) == (401, "INIT_DATA_BAD_SIGNATURE")

    @pytest.mark.parametrize("init_data", ["unsigned", None], ids=["no-hash", "no-init-data"])
    def test_no_signature_is_rejected(self, admin, salon, init_data):
        raw = _signed(None) if init_data == "unsigned" else None
        resp = _exchange(raw, tenant=salon)
        assert resp.status_code == 400
        assert "data" not in resp.data  # an error envelope, no token

    def test_a_second_hash_is_rejected(self, admin, salon):
        raw = _signed(SALON_BOT_KEY) + "&hash=" + "0" * 64
        assert _exchange(raw, tenant=salon).status_code == 400


@pytest.mark.django_db
class TestFreshness:
    @pytest.mark.parametrize(
        ("offset", "status"),
        [
            (-(AUTH_DATE_MAX_AGE_SECONDS - 60), 200),
            (-(AUTH_DATE_MAX_AGE_SECONDS + 60), 401),
            (AUTH_DATE_FUTURE_SKEW_SECONDS - 60, 200),
            (AUTH_DATE_FUTURE_SKEW_SECONDS + 60, 401),
            # A number, not the constant: the decided window is 10 minutes
            # (the bot reads for 60) — a test built only from the constant
            # would pass whatever the constant became.
            (-15 * 60, 401),
        ],
        ids=["inside-age", "stale", "inside-skew", "from-future", "past-the-decided-ten-minutes"],
    )
    def test_the_window(self, admin, salon, offset, status):
        raw = _signed(SALON_BOT_KEY, auth_date=int(time.time()) + offset)
        assert _exchange(raw, tenant=salon).status_code == status


@pytest.mark.django_db
class TestWhoGetsAToken:
    def test_an_unlinked_max_account_gets_nothing(self, admin, salon):
        """Correct salon-bot signature, but nobody is bound under that id."""
        resp = _exchange(_signed(SALON_BOT_KEY, max_id="26079999"), tenant=salon)
        assert (resp.status_code, resp.data["error"]["code"]) == (403, "IDENTITY_NOT_LINKED")

    def test_the_pilot_shape_a_proxy_with_its_own_grant(self, salon):
        """The pilot administrator of formula-tela IS a proxy ``bot:max:<id>``
        with the admin link on the proxy row itself, and no ``linked_user``.
        Pair: the same proxy without the grant gets nothing."""
        from users.services import resolve_external_user

        proxy = resolve_external_user("bot:max:26070003")
        assert proxy.is_proxy and proxy.linked_user_id is None  # presence: the pilot shape
        refused = _exchange(_signed(SALON_BOT_KEY, max_id="26070003"), tenant=salon)
        assert (refused.status_code, refused.data["error"]["code"]) == (403, "NO_SALON_ROLE")

        _grant(proxy, salon)
        ok = _exchange(_signed(SALON_BOT_KEY, max_id="26070003"), tenant=salon)
        assert ok.status_code == 200, ok.content
        assert SalonAdminAccessToken(ok.data["data"]["access_token"])["user_id"] == str(proxy.pk)

    def test_a_grant_in_another_salon_is_not_this_salon(self, admin, db):
        third = Tenant.objects.create(slug="s2607-c", name="Salon C")
        resp = _exchange(_signed(SALON_BOT_KEY), tenant=third)
        assert (resp.status_code, resp.data["error"]["code"]) == (403, "NO_SALON_ROLE")

    def test_the_production_link_shape_is_accepted(self, salon):
        """As ``salon_admin_linking`` builds it: a proxy ``bot:max:<id>``
        bound to a fresh ``role=admin`` account — the token is the ACCOUNT's.
        Pair: the same link with the account deactivated gets nothing."""
        from users.services import INITIATOR_BOT_SALON_ADMIN_LINK, bind_external_identity

        fresh = User.objects.create_user(username="salon-admin:s2607-a:fresh", password=None, role="admin")
        _grant(fresh, salon)
        bind_external_identity(
            "bot:max:26070004", fresh.pk, initiator=INITIATOR_BOT_SALON_ADMIN_LINK, target_roles=("admin",)
        )
        ok = _exchange(_signed(SALON_BOT_KEY, max_id="26070004"), tenant=salon)
        assert ok.status_code == 200, ok.content
        assert SalonAdminAccessToken(ok.data["data"]["access_token"])["user_id"] == str(fresh.pk)

        User.objects.filter(pk=fresh.pk).update(is_active=False)
        assert _exchange(_signed(SALON_BOT_KEY, max_id="26070004"), tenant=salon).status_code == 403

    def test_a_link_through_the_s2s_door_is_not_proof(self, salon):
        """``bind-external`` (initiator ``internal_api``) binds a caller-named
        pair with no proof of who owns the MAX id. The target administers the
        salon — and the exchange still refuses. Pair: the same salon, an
        identity that carries its own grant (the pilot shape), passes."""
        from users.services import bind_external_identity, resolve_external_user

        target = _person("t2607-client-admin", "+79995260705")
        _grant(target, salon)
        bind_external_identity("bot:max:26070005", target.pk)  # default door: internal_api
        refused = _exchange(_signed(SALON_BOT_KEY, max_id="26070005"), tenant=salon)
        assert (refused.status_code, refused.data["error"]["code"]) == (403, "LINK_NOT_PROVEN")

        own = resolve_external_user("bot:max:26070006")
        _grant(own, salon)
        assert _exchange(_signed(SALON_BOT_KEY, max_id="26070006"), tenant=salon).status_code == 200

    def test_a_link_without_an_audit_row_is_not_proof(self, salon):
        from users.services import resolve_external_user

        target = _person("t2607-unaudited", "+79995260706")
        _grant(target, salon)
        proxy = resolve_external_user("bot:max:26070007")
        User.objects.filter(pk=proxy.pk).update(linked_user=target)  # no bind, no audit
        resp = _exchange(_signed(SALON_BOT_KEY, max_id="26070007"), tenant=salon)
        assert (resp.status_code, resp.data["error"]["code"]) == (403, "LINK_NOT_PROVEN")

    @pytest.mark.parametrize("flag", ["is_platform_admin", "is_staff", "is_superuser"])
    def test_a_platform_account_gets_nothing(self, admin, salon, flag):
        """A platform admin passes ``IsTenantAdminOrPlatformAdmin`` in EVERY
        salon — a MAX door to such an account would be a write anywhere."""
        assert _exchange(_signed(SALON_BOT_KEY), tenant=salon).status_code == 200  # presence
        User.objects.filter(pk=admin.pk).update(**{flag: True})
        resp = _exchange(_signed(SALON_BOT_KEY), tenant=salon)
        assert (resp.status_code, resp.data["error"]["code"]) == (403, "IDENTITY_NOT_ELIGIBLE")

    def test_without_the_key_the_server_says_so(self, admin, salon, settings):
        settings.MAX_SALON_BOT_TOKEN = ""
        resp = _exchange(_signed(SALON_BOT_KEY), tenant=salon)
        assert (resp.status_code, resp.data["error"]["code"]) == (503, "NOT_CONFIGURED")

    def test_the_tenant_is_required(self, admin):
        resp = _client(tenant=None).post(EXCHANGE, {"init_data": _signed(SALON_BOT_KEY)}, format="json")
        assert resp.status_code == 400


# --- 2. The token: short, narrow, one salon ---------------------------------


@pytest.mark.django_db
class TestTheTokenIsNarrow:
    def test_it_lives_minutes(self, admin, salon):
        token = issue_salon_admin_token(admin, salon)
        assert token["exp"] - token["iat"] == int(TOKEN_LIFETIME.total_seconds()) <= 15 * 60

    def test_it_writes_in_its_salon_and_not_in_another(self, admin, salon, other_salon, master):
        """The person administers BOTH salons; only the binding differs."""
        token = str(issue_salon_admin_token(admin, salon))
        ok = _client(tenant=salon, bearer=token).post(_time_off_url(master), _time_off_body(), format="json")
        assert ok.status_code == 201, ok.content

        elsewhere = _client(tenant=other_salon, bearer=token).get(
            f"/api/v1/tenants/me/masters/{master.id}/time-off/"
        )
        assert elsewhere.status_code == 401

    def test_without_x_tenant_the_token_is_refused(self, admin, salon, master):
        """No header → no fallback to the claim: the middleware's claim
        fallback reads ordinary access tokens only."""
        token = str(issue_salon_admin_token(admin, salon))
        resp = _client(tenant=None, bearer=token).post(_time_off_url(master), _time_off_body(), format="json")
        assert resp.status_code == 401

    def test_a_date_exception_in_another_salon_is_refused(self, admin, salon, other_salon, master):
        token = str(issue_salon_admin_token(admin, salon))
        resp = _client(tenant=other_salon, bearer=token).put(
            f"/api/v1/tenants/me/masters/{master.id}/schedule-exceptions/",
            {"date": "2031-02-04", "is_working_day": False},
            format="json",
        )
        assert resp.status_code == 401

    def test_its_own_time_off_can_be_deleted(self, admin, salon, master):
        client = _client(tenant=salon, bearer=str(issue_salon_admin_token(admin, salon)))
        created = client.post(_time_off_url(master), _time_off_body(), format="json")
        assert created.status_code == 201, created.content
        gone = client.delete(f"{_time_off_url(master)}{created.data['data']['id']}/")
        assert gone.status_code in (200, 204), gone.content

    def test_an_expired_token_writes_nothing(self, admin, salon, master):
        token = issue_salon_admin_token(admin, salon)
        token.set_exp(lifetime=-timedelta(seconds=1))
        resp = _client(tenant=salon, bearer=str(token)).post(_time_off_url(master), _time_off_body(), format="json")
        assert resp.status_code == 401

    def test_the_ordinary_jwt_path_refuses_it(self, admin, salon):
        """Own token type: every view on the default authenticator says no —
        against an ordinary access token of the same person, which works."""
        salon_token = str(issue_salon_admin_token(admin, salon))
        ordinary = str(RefreshToken.for_user(admin).access_token)
        me = "/api/v1/auth/users/me/"
        assert _client(tenant=salon, bearer=ordinary).get(me).status_code == 200
        assert _client(tenant=salon, bearer=salon_token).get(me).status_code == 401

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("put", "schedule/"),  # the weekly template waits for the shrink guard
        ],
    )
    def test_the_weekly_template_is_not_opened(self, admin, salon, master, method, path):
        token = str(issue_salon_admin_token(admin, salon))
        client = _client(tenant=salon, bearer=token)
        resp = getattr(client, method)(f"/api/v1/tenants/me/masters/{master.id}/{path}", {}, format="json")
        assert resp.status_code == 401

    def test_closures_are_not_opened(self, admin, salon):
        token = str(issue_salon_admin_token(admin, salon))
        resp = _client(tenant=salon, bearer=token).post(
            "/api/v1/tenants/me/closures/", {"date": "2031-01-01"}, format="json"
        )
        assert resp.status_code == 401


# --- 3. Who it is vs. what they may do --------------------------------------


@pytest.mark.django_db
class TestAuthenticationIsNotAuthority:
    def test_a_linked_person_without_the_admin_link_writes_nothing(self, salon, master):
        """Pair: the same person, the same token, before and after the grant."""
        person = _person("bot:max:26070002", "+79995260702")
        token = str(issue_salon_admin_token(person, salon))

        refused = _client(tenant=salon, bearer=token).post(_time_off_url(master), _time_off_body(), format="json")
        assert refused.status_code == 403

        _grant(person, salon)
        allowed = _client(tenant=salon, bearer=token).post(_time_off_url(master), _time_off_body(), format="json")
        assert allowed.status_code == 201, allowed.content

    def test_a_date_exception_is_open_to_the_admin(self, admin, salon, master):
        token = str(issue_salon_admin_token(admin, salon))
        resp = _client(tenant=salon, bearer=token).put(
            f"/api/v1/tenants/me/masters/{master.id}/schedule-exceptions/",
            {"date": "2031-02-03", "is_working_day": False},
            format="json",
        )
        assert resp.status_code in (200, 201), resp.content


# --- 4. The service key takes no part in a write ----------------------------


@pytest.mark.django_db
class TestTheServiceKeyDoesNotWrite:
    def test_service_bearer_with_a_persons_proof_attached_is_refused(self, admin, salon, master):
        """Everything a confused path could pick up is on the request — the
        admin's external id, a valid salon token in a side header, a valid
        signature in the body. The service Bearer still writes nothing."""
        salon_token = str(issue_salon_admin_token(admin, salon))
        client = _client(
            tenant=salon,
            bearer=SERVICE_TOKEN,
            HTTP_X_EXTERNAL_USER_ID=f"bot:max:{MAX_ID}",
            HTTP_X_SALON_ADMIN_TOKEN=salon_token,
        )
        body = {**_time_off_body(), "init_data": _signed(SALON_BOT_KEY)}
        resp = client.post(_time_off_url(master), body, format="json")
        assert resp.status_code == 403

        # Pair: the same person's own token, the same write.
        own = _client(tenant=salon, bearer=salon_token).post(_time_off_url(master), _time_off_body(), format="json")
        assert own.status_code == 201, own.content

    def test_no_proof_at_all_is_not_the_old_way(self, admin, salon, master):
        """The matrix's fourth row on the write side: without a signature
        there is no silent fallback to the service path — it still reads only."""
        client = _client(tenant=salon, bearer=SERVICE_TOKEN, HTTP_X_EXTERNAL_USER_ID=f"bot:max:{MAX_ID}")
        assert client.get(_time_off_url(master)).status_code == 200  # presence: the service path is live
        assert client.post(_time_off_url(master), _time_off_body(), format="json").status_code == 403


# --- 5. The journal names the person and how they proved it -----------------


@pytest.mark.django_db
class TestTheJournal:
    def _write_and_read_journal(self, caplog, client, master) -> str:
        # ``users`` has ``propagate: False`` (settings.LOGGING): caplog's
        # root handler would see nothing — attach it to the logger itself.
        journal = logging.getLogger("users.schedule_admin_api")
        journal.addHandler(caplog.handler)
        try:
            with caplog.at_level(logging.INFO, logger="users.schedule_admin_api"):
                resp = client.post(_time_off_url(master), _time_off_body(), format="json")
        finally:
            journal.removeHandler(caplog.handler)
        assert resp.status_code == 201, resp.content
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("schedule.time_off_created")]
        assert len(lines) == 1, lines
        return lines[0]

    def test_a_persons_own_write_names_them_and_the_signature(self, caplog, admin, salon, master):
        token = str(issue_salon_admin_token(admin, salon))
        line = self._write_and_read_journal(caplog, _client(tenant=salon, bearer=token), master)
        assert f"actor={admin.pk} via=max_init_data tenant={salon.pk}" in line

    def test_the_same_write_over_an_ordinary_jwt_says_so(self, caplog, admin, salon, master):
        """Pair: ``actor`` would read the same — ``via`` is what differs."""
        ordinary = str(RefreshToken.for_user(admin).access_token)
        line = self._write_and_read_journal(caplog, _client(tenant=salon, bearer=ordinary), master)
        assert f"actor={admin.pk} via=jwt tenant={salon.pk}" in line
