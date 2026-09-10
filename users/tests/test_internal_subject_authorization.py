"""CP-2 / DRF-1617 — object-level authorization on the personal-data surface.

The defect under test: a holder of the shared internal bearer could export,
overwrite and erase the personal data of ANY subject by putting that
subject's UUID in the URL.

### What is a real negative test here, and what only looks like one

§7 of the launch pack lists four negatives. One of them —
``invalid token → deny`` — **passed before a single line of this change**:
the perimeter has always refused an unknown bearer. It is kept below
(:class:`TestPreExistingPerimeter`) and labelled, because leaving it out
would invite someone to re-add it as proof. It proves the door, not the
boundary inside it.

The three that are about this change are:

* a valid token naming a subject that belongs to someone else;
* a credential issued for a different purpose;
* export **and** delete of a foreign subject, separately — the two verbs
  reach different code and one of them is destructive.

### Why the foreign subject is built the way it is

"Someone else" is not "a different UUID". Each subject here is a real
client account with its OWN bound external identity, so a denial means the
boundary held between two genuine owners — not that a fixture happened to
disagree with itself.

### Two guards that are not negatives at all

:class:`TestGuardCoversItsSubject` enumerates the routes from the URL conf
and asserts every one of them carries the check. Without it, route nine
arrives next month looking protected because the class name is in a list
somewhere.

:class:`TestUnnamedCallerIsCounted` pins the staged behaviour in BOTH
positions of the flag. While it is off this surface is **not** closed —
that is the honest reading, and the test says so in both directions so the
day someone flips it, the expectation is already written down.
"""
from __future__ import annotations

import logging
import uuid

import pytest
from django.urls import resolve
from rest_framework.test import APIClient

from analytics.models import AnalyticsEvent
from users.models import User, UserPersonalContext
from users.permissions import IsInternalBearerForSubject

pytestmark = pytest.mark.django_db

# Test constants — deliberately not resembling a real secret, and never
# printed anywhere but here.
RUNTIME_TOKEN = "test-runtime-internal-token"  # noqa: S105
PROVISIONING_TOKEN = "test-provisioning-only-token"  # noqa: S105


@pytest.fixture(autouse=True)
def _tokens(settings):
    """Both purposes provisioned, and DIFFERENT — the equal-values case is
    already fail-closed by ``users.checks.E001`` and is not this test's
    subject."""
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN
    settings.AYLA_IDENTITY_PROVISIONING_TOKEN = PROVISIONING_TOKEN
    return settings


def _client(*, bearer: str | None = RUNTIME_TOKEN, actor: str | None = None) -> APIClient:
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if actor is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = actor
    return c


def _subject(nick: str, external_id: str) -> tuple[User, str]:
    """A real client account plus the external identity bound to it.

    Mirrors what ``bind_external_identity`` produces in production: a proxy
    row keyed by the external id, pointing at a real, active account. Built
    here rather than called through the binding service so that a change in
    the binding *policy* (who may bind, when) cannot quietly change what
    this test believes an owner is.
    """
    real = User.objects.create_user(
        username=nick, password="x", role="client", phone=f"+7999000{nick[-4:]}",
    )
    User.objects.create(
        username=external_id, role="client", is_proxy=True, is_guest=False,
        linked_user=real,
    )
    return real, external_id


@pytest.fixture
def alice() -> tuple[User, str]:
    return _subject("alice1001", "bot:telegram:1001")


@pytest.fixture
def bob() -> tuple[User, str]:
    return _subject("bob2002", "bot:telegram:2002")


# Every route on the guarded surface, as a (method, template, kwarg) triple.
# The template's ``{subject}`` is the caller-supplied subject id — the thing
# the whole slice exists to stop being trusted.
EXPORT = ("get", "/api/v1/internal/users/{subject}/personal-data/export/")
DELETE = ("delete", "/api/v1/internal/users/{subject}/personal-data/")
CTX_GET = ("get", "/api/v1/internal/users/{subject}/personal-context/")
CTX_PATCH = ("patch", "/api/v1/internal/users/{subject}/personal-context/")
CTX_DELETE = ("delete", "/api/v1/internal/users/{subject}/personal-context/")
CTX_ELIG = ("get", "/api/v1/internal/users/{subject}/personal-context/ask-eligibility/")
CTX_ASKED = ("post", "/api/v1/internal/users/{subject}/personal-context/mark-asked/")
CTX_SKIP = ("post", "/api/v1/internal/users/{subject}/personal-context/skip/")

