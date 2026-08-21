"""DRF-1063 block D — the three things a front desk does all day.

Book someone in, move a booking, cancel one. Before this there was no
endpoint anywhere in Ayla where the actor was a salon employee: the
public create rejects non-clients, the internal one resolves the caller
INTO a client, and walk-in belongs to the master.

What these tests are mostly about is that the salon's actions are
recorded as the salon's — not laundered into the customer's. The
temptation with block D is to widen the existing customer endpoints,
which would have produced working buttons that write the wrong fact:
a salon cancellation attributed to the client, and priced with the
client's cancellation fee.

Also pinned here, from the Master Schedule UX Contract (15.08):
the service is a required input; availability is re-checked at commit;
a new guest needs a name and a phone.
"""
from __future__ import annotations

from uuid import uuid4
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment, OutboxEvent
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="manual-1063", name="Manual Salon")


@pytest.fixture
def other_salon(db):
    return Tenant.objects.create(slug="manual-1063-b", name="Other Manual")


def _make_master(tenant, username, phone, name):
    u = User.objects.create_user(
        username=username, password="x", role="specialist", phone=phone,
    )
    u.tenant = tenant
    u.save(update_fields=["tenant"])
    p = SpecialistProfile.objects.get(user=u)
    p.display_name = name
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.tenant = tenant
    p.save()
    return p


@pytest.fixture
def master(salon):
    return _make_master(salon, "man_master", "+79991050641", "Ольга")


@pytest.fixture
def foreign_master(other_salon):
    return _make_master(
        other_salon, "man_master_b", "+79991050642", "Чужая",
    )


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Man Cat", slug="man-cat")


