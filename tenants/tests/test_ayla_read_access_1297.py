"""DRF-1297 В-1 — Ayla reads the salon, and only the salon, and only reads.

Three properties, and the file is arranged around them.

**It reaches the tenant tree at all.** Before this, the service Bearer
could reach four salon *write* endpoints and nothing else: the day
journal and the DRF-1062 schedule surface run the JWT authenticator,
which raises 401 on an opaque token before any permission is consulted.
Ayla's most basic question — "what is happening in the salon today" —
had no endpoint it could call.

**It cannot read someone else's salon.** The obvious cheap alternative
was a read-only bearer on ``/api/v1/internal/``. That tree is excluded
from ``TenantContextMiddleware`` and its handles are not tenant-scoped —
the specialist list returns every active master on the platform. Here the
tenant is named in ``X-Tenant`` *and* has to be one the human named by
``X-External-User-ID`` actually administers, so naming a slug is not
enough. :class:`TestItIsScopedToOneTenant` is the test that matters.

**A read grant is not a write grant.** Four of the six surfaces are mixed
classes — ``get`` and ``put``/``post`` share one ``permission_classes``.
:class:`TestTheReadGrantDoesNotWrite` pins every one of them: the same
credential that just returned 200 on ``GET`` must be refused on the write
method of the same URL.

Every request here goes through the real authentication stack.
``force_authenticate`` is deliberately not used: it short-circuits
``_authenticate()`` entirely, which is exactly the layer under test —
and is why DRF-1231's live 401 went unnoticed by a green suite.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from appointments.models import SpecialistWorkingHours
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User

SERVICE_TOKEN = "ayla-read-token-under-test"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _service_token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = SERVICE_TOKEN


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="r1297-a", name="Read Salon A")


@pytest.fixture
def other_salon(db):
    return Tenant.objects.create(slug="r1297-b", name="Read Salon B")


def _admin_of(tenant, *, username, phone):
    """A person who administers a salon.

    Role stays ``client`` on purpose. Authority in a salon comes from the
    relationship row and nothing else — ``IsTenantAdmin`` never reads
    ``User.role`` — and the pilot's administrator is a client account
    promoted by a grant, because ``bind_external_identity`` refuses to
    bind an external identity to a staff/admin account.
    """
    user = User.objects.create_user(
        username=username, password="x", role="client", phone=phone,
    )
    TenantUserRelationship.objects.create(
        user=user, tenant=tenant,
        role=TenantUserRelationship.Role.ADMIN, is_active=True,
    )
    return user


@pytest.fixture
def admin_a(salon):
    return _admin_of(salon, username="bot:max:r1297a", phone="+79995201001")


@pytest.fixture
def outsider(db):
    """A real account with no grant anywhere — the "token alone" case."""
    return User.objects.create_user(
        username="bot:max:r1297out", password="x", role="client",
        phone="+79995201009",
    )


def _master(tenant, *, username, phone, name):
    user = User.objects.create_user(
        username=username, password="x", role="specialist", phone=phone,
    )
    user.tenant = tenant
    user.save(update_fields=["tenant"])
    profile = SpecialistProfile.objects.get(user=user)
    profile.display_name = name
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.timezone = "Europe/Moscow"
    profile.tenant = tenant
    profile.save()
    return profile


@pytest.fixture
def master(salon):
    return _master(
        salon, username="r1297_master", phone="+79995201002", name="Ольга",
    )


@pytest.fixture
def other_master(other_salon):
    return _master(
        other_salon, username="r1297_master_b", phone="+79995201003",
        name="Инна",
    )


def _ayla(external_user_id, *, tenant_slug, token=SERVICE_TOKEN) -> APIClient:
    """The bot's real credential set — no ``force_authenticate``.

    ``X-App-Type: pro`` is part of the contract, not a workaround.
    ``IsProApp`` stays on every one of these views: excluding the prefix
    from ``AppTypeMiddleware`` would set ``request.app_type = None`` and
    make ``IsProApp`` permanently unsatisfiable *for every caller*, which
    is a far wider change than declaring a header.
    """
    client = APIClient()
    client.defaults["HTTP_X_APP_TYPE"] = "pro"
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    if external_user_id is not None:
        client.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    if tenant_slug:
        client.defaults["HTTP_X_TENANT"] = tenant_slug
    return client


def _human(user, *, tenant_slug) -> APIClient:
    """A salon administrator on a real JWT — the console's path.

    Issued rather than ``force_authenticate``d because the point of these
    tests is that adding an authenticator in front of JWT did not disturb
    JWT.
    """
    client = APIClient()
    client.defaults["HTTP_X_APP_TYPE"] = "pro"
    access = RefreshToken.for_user(user).access_token
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {access}"
    if tenant_slug:
        client.defaults["HTTP_X_TENANT"] = tenant_slug
    return client


def _full_week() -> list[dict]:
    """PUT replaces the template wholesale and requires all seven days."""
    return [
        {
            "day_of_week": day,
            "is_working_day": day < 5,
            "start_time": "10:00" if day < 5 else None,
            "end_time": "19:00" if day < 5 else None,
            "break_start": None,
            "break_end": None,
        }
        for day in range(7)
    ]


def _day_url() -> str:
    return "/api/v1/tenants/me/day/"


def _schedule_url(master) -> str:
    return f"/api/v1/tenants/me/masters/{master.id}/schedule/"


def _time_off_url(master) -> str:
    return f"/api/v1/tenants/me/masters/{master.id}/time-off/"


def _exceptions_url(master) -> str:
    return f"/api/v1/tenants/me/masters/{master.id}/schedule-exceptions/"


def _closures_url() -> str:
    return "/api/v1/tenants/me/closures/"


@pytest.mark.django_db
class TestItReachesTheTenantTree:
    def test_the_day_journal_answers_the_service_credential(
        self, salon, admin_a, master
    ):
        """The endpoint Ayla exists to call. Unreachable before DRF-1297:
        the JWT authenticator refused the service Bearer with 401 before
        IsTenantAdmin ever ran."""
        resp = _ayla(admin_a.username, tenant_slug=salon.slug).get(_day_url())

        assert resp.status_code == 200, resp.data
        names = [m["display_name"] for m in resp.data["data"]["masters"]]
        assert "Ольга" in names

    @pytest.mark.parametrize(
        "url_factory",
        [
            pytest.param(lambda m: _schedule_url(m), id="weekly-template"),
            pytest.param(lambda m: _time_off_url(m), id="time-off"),
            pytest.param(lambda m: _exceptions_url(m), id="schedule-exceptions"),
        ],
    )
    def test_the_schedule_reads_answer_the_service_credential(
        self, salon, admin_a, master, url_factory
    ):
        resp = _ayla(admin_a.username, tenant_slug=salon.slug).get(
            url_factory(master)
        )

        assert resp.status_code == 200, resp.data

    def test_the_closure_list_answers_the_service_credential(
        self, salon, admin_a, master
    ):
        resp = _ayla(admin_a.username, tenant_slug=salon.slug).get(_closures_url())

        assert resp.status_code == 200, resp.data


@pytest.mark.django_db
class TestItIsScopedToOneTenant:
    def test_naming_another_salon_reads_nothing(
        self, salon, other_salon, admin_a, other_master
    ):
        """The whole argument for putting the read grant here rather than
        on the internal tree. The token is valid, the slug is valid, and
        the answer is still no — because this human administers A."""
        resp = _ayla(admin_a.username, tenant_slug=other_salon.slug).get(_day_url())

        assert resp.status_code == 403, resp.data

    def test_a_master_of_another_salon_is_not_found(
        self, salon, admin_a, other_master
    ):
        """Cross-tenant by master id rather than by slug. 404, not 403 —
        the surface does not confirm which ids exist elsewhere."""
        resp = _ayla(admin_a.username, tenant_slug=salon.slug).get(
            _schedule_url(other_master)
        )

        assert resp.status_code == 404, resp.data

    def test_the_token_alone_authorises_nothing(self, salon, outsider):
        """A leaked service token plus a slug is not access. Authority
        comes from the relationship of the human it names."""
        resp = _ayla(outsider.username, tenant_slug=salon.slug).get(_day_url())

        assert resp.status_code == 403, resp.data

    def test_an_unknown_external_id_does_not_become_an_administrator(
        self, salon
    ):
        """``resolve_external_user`` creates a proxy client on first sight.
        That account has no grant, so a caller cannot mint authority by
        inventing an id."""
        resp = _ayla("bot:max:neverseen", tenant_slug=salon.slug).get(_day_url())

        assert resp.status_code == 403, resp.data

    def test_no_tenant_header_reads_nothing(self, salon, admin_a):
        resp = _ayla(admin_a.username, tenant_slug=None).get(_day_url())

        assert resp.status_code in (400, 403), resp.data


@pytest.mark.django_db
class TestTheReadGrantDoesNotWrite:
    """Four of the six surfaces are mixed ``get`` + write classes.

    Each case below is a URL the credential can GET. The assertion is
    that the write verb on the same URL is refused — 403 from
    ``ServiceCredentialIsReadOnly``, never a 200 and never a 500.
    """

    def test_the_weekly_template_cannot_be_replaced(
        self, salon, admin_a, master
    ):
        api = _ayla(admin_a.username, tenant_slug=salon.slug)
        assert api.get(_schedule_url(master)).status_code == 200

        resp = api.put(
            _schedule_url(master),
            {"schedule": [{
                "day_of_week": 0, "is_working_day": True,
                "start_time": "10:00", "end_time": "19:00",
            }]},
            format="json",
        )

        assert resp.status_code == 403, resp.data
        assert not SpecialistWorkingHours.objects.filter(
            specialist=master
        ).exists()

    def test_the_weekly_template_cannot_be_patched(
        self, salon, admin_a, master
    ):
        resp = _ayla(admin_a.username, tenant_slug=salon.slug).patch(
            _schedule_url(master),
            {"schedule": [{
                "day_of_week": 0, "is_working_day": False,
                "start_time": None, "end_time": None,
            }]},
            format="json",
        )

        assert resp.status_code == 403, resp.data

    def test_an_absence_cannot_be_recorded(self, salon, admin_a, master):
        start = "2026-09-01T10:00:00Z"
        resp = _ayla(admin_a.username, tenant_slug=salon.slug).post(
            _time_off_url(master),
            {"start_at": start, "end_at": "2026-09-01T12:00:00Z"},
            format="json",
        )

        assert resp.status_code == 403, resp.data

    def test_a_date_override_cannot_be_set(self, salon, admin_a, master):
        resp = _ayla(admin_a.username, tenant_slug=salon.slug).put(
            _exceptions_url(master),
            {"date": "2026-09-02", "is_working_day": False},
            format="json",
        )

        assert resp.status_code == 403, resp.data

    def test_the_salon_cannot_be_closed(self, salon, admin_a):
        resp = _ayla(admin_a.username, tenant_slug=salon.slug).post(
            _closures_url(),
            {"date": "2026-09-03"},
            format="json",
        )

        assert resp.status_code == 403, resp.data

    def test_a_date_override_cannot_be_deleted(self, salon, admin_a, master):
        """DELETE is not a safe method either. Named separately because
        the detail views are the ones with no GET at all — the place a
        blanket "it is a read surface" assumption would quietly fail."""
        resp = _ayla(admin_a.username, tenant_slug=salon.slug).delete(
            f"{_exceptions_url(master)}2026-09-02/"
        )

        assert resp.status_code == 403, resp.data


@pytest.mark.django_db
class TestTheHumanPathIsUnchanged:
    """The regression that would be easy to cause and hard to notice.

    An authenticator placed in front of JWT that raised instead of
    abstaining would turn every salon-console request into a 401.
    """

    def test_a_salon_administrator_still_reads_over_jwt(
        self, salon, admin_a, master
    ):
        resp = _human(admin_a, tenant_slug=salon.slug).get(_day_url())

        assert resp.status_code == 200, resp.data

    def test_a_salon_administrator_still_writes_over_jwt(
        self, salon, admin_a, master
    ):
        """``ServiceCredentialIsReadOnly`` must abstain for a human. If it
        keyed off the view instead of the credential, this would 403 and
        the console would lose schedule editing."""
        resp = _human(admin_a, tenant_slug=salon.slug).put(
            _schedule_url(master), {"schedule": _full_week()}, format="json",
        )

        assert resp.status_code == 200, resp.data
        assert SpecialistWorkingHours.objects.filter(
            specialist=master, day_of_week=0,
        ).exists()

    def test_a_wrong_service_token_is_refused(self, salon, admin_a):
        resp = _ayla(
            admin_a.username, tenant_slug=salon.slug, token="not-the-token",
        ).get(_day_url())

        assert resp.status_code == 401, resp.data

    def test_a_valid_token_without_an_external_id_is_refused(
        self, salon, admin_a
    ):
        """No named human means no authority to check. The authenticator
        abstains, JWT then rejects the opaque token."""
        resp = _ayla(None, tenant_slug=salon.slug).get(_day_url())

        assert resp.status_code == 401, resp.data

    def test_an_unset_service_token_is_not_a_wildcard(
        self, settings, salon, admin_a
    ):
        """Fail closed on a misconfigured deployment: an empty setting
        must not make every bearer acceptable."""
        settings.AYLA_INTERNAL_API_TOKEN = ""

        resp = _ayla(admin_a.username, tenant_slug=salon.slug, token="").get(
            _day_url()
        )

        assert resp.status_code == 401, resp.data


@pytest.mark.django_db
class TestTheImpactPreviewIsReadable:
    """Ayla has to be able to look before it proposes an absence.

    The preview is the read half of the one operation that already has a
    full Conflict Guard, so it is the one place the bot can honestly say
    "this would displace these bookings".
    """

    def test_the_preview_answers_the_service_credential(
        self, salon, admin_a, master
    ):
        start = date.today() + timedelta(days=3)
        url = (
            f"/api/v1/tenants/me/masters/{master.id}/schedule/impact/"
            f"?start_at={start}T10:00:00Z&end_at={start}T12:00:00Z"
        )

        resp = _ayla(admin_a.username, tenant_slug=salon.slug).get(url)

        assert resp.status_code == 200, resp.data
        assert resp.data["data"]["bookings"] == []