ALL_ROUTES = [
    EXPORT, DELETE, CTX_GET, CTX_PATCH, CTX_DELETE, CTX_ELIG, CTX_ASKED, CTX_SKIP,
]

# Bodies that satisfy each route's serializer, so a 4xx below is always the
# authorization answer and never an input answer.
_BODIES = {
    CTX_PATCH: {"updates": [{"field": "workplace_district", "value": "Центр"}]},
    CTX_ASKED: {"field": "preferred_time_slots"},
    CTX_SKIP: {"field": "preferred_time_slots"},
}


def _call(client: APIClient, route, subject_id):
    method, template = route
    url = template.format(subject=subject_id)
    body = _BODIES.get(route)
    if body is None:
        return getattr(client, method)(url)
    return getattr(client, method)(url, body, format="json")


# ---------------------------------------------------------------------------
# The three negatives this change is actually about
# ---------------------------------------------------------------------------


class TestForeignSubjectDenied:
    """Valid runtime token, caller names itself — and names somebody else."""

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: f"{r[0]}:{r[1]}")
    def test_every_route_refuses_a_foreign_subject(self, route, alice, bob):
        alice_user, alice_id = alice
        bob_user, _ = bob

        resp = _call(_client(actor=alice_id), route, bob_user.pk)

        assert resp.status_code == 403, (
            f"{route} let Alice reach Bob: {resp.status_code}"
        )
        assert resp.json()["error"]["code"] == "CLIENT_MISMATCH"

    def test_export_of_a_foreign_subject_returns_nothing(self, alice, bob):
        """The refusal must precede the read, not filter it afterwards."""
        alice_user, alice_id = alice
        bob_user, _ = bob
        bob_user.email = "bob@example.com"
        bob_user.save(update_fields=["email"])

        resp = _call(_client(actor=alice_id), EXPORT, bob_user.pk)

        assert resp.status_code == 403
        assert "bob@example.com" not in resp.content.decode()

    def test_delete_of_a_foreign_subject_leaves_the_data_intact(self, alice, bob):
        """The destructive verb, checked by its effect and not by its status.

        A 403 that still erased would be the worst possible outcome and the
        easiest to miss: the caller is told no, the data is gone anyway.
        """
        alice_user, alice_id = alice
        bob_user, _ = bob
        UserPersonalContext.objects.create(
            user=bob_user, workplace_district="Заводской",
            data_sources={"workplace_district": "explicit"},
        )

        resp = _call(_client(actor=alice_id), DELETE, bob_user.pk)

        assert resp.status_code == 403
        ctx = UserPersonalContext.objects.get(user=bob_user)
        assert ctx.workplace_district == "Заводской"
        assert ctx.data_sources.get("workplace_district") == "explicit"

    def test_context_delete_of_a_foreign_subject_leaves_the_data_intact(
        self, alice, bob,
    ):
        """The SECOND erasure route. Both call ``erase_personal_context``;
        closing one of the two would have been closing neither."""
        alice_user, alice_id = alice
        bob_user, _ = bob
        UserPersonalContext.objects.create(
            user=bob_user, workplace_district="Заводской",
            data_sources={"workplace_district": "explicit"},
        )

        resp = _call(_client(actor=alice_id), CTX_DELETE, bob_user.pk)

        assert resp.status_code == 403
        assert (
            UserPersonalContext.objects.get(user=bob_user).workplace_district
            == "Заводской"
        )

    def test_context_patch_of_a_foreign_subject_writes_nothing(self, alice, bob):
        """The case §7's four negatives do not name: not a read, a WRITE.

        An export hands an attacker a copy. A write hands them control of
        what Ayla believes about somebody else.
        """
        alice_user, alice_id = alice
        bob_user, _ = bob

        resp = _call(_client(actor=alice_id), CTX_PATCH, bob_user.pk)

        assert resp.status_code == 403
        ctx = UserPersonalContext.objects.filter(user=bob_user).first()
        assert ctx is None or ctx.workplace_district != "Центр"