@pytest.fixture
def service(master, category):
    return Service.objects.create(
        specialist=master, category=category, name="Массаж",
        price=Decimal("2000.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def admin_user(db, salon):
    # The username is the external id the bot sends in
    # X-External-User-ID: `resolve_external_user` keys off it, so this is
    # what makes the header resolve to *this* actor rather than to a
    # freshly provisioned proxy. Same shape as the pilot's real actor
    # (`bot:max:83146139`).
    u = User.objects.create_user(
        username="bot:max:1063admin", password="x", role="admin",
        phone="+79991050643",
    )
    TenantUserRelationship.objects.create(
        user=u, tenant=salon,
        role=TenantUserRelationship.Role.ADMIN, is_active=True,
    )
    return u


@pytest.fixture
def returning_client(db, salon):
    """Someone who already has a relationship with this salon."""
    # External-id shaped for the same reason as admin_user: this fixture
    # is both the customer being booked AND the actor in the negative
    # test that a mere customer may not operate the salon surface.
    u = User.objects.create_user(
        username="bot:max:1063client", password="x", role="client",
        phone="+79991050644", first_name="Анна", last_name="Кузнецова",
    )
    TenantUserRelationship.objects.create(
        user=u, tenant=salon,
        role=TenantUserRelationship.Role.CUSTOMER, is_active=True,
    )
    return u


@pytest.fixture
def stranger(db):
    """A registered Ayla user with no relationship to this salon."""
    return User.objects.create_user(
        username="man_stranger", password="x", role="client",
        phone="+79991050645", first_name="Пётр",
    )


SERVICE_TOKEN = "salon-surface-token-under-test"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _service_token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = SERVICE_TOKEN


def _api(user, *, tenant_slug=None, app_type=None, token=SERVICE_TOKEN) -> APIClient:
    """A client that authenticates the way the bot actually does.

    This deliberately does NOT use ``force_authenticate`` (DRF-1231).
    That helper installs ``request.user`` and skips authentication
    entirely — which is exactly the layer that refused every live request
    with 401 while this file stayed green. A test that bypasses the
    failing layer cannot see the failure, and this one did not: the
    endpoint was unreachable for as long as it existed, and a human found
    it, not CI.

    So: a real ``Authorization: Bearer`` plus ``X-External-User-ID``,
    resolved server-side into ``user`` by ``resolve_external_user``
    (hence the external-id-shaped usernames on the actor fixtures).

    ``X-App-Type`` is sent only when a test asks for it. The booking
    prefixes sit in ``EXCLUDED_PATH_PREFIXES``, so its absence is the
    normal case and its presence must change nothing.
    """
    c = APIClient()
    if app_type:
        c.defaults["HTTP_X_APP_TYPE"] = app_type
    if tenant_slug:
        c.defaults["HTTP_X_TENANT"] = tenant_slug
    if token:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    if user is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = user.username
    return c


def _slot(hours_ahead: int = 30) -> datetime:
    """A future slot aligned to the 30-minute booking grid."""
    base = datetime.now(tz=dt_timezone.utc) + timedelta(hours=hours_ahead)
    return base.replace(minute=0, second=0, microsecond=0)


def _create(api, *, service, master, when=None, idempotency_key=None, **client_fields):
    """Book someone in.

    ``X-Idempotency-Key`` is required by the endpoint (DRF-1232) and gets a
    fresh value per call, because each test is an independent booking. Pass
    ``idempotency_key`` explicitly to model a retry of the *same* request —
    which is the only case where reusing it is meaningful.
    """
    body = {
        "specialist_id": str(master.id),
        "service_id": str(service.id),
        "start_datetime": (when or _slot()).isoformat(),
    }
    body.update(client_fields)
    return api.post(
        "/api/v1/tenants/me/appointments/",
        body,
        format="json",
        HTTP_X_IDEMPOTENCY_KEY=idempotency_key or str(uuid4()),
    )


@pytest.mark.django_db(transaction=True)
class TestTheDoorIsOpenToTheBotAndNobodyElse:
    """DRF-1231 — the authentication layer, tested through the front door.

    Live measurement 2026-08-21: this surface answered **401
    token_not_valid** to the bot's service Bearer. The JWT authenticator
    from ``DEFAULT_AUTHENTICATION_CLASSES`` ran before permissions and
    refused a credential it was never meant to judge, so the console's
    buttons had nowhere to point. Every test in this file passed
    throughout, because they all went through ``force_authenticate``.

    The fix has three visible states, and each means something different:

    ===========================  =====  ==========================
    state                        code   meaning
    ===========================  =====  ==========================
    before DRF-1231              401    we are not let in at all
    after DRF-1231, no TUR       403    we are in; this actor may not
    after DRF-1228 (admin TUR)   201    the booking happens
    ===========================  =====  ==========================

    The middle one is not a failure — it is the door working while the
    key is still being cut in the other repo. These tests pin the two
    states that exist in code; the first is gone by construction, which
    ``test_a_service_bearer_is_no_longer_refused_at_authentication``
    asserts directly.
    """

    def test_a_service_bearer_is_no_longer_refused_at_authentication(
        self, salon, admin_user, master, service, returning_client,
    ):
        """The regression that started all of this: 401 must be gone.

        Asserted as «not 401» rather than «is 201» on purpose — this is
        about the authentication layer specifically, and it should keep
        holding when the permission answer below changes.
        """
        resp = _create(
            _api(admin_user, tenant_slug=salon.slug),
            service=service, master=master,
            client_id=str(returning_client.id),
        )
        assert resp.status_code != 401

    def test_an_admin_of_this_salon_gets_in(
        self, salon, admin_user, master, service, returning_client,
    ):
        resp = _create(
            _api(admin_user, tenant_slug=salon.slug),
            service=service, master=master,
            client_id=str(returning_client.id),
        )
        assert resp.status_code == 201

    def test_a_resolvable_actor_without_an_admin_tur_is_refused(
        self, salon, master, service, returning_client, django_user_model,
    ):
        """The state the pilot is in until DRF-1228 lands.

        `formula-tela` has no admin relationship for anyone at all
        (measured in the live database, 2026-08-21), so this is the
        answer the bot's actor gets today with the door already open.
        """
        actor = django_user_model.objects.create_user(
            username="bot:max:noroleactor", password="x", role="client",
            phone="+79991050649",
        )
        resp = _create(
            _api(actor, tenant_slug=salon.slug),
            service=service, master=master,
            client_id=str(returning_client.id),
        )
        assert resp.status_code == 403
        assert Appointment.objects.count() == 0

    def test_no_bearer_at_all_is_refused(
        self, salon, admin_user, master, service, returning_client,
    ):
        resp = _create(
            _api(admin_user, tenant_slug=salon.slug, token=None),
            service=service, master=master,
            client_id=str(returning_client.id),
        )
        assert resp.status_code == 403
        assert Appointment.objects.count() == 0

    def test_a_wrong_bearer_is_refused(
        self, salon, admin_user, master, service, returning_client,
    ):
        resp = _create(
            _api(admin_user, tenant_slug=salon.slug, token="not-the-token"),
            service=service, master=master,
            client_id=str(returning_client.id),
        )
        assert resp.status_code == 403
        assert Appointment.objects.count() == 0

    def test_the_right_bearer_naming_nobody_is_refused(
        self, salon, master, service, returning_client,
    ):
        """The token alone must not be enough to act.

        It is a single shared secret; the actor header is what says who
        is acting, and IsTenantAdmin is what says they may.
        """
        resp = _create(
            _api(None, tenant_slug=salon.slug),
            service=service, master=master,
            client_id=str(returning_client.id),
        )
        assert resp.status_code == 403

    def test_an_admin_of_another_salon_may_not_act_here(
        self, salon, other_salon, master, service, returning_client,
    ):
        """The (actor, tenant) tuple, not the actor alone.

        Without this, a leaked bearer plus any X-Tenant would be write
        access to every salon in Ayla — which is the whole reason
        IsTenantAdmin stays in the list next to the bearer check.
        """
        outsider = User.objects.create_user(
            username="bot:max:otheradmin", password="x", role="admin",
            phone="+79991050648",
        )
        TenantUserRelationship.objects.create(
            user=outsider, tenant=other_salon,
            role=TenantUserRelationship.Role.ADMIN, is_active=True,
        )
        resp = _create(
            _api(outsider, tenant_slug=salon.slug),
            service=service, master=master,
            client_id=str(returning_client.id),
        )
        assert resp.status_code == 403
        assert Appointment.objects.count() == 0

    def test_the_tenant_header_is_still_required(
        self, salon, admin_user, master, service, returning_client,
    ):
        """Excluded from X-App-Type, NOT from X-Tenant.

        These two exclusion lists are separate on purpose: IsTenantAdmin
        authorises against ``request.tenant``, which only exists because
        TenantContextMiddleware still reads the header on this path.
        """
        resp = _create(
            _api(admin_user),  # no tenant_slug
            service=service, master=master,
            client_id=str(returning_client.id),
        )
        assert resp.status_code == 403
        assert Appointment.objects.count() == 0

    def test_the_app_type_header_is_neither_required_nor_harmful(
        self, salon, admin_user, master, service, returning_client,
    ):
        """The path is excluded, so the header is simply not consulted.

        Sending it must not help and must not hurt — the bot is «neither
        client nor pro» and should not have to pretend otherwise.
        """
        resp = _create(
            _api(admin_user, tenant_slug=salon.slug, app_type="client"),
            service=service, master=master,
            client_id=str(returning_client.id),
        )
        assert resp.status_code == 201


@pytest.mark.django_db(transaction=True)
class TestIdempotencyKeyIsRequired:
    """DRF-1232 — the key must come from the caller, or de-duplication is a lie.

    ``Appointment.idempotency_key`` is unique and CreateBookingService looks
    the row up by it before creating anything. The view used to substitute a
    fresh uuid whenever the header was absent, which kept that lookup running
    against a value nothing had ever been stored under: the machinery
    executed, matched nothing by construction, and every retry produced a
    second booking behind a 201.

    Worth stating what this is NOT: reschedule and cancel pass
    ``command_key=... or None`` and never query by it — there the value is an
    audit trace, and repeats are caught by ``expected_version`` instead.
    Create is the only one of the three with key-based de-duplication, and so
    the only one a missing key silently disarms.
    """

    def test_a_missing_header_is_refused(self, salon, admin_user, master, service, returning_client):
        api = _api(admin_user, tenant_slug=salon.slug)
        resp = api.post(
            "/api/v1/tenants/me/appointments/",
            {
                "specialist_id": str(master.id),
                "service_id": str(service.id),
                "start_datetime": _slot().isoformat(),
                "client_id": str(returning_client.id),
            },
            format="json",
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
        assert Appointment.objects.count() == 0

    def test_a_blank_header_is_refused_too(self, salon, admin_user, master, service, returning_client):
        api = _api(admin_user, tenant_slug=salon.slug)
        resp = _create(
            api,
            service=service,
            master=master,
            client_id=str(returning_client.id),
            idempotency_key="   ",
        )
        assert resp.status_code == 400
        assert Appointment.objects.count() == 0

    def test_the_same_key_returns_the_same_booking(
        self, salon, admin_user, master, service, returning_client
    ):
        """The retry case the key exists for — and which never worked before.

        A caller whose write timed out repeats it with the same key. One
        appointment must exist afterwards, not two.
        """
        key = str(uuid4())
        when = _slot()

        api = _api(admin_user, tenant_slug=salon.slug)
        first = _create(
            api,
            service=service,
            master=master,
            when=when,
            client_id=str(returning_client.id),
            idempotency_key=key,
        )
        assert first.status_code == 201

        second = _create(
            api,
            service=service,
            master=master,
            when=when,
            client_id=str(returning_client.id),
            idempotency_key=key,
        )

        assert second.status_code in (200, 201)
        assert Appointment.objects.count() == 1
        # ``success_response`` wraps every payload in ``{"data": ...}``; the
        # first version of this assertion read the id off the top level and
        # died on the envelope rather than on the behaviour.
        assert second.json()["data"]["id"] == first.json()["data"]["id"]

    def test_different_keys_are_different_bookings(
        self, salon, admin_user, master, service, returning_client
    ):
        """Control for the test above: de-duplication keys off the value."""
        api = _api(admin_user, tenant_slug=salon.slug)
        first = _create(
            api,
            service=service,
            master=master,
            when=_slot(hours_ahead=30),
            client_id=str(returning_client.id),
        )
        second = _create(
            api,
            service=service,
            master=master,
            when=_slot(hours_ahead=54),
            client_id=str(returning_client.id),
        )
        assert first.status_code == 201
        assert second.status_code == 201
        assert Appointment.objects.count() == 2


@pytest.mark.django_db(transaction=True)
class TestSalonBooksACustomerIn:

    def test_books_a_returning_customer(
        self, salon, admin_user, master, service, returning_client,
    ):
        api = _api(admin_user, tenant_slug=salon.slug)

        resp = _create(
            api, service=service, master=master,
            client_id=str(returning_client.id),
        )

        assert resp.status_code == 201, resp.data
        appt = Appointment.objects.get(id=resp.data["data"]["id"])
        assert appt.client_id == returning_client.id
        assert appt.tenant_id == salon.id
        # No prepayment: the salon books, the customer pays at the salon.
        assert appt.status == Appointment.Status.CONFIRMED

    def test_the_event_says_the_salon_did_it(
        self, salon, admin_user, master, service, returning_client,
    ):
        api = _api(admin_user, tenant_slug=salon.slug)
        OutboxEvent.objects.all().delete()

        _create(
            api, service=service, master=master,
            client_id=str(returning_client.id),
        )

        created = OutboxEvent.objects.get(
            topic=OutboxEvent.Topic.BOOKING_CREATED,
        )
        # §3.1 `source` — the `origin` Appointment Contract §10 names as
        # what distinguishes a manual salon booking from a customer one.
        assert created.payload["data"]["source"] == "admin_console"
        # Envelope actor stays inside its pinned three values.
        assert created.payload["actor"] == "admin"
        # The affected user is the customer, not the operator.
        assert created.payload["user_id"] == str(returning_client.id)

    def test_books_a_new_guest_by_name_and_phone(
        self, salon, admin_user, master, service,
    ):
        api = _api(admin_user, tenant_slug=salon.slug)

        resp = _create(
            api, service=service, master=master,
            client_name="Мария Н.", client_phone="8 999 105 06 99",
        )

        assert resp.status_code == 201, resp.data
        appt = Appointment.objects.get(id=resp.data["data"]["id"])
        assert appt.client.first_name == "Мария Н."
        assert appt.client.is_proxy is True
        # The phone was ENTERED by the salon, not disclosed to it, and is
        # stored in the canonical +7 shape rather than as typed — so the
        # guest helper's "is this number already a real account?" check
        # compares like with like.
        assert appt.client.phone == "+79991050699"
        # Unlike the master's walk-in path, the number is NOT mirrored
        # into notes: there the master typed it themselves, here it would
        # put a number the salon collected in front of a master who never
        # saw it (DRF-1039).
        assert "9991050699" not in (appt.notes or "")

    def test_a_new_guest_without_a_phone_is_refused(
        self, salon, admin_user, master, service,
    ):
        """Master Schedule UX Contract: "минимальный новый клиент — имя
        и телефон"."""
        api = _api(admin_user, tenant_slug=salon.slug)

        resp = _create(
            api, service=service, master=master, client_name="Безымянная",
        )

        assert resp.status_code == 400
        assert not Appointment.objects.exists()

    def test_naming_both_a_customer_and_a_guest_is_refused(
        self, salon, admin_user, master, service, returning_client,
    ):
        api = _api(admin_user, tenant_slug=salon.slug)

        resp = _create(
            api, service=service, master=master,
            client_id=str(returning_client.id),
            client_name="Мария", client_phone="+79991050698",
        )

        assert resp.status_code == 400

    def test_naming_neither_is_refused(
        self, salon, admin_user, master, service,
    ):
        api = _api(admin_user, tenant_slug=salon.slug)

        resp = _create(api, service=service, master=master)

        assert resp.status_code == 400

    def test_the_service_is_required_never_inferred(
        self, salon, admin_user, master, returning_client,
    ):
        """The UX contract fixes Client → Service → Date/time precisely
        because the service's duration decides which intervals are
        usable. An endpoint that defaulted it would be answering a
        different question than the console asked."""
        api = _api(admin_user, tenant_slug=salon.slug)

        resp = api.post(
            "/api/v1/tenants/me/appointments/",
            {
                "specialist_id": str(master.id),
                "start_datetime": _slot().isoformat(),
                "client_id": str(returning_client.id),
            },
            format="json",
        )

        assert resp.status_code == 400
        assert "service_id" in str(resp.data)

    def test_availability_is_rechecked_at_commit(
        self, salon, admin_user, master, service, returning_client,
    ):
        """The contract requires the write to re-check, not to trust the
        interval the interface showed. The engine does it under an
        advisory lock inside the transaction; this pins that the salon
        path actually goes through it rather than around it."""
        api = _api(admin_user, tenant_slug=salon.slug)
        when = _slot()

        first = _create(
            api, service=service, master=master, when=when,
            client_id=str(returning_client.id),
        )
        assert first.status_code == 201

        second = _create(
            api, service=service, master=master, when=when,
            client_name="Вторая", client_phone="+79991050697",
        )

        assert second.status_code == 409
        assert second.data["error"]["code"] == "SLOT_NOT_AVAILABLE"
        assert Appointment.objects.count() == 1


@pytest.mark.django_db(transaction=True)
class TestManualBookingBoundaries:

    def test_a_master_of_another_salon_is_not_found(
        self, salon, admin_user, foreign_master, service, returning_client,
    ):
        api = _api(admin_user, tenant_slug=salon.slug)

        resp = _create(
            api, service=service, master=foreign_master,
            client_id=str(returning_client.id),
        )

        assert resp.status_code == 404
        assert not Appointment.objects.exists()

    def test_a_stranger_cannot_be_booked_by_id(
        self, salon, admin_user, master, service, stranger,
    ):
        """A booking grants the tenant a relationship with the customer,
        so accepting any user id would let one salon attach itself to
        people who have never dealt with it. Strangers go through the
        guest path, where the salon states who they are."""
        api = _api(admin_user, tenant_slug=salon.slug)

        resp = _create(
            api, service=service, master=master, client_id=str(stranger.id),
        )

        assert resp.status_code == 404
        assert not Appointment.objects.exists()

    def test_a_client_cannot_use_the_salon_surface(
        self, salon, master, service, returning_client,
    ):
        api = _api(returning_client, tenant_slug=salon.slug)

        resp = _create(
            api, service=service, master=master,
            client_id=str(returning_client.id),
        )

        assert resp.status_code == 403


@pytest.mark.django_db(transaction=True)
class TestSalonMovesABooking:

    def _booked(self, salon, admin_user, master, service, client):
        api = _api(admin_user, tenant_slug=salon.slug)
        resp = _create(
            api, service=service, master=master, client_id=str(client.id),
        )
        assert resp.status_code == 201, resp.data
        return Appointment.objects.get(id=resp.data["data"]["id"])

    def test_moves_a_booking_and_records_who_moved_it(
        self, salon, admin_user, master, service, returning_client,
    ):
        appt = self._booked(
            salon, admin_user, master, service, returning_client,
        )
        OutboxEvent.objects.all().delete()

        resp = _api(admin_user, tenant_slug=salon.slug).post(
            f"/api/v1/tenants/me/appointments/{appt.id}/reschedule/",
            {
                "new_start_datetime": _slot(hours_ahead=54).isoformat(),
                "expected_version": appt.version,
            },
            format="json",
        )

        assert resp.status_code == 200, resp.data
        appt.refresh_from_db()
        assert appt.version == 2
        revision = appt.revisions.get(version=2)
        assert revision.actor_role == "salon"
        assert revision.basis == "salon_console"
        assert revision.actor_id == admin_user.id

    def test_expected_version_is_mandatory_here(
        self, salon, admin_user, master, service, returning_client,
    ):
        """Optional on mobile only because app builds predating the field
        exist. The salon console reads the day journal — which carries
        every booking's version — immediately before offering the button."""
        appt = self._booked(
            salon, admin_user, master, service, returning_client,
        )

        resp = _api(admin_user, tenant_slug=salon.slug).post(
            f"/api/v1/tenants/me/appointments/{appt.id}/reschedule/",
            {"new_start_datetime": _slot(hours_ahead=54).isoformat()},
            format="json",
        )

        assert resp.status_code == 400

    def test_a_stale_version_is_409_and_moves_nothing(
        self, salon, admin_user, master, service, returning_client,
    ):
        appt = self._booked(
            salon, admin_user, master, service, returning_client,
        )
        original_start = appt.start_datetime

        resp = _api(admin_user, tenant_slug=salon.slug).post(
            f"/api/v1/tenants/me/appointments/{appt.id}/reschedule/",
            {
                "new_start_datetime": _slot(hours_ahead=54).isoformat(),
                "expected_version": appt.version + 5,
            },
            format="json",
        )

        assert resp.status_code == 409
        assert resp.data["error"]["code"] == "STALE_VERSION"
        appt.refresh_from_db()
        assert appt.start_datetime == original_start

    def test_a_booking_of_another_salon_is_not_found(
        self, salon, other_salon, admin_user, master, service,
        returning_client, foreign_master, category,
    ):
        appt = self._booked(
            salon, admin_user, master, service, returning_client,
        )
        # Address the salon we DO administer, reach for a booking in it —
        # then flip the booking's tenant to prove the guard is the tenant
        # filter and not the id.
        Appointment.objects.filter(pk=appt.pk).update(tenant=other_salon)

        resp = _api(admin_user, tenant_slug=salon.slug).post(
            f"/api/v1/tenants/me/appointments/{appt.id}/reschedule/",
            {
                "new_start_datetime": _slot(hours_ahead=54).isoformat(),
                "expected_version": appt.version,
            },
            format="json",
        )

        assert resp.status_code == 404


@pytest.mark.django_db(transaction=True)
class TestSalonCancels:

    def _booked(self, salon, admin_user, master, service, client, **kw):
        api = _api(admin_user, tenant_slug=salon.slug)
        resp = _create(
            api, service=service, master=master, client_id=str(client.id),
            **kw,
        )
        assert resp.status_code == 201, resp.data
        return Appointment.objects.get(id=resp.data["data"]["id"])

    def test_cancels_and_the_event_reads_as_admin(
        self, salon, admin_user, master, service, returning_client,
    ):
        appt = self._booked(
            salon, admin_user, master, service, returning_client,
        )
        OutboxEvent.objects.all().delete()

        resp = _api(admin_user, tenant_slug=salon.slug).post(
            f"/api/v1/tenants/me/appointments/{appt.id}/cancel/",
            {"reason": "салон закрыт"}, format="json",
        )

        assert resp.status_code == 200, resp.data
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CANCELLED
        evt = OutboxEvent.objects.get(
            topic=OutboxEvent.Topic.BOOKING_CANCELLED,
        )
        # §3.2 declares `admin` in the cancelled_by enum; nothing in Ayla
        # produced it until the salon could act.
        assert evt.payload["data"]["cancelled_by"] == "admin"
        assert evt.payload["data"]["initiator_role"] == "salon"

    def test_the_salon_may_state_a_reason_the_client_can_be_told(
        self, salon, admin_user, master, service, returning_client,
    ):
        appt = self._booked(
            salon, admin_user, master, service, returning_client,
        )
        OutboxEvent.objects.all().delete()

        _api(admin_user, tenant_slug=salon.slug).post(
            f"/api/v1/tenants/me/appointments/{appt.id}/cancel/",
            {"reason_code": "master_unavailable", "reason": "мастер заболел"},
            format="json",
        )

        evt = OutboxEvent.objects.get(
            topic=OutboxEvent.Topic.BOOKING_CANCELLED,
        )
        assert evt.payload["data"]["reason_code"] == "master_unavailable"

    def test_an_unlisted_reason_code_is_refused(
        self, salon, admin_user, master, service, returning_client,
    ):
        """The allowlist is narrow on purpose: `user_*` codes are the
        client's business and `payment_hold_expired` is the payment
        system's fact. Letting the salon claim either would let one party
        author another's attribution."""
        appt = self._booked(
            salon, admin_user, master, service, returning_client,
        )

        resp = _api(admin_user, tenant_slug=salon.slug).post(
            f"/api/v1/tenants/me/appointments/{appt.id}/cancel/",
            {"reason_code": "user_changed_plans"}, format="json",
        )

        assert resp.status_code == 400
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED

    def test_the_client_is_not_charged_for_the_salons_decision(
        self, salon, admin_user, master, service, returning_client,
    ):
        """The 24h/2h fee schedule prices the CLIENT changing their mind.
        Before DRF-1064 the only initiator exempt from it was
        `specialist`, so a front desk cancelling an hour before would
        have handed the customer a 0% refund for a decision that was
        never theirs."""
        from appointments.domain.policies import StandardCancellationPolicy

        soon = datetime.now(tz=dt_timezone.utc) + timedelta(minutes=30)
        policy = StandardCancellationPolicy()

        assert policy.get_refund_percent(
            booking_start_at=soon, initiator="salon",
        ) == 100.0
        assert policy.get_refund_percent(
            booking_start_at=soon, initiator="client",
        ) == 0.0


@pytest.mark.django_db
class TestCustomerLookup:

    def test_finds_a_returning_customer_by_name(
        self, salon, admin_user, returning_client,
    ):
        resp = _api(admin_user, tenant_slug=salon.slug).get(
            "/api/v1/tenants/me/customers/?q=Анн",
        )

        assert resp.status_code == 200, resp.data
        results = resp.data["data"]["results"]
        assert [r["id"] for r in results] == [str(returning_client.id)]
        assert results[0]["name"] == "Анна Кузнецова"

    def test_finds_by_exact_phone_but_never_returns_one(
        self, salon, admin_user, returning_client,
    ):
        """The phone is an input, never an output. A search endpoint is
        the classic way to leak back the numbers DRF-1039 keeps in."""
        resp = _api(admin_user, tenant_slug=salon.slug).get(
            "/api/v1/tenants/me/customers/?q=89991050644",
        )

        assert resp.status_code == 200
        assert len(resp.data["data"]["results"]) == 1
        assert "+79991050644" not in str(resp.data)
        assert "phone" not in str(resp.data)

    def test_does_not_find_people_who_are_not_your_customers(
        self, salon, admin_user, stranger,
    ):
        resp = _api(admin_user, tenant_slug=salon.slug).get(
            "/api/v1/tenants/me/customers/?q=Пёт",
        )

        assert resp.data["data"]["results"] == []

    def test_a_one_character_query_is_refused(
        self, salon, admin_user, returning_client,
    ):
        """Answers "which of my customers is this?", not "list them"."""
        resp = _api(admin_user, tenant_slug=salon.slug).get(
            "/api/v1/tenants/me/customers/?q=А",
        )

        assert resp.status_code == 400

    def test_a_client_cannot_search_customers(
        self, salon, returning_client,
    ):
        resp = _api(returning_client, tenant_slug=salon.slug).get(
            "/api/v1/tenants/me/customers/?q=Анн",
        )

        assert resp.status_code == 403