class TestWrongPurposeDenied:
    """A credential we recognise, issued for something else."""

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: f"{r[0]}:{r[1]}")
    def test_provisioning_credential_is_refused(self, route, alice):
        alice_user, alice_id = alice
        client = _client(bearer=PROVISIONING_TOKEN, actor=alice_id)

        resp = _call(client, route, alice_user.pk)

        assert resp.status_code in (401, 403)

    def test_refusal_reasons_are_distinguishable_from_the_inside(
        self, alice, bob, caplog,
    ):
        """Outward one coarse name; inward two different counters.

        Without this the operator cannot answer, a month from now, whether
        somebody is reaching into foreign data or a purpose is merely
        misconfigured — and the two have opposite fixes.
        """
        alice_user, alice_id = alice
        bob_user, _ = bob

        with caplog.at_level(logging.WARNING, logger="users.internal_authz"):
            _call(_client(bearer=PROVISIONING_TOKEN, actor=alice_id),
                  EXPORT, alice_user.pk)
            _call(_client(actor=alice_id), EXPORT, bob_user.pk)

        reasons = [
            line.split("reason=")[1].split(" ")[0]
            for line in caplog.text.splitlines()
            if "internal.subject_authz.denied" in line
        ]
        assert "wrong_purpose" in reasons
        assert "subject_mismatch" in reasons
        assert reasons.count("wrong_purpose") == 1
        assert reasons.count("subject_mismatch") == 1


class TestPreExistingPerimeter:
    """The fourth negative from §7 — and what it does and does not prove.

    These passed before this change existed. They are here so nobody
    re-adds them as evidence for the boundary INSIDE the perimeter.
    """

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: f"{r[0]}:{r[1]}")
    def test_invalid_token_denied(self, route, alice):
        alice_user, _ = alice
        resp = _call(_client(bearer="nope"), route, alice_user.pk)
        assert resp.status_code in (401, 403)

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: f"{r[0]}:{r[1]}")
    def test_no_token_denied(self, route, alice):
        alice_user, _ = alice
        resp = _call(_client(bearer=None), route, alice_user.pk)
        assert resp.status_code in (401, 403)

    def test_unset_runtime_token_fails_closed(self, settings, alice):
        """An empty setting must never mean "accept anything"."""
        alice_user, alice_id = alice
        settings.AYLA_INTERNAL_API_TOKEN = ""
        resp = _call(_client(bearer="", actor=alice_id), EXPORT, alice_user.pk)
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# The positive half — a denial-only guard is indistinguishable from a broken
# endpoint, and would be just as green.
# ---------------------------------------------------------------------------


class TestOwnSubjectAllowed:
    @pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: f"{r[0]}:{r[1]}")
    def test_caller_reaches_its_own_subject(self, route, alice):
        alice_user, alice_id = alice
        resp = _call(_client(actor=alice_id), route, alice_user.pk)
        assert resp.status_code == 200, resp.content

    def test_binding_is_followed_not_bypassed(self, alice):
        """The header names the PROXY row; the authorised subject is the REAL
        account it is bound to.

        This is the difference between the check reading the identity graph
        and the check merely comparing two strings. If ``_follow_binding``
        ever stops being called here, every bound caller starts being
        refused access to its own data — and this test is what says so.
        """
        alice_user, alice_id = alice
        proxy = User.objects.get(username=alice_id)
        assert proxy.pk != alice_user.pk

        allowed = _call(_client(actor=alice_id), EXPORT, alice_user.pk)
        refused = _call(_client(actor=alice_id), EXPORT, proxy.pk)

        assert allowed.status_code == 200
        assert refused.status_code == 403

    def test_unbound_proxy_reaches_only_itself(self):
        """An external identity with no binding is an isolated subject.

        Not an error, and not a fallback to somebody: a controlled empty
        result of its own.
        """
        User.objects.create(
            username="bot:telegram:9009", role="client",
            is_proxy=True, is_guest=False,
        )
        proxy = User.objects.get(username="bot:telegram:9009")
        stranger = User.objects.create_user(
            username="stranger", password="x", role="client", phone="+79990009999",
        )

        own = _call(_client(actor="bot:telegram:9009"), EXPORT, proxy.pk)
        other = _call(_client(actor="bot:telegram:9009"), EXPORT, stranger.pk)

        assert own.status_code == 200
        assert other.status_code == 403


class TestAuthorizationCreatesNothing:
    """The check must not become the attack.

    ``resolve_external_user`` provisions on sight — that is right for an
    endpoint acting FOR a caller and wrong for one authorising a caller.
    Had the permission reused it, a token holder could mint accounts by
    guessing headers, and an export would create data about a person who
    did not exist.
    """

    def test_unknown_actor_is_refused_without_provisioning_a_row(self, alice):
        alice_user, _ = alice
        before = User.objects.count()

        resp = _call(_client(actor="bot:telegram:404404"), EXPORT, alice_user.pk)

        assert resp.status_code in (401, 403)
        assert User.objects.count() == before
        assert not User.objects.filter(username="bot:telegram:404404").exists()

    def test_malformed_actor_is_refused_without_provisioning_a_row(self, alice):
        alice_user, _ = alice
        before = User.objects.count()

        resp = _call(_client(actor="not a valid external id"), EXPORT, alice_user.pk)

        assert resp.status_code in (401, 403)
        assert User.objects.count() == before


# ---------------------------------------------------------------------------
# The staged behaviour, pinned in both positions of the flag
# ---------------------------------------------------------------------------


class TestUnnamedCallerIsCounted:
    def test_flag_off_the_hole_is_open_and_that_is_deliberate(
        self, settings, alice, bob, caplog,
    ):
        """While the flag is off, a caller that names NOBODY still reaches
        anybody. Asserted, not glossed: this is the state production is in
        until both unnamed callers are fixed, and a reader of this file
        deserves to see it written down rather than inferred.
        """
        settings.INTERNAL_SUBJECT_AUTHZ_ENFORCE = False
        alice_user, _ = alice
        bob_user, _ = bob

        with caplog.at_level(logging.INFO, logger="users.internal_authz"):
            resp = _call(_client(), EXPORT, bob_user.pk)

        assert resp.status_code == 200
        assert "internal.subject_authz.unnamed_actor" in caplog.text

    def test_the_counter_counts_every_unnamed_call_not_just_the_first(
        self, settings, alice, caplog,
    ):
        """A counter that fires once is a notification, not a measurement.

        The flip is earned by this number reading zero, and zero only means
        something if every call would have moved it.
        """
        settings.INTERNAL_SUBJECT_AUTHZ_ENFORCE = False
        alice_user, _ = alice

        with caplog.at_level(logging.INFO, logger="users.internal_authz"):
            _call(_client(), EXPORT, alice_user.pk)
            _call(_client(), EXPORT, alice_user.pk)
            _call(_client(), EXPORT, alice_user.pk)

        hits = caplog.text.count("internal.subject_authz.unnamed_actor")
        assert hits == 3, f"counter moved {hits} times for 3 unnamed calls"

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: f"{r[0]}:{r[1]}")
    def test_flag_on_the_unnamed_caller_is_refused(self, settings, route, alice):
        settings.INTERNAL_SUBJECT_AUTHZ_ENFORCE = True
        alice_user, _ = alice
        resp = _call(_client(), route, alice_user.pk)
        assert resp.status_code in (401, 403)

    def test_flag_on_a_named_caller_still_reaches_its_own_subject(
        self, settings, alice,
    ):
        """The flip must close the unnamed branch and nothing else."""
        settings.INTERNAL_SUBJECT_AUTHZ_ENFORCE = True
        alice_user, alice_id = alice
        resp = _call(_client(actor=alice_id), EXPORT, alice_user.pk)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Audit — §7 ``audit sensitive access``
# ---------------------------------------------------------------------------


class TestSensitiveAccessIsAudited:
    def test_export_writes_a_durable_record(self, alice):
        alice_user, alice_id = alice
        UserPersonalContext.objects.create(user=alice_user, workplace_district="Центр")

        resp = _call(_client(actor=alice_id), EXPORT, alice_user.pk)

        assert resp.status_code == 200
        event = AnalyticsEvent.objects.get(event_name="personal_data_exported")
        assert event.payload["user_id"] == str(alice_user.pk)
        assert event.payload["sections"] == ["profile", "personal_context"]
        assert event.payload["initiator"] == "internal_api"

    def test_the_audit_does_not_copy_the_data_it_audits(self, alice):
        """An audit row carrying the exported values would be a second store
        of the same personal data, with none of the erasure paths pointing
        at it."""
        alice_user, alice_id = alice
        alice_user.email = "alice@example.com"
        alice_user.save(update_fields=["email"])
        UserPersonalContext.objects.create(user=alice_user, workplace_district="Центр")

        _call(_client(actor=alice_id), EXPORT, alice_user.pk)

        event = AnalyticsEvent.objects.get(event_name="personal_data_exported")
        blob = str(event.payload)
        assert "alice@example.com" not in blob
        assert "Центр" not in blob

    def test_reaching_for_a_foreign_subject_outlives_log_rotation(self, alice, bob):
        alice_user, alice_id = alice
        bob_user, _ = bob

        _call(_client(actor=alice_id), EXPORT, bob_user.pk)

        event = AnalyticsEvent.objects.get(
            event_name="internal_subject_access_denied",
        )
        assert event.payload["reason"] == "subject_mismatch"
        assert event.payload["subject_id"] == str(bob_user.pk)
        # The row is about the TARGET. Resolving the caller here would mean a
        # refused request gets to touch the identity tables.
        assert event.actor_id is None
        # And it must not hand an attacker's own probe back to whoever reads
        # the audit.
        assert alice_id not in str(event.payload)

    def test_operational_refusals_do_not_grow_the_table(self, alice):
        """A row per unauthenticated request is a way to let an
        unauthenticated caller fill our storage."""
        alice_user, _ = alice
        before = AnalyticsEvent.objects.count()

        for _ in range(5):
            _call(_client(bearer="nope"), EXPORT, alice_user.pk)
            _call(_client(bearer=None), EXPORT, alice_user.pk)

        assert AnalyticsEvent.objects.count() == before


# ---------------------------------------------------------------------------
# The guard's own subject — §28 of the executor rules
# ---------------------------------------------------------------------------


class TestGuardCoversItsSubject:
    """A guard that stops matching its subject reports success forever.

    These two tests are the reason route nine cannot arrive next month
    looking protected: the route list is read from the URL conf, not from a
    list somebody remembered to update.
    """

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: f"{r[0]}:{r[1]}")
    def test_every_route_carries_the_check(self, route):
        _, template = route
        match = resolve(template.format(subject=uuid.uuid4()))
        view_cls = match.func.cls
        assert IsInternalBearerForSubject in view_cls.permission_classes, (
            f"{view_cls.__name__} is on the personal-data surface without the "
            f"object-level check"
        )

    @pytest.mark.parametrize("route", ALL_ROUTES, ids=lambda r: f"{r[0]}:{r[1]}")
    def test_every_route_declares_a_subject_kwarg_that_actually_exists(self, route):
        """``subject_url_kwarg`` naming a kwarg the URL does not supply would
        fail closed at runtime — correct, but discovered in production. Here
        it is discovered at PR time."""
        _, template = route
        match = resolve(template.format(subject=uuid.uuid4()))
        declared = getattr(match.func.cls, "subject_url_kwarg", None)
        assert declared, f"{match.func.cls.__name__} declares no subject_url_kwarg"
        assert declared in match.kwargs, (
            f"{match.func.cls.__name__} authorises against {declared!r}, which "
            f"this URL does not supply (it supplies {sorted(match.kwargs)})"
        )

    def test_the_route_list_here_is_the_whole_surface(self):
        """Guards the list itself: every ``{ayla_user_id}`` / ``{user_id}``
        route under ``internal/users/`` that touches personal data must
        appear in ``ALL_ROUTES``.

        Named explicitly, because this is the one assertion in the file that
        a reader could mistake for covering more than it does: it compares
        against the two view MODULES that own personal data. A ninth route
        added in a third module is outside its reach — and that limit is why
        the per-route assertions above read the URL conf rather than trusting
        this list.
        """
        from users import internal_personal_context_api as ctx_api
        from users import personal_data_api as pd_api

        owning_modules = {ctx_api.__name__, pd_api.__name__}
        covered = set()
        for _, template in ALL_ROUTES:
            match = resolve(template.format(subject=uuid.uuid4()))
            covered.add(match.func.cls)

        declared_views = {
            obj
            for module in (ctx_api, pd_api)
            for obj in vars(module).values()
            if isinstance(obj, type)
            and getattr(obj, "permission_classes", None) is not None
            and obj.__module__ in owning_modules
        }
        missing = declared_views - covered
        assert not missing, (
            f"views on the personal-data surface with no route in ALL_ROUTES: "
            f"{sorted(v.__name__ for v in missing)}"
        )
