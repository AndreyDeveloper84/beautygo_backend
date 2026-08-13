"""DRF-1043 — GET /api/v1/internal/me/identity/ (backend half of DRF-1035).

Test plan mirrors the brief's §6, one class per required group:

* Authentication — the four gates on the way in.
* Identity resolution — same subject in, same UUID out; first sight
  provisions; ``is_proxy`` tracks the model.
* Isolation — there is no subject selector, so caller A cannot name B.
* Booking-contract compatibility — the UUID this endpoint hands back is
  the one the existing ``client_id`` cross-check accepts, and a
  different one is still rejected. Proven at the cross-check boundary
  (no real booking is created) per the brief's explicit allowance.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from users.models import User
from users.services import bind_external_identity, resolve_external_user


URL = "/api/v1/internal/me/identity/"
BOOKING_CREATE_URL = "/api/v1/internal/appointments/"

VALID_TOKEN = "test-ayla-internal-token-1043"
EXTERNAL_ID_A = "bot:max:1043-subject-a"
EXTERNAL_ID_B = "bot:max:1043-subject-b"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


def _api(
    *,
    token: str | None = VALID_TOKEN,
    external_user_id: str | None = EXTERNAL_ID_A,
) -> APIClient:
    """Bot-shaped client. ``None`` for either argument omits the header
    entirely — that is the "missing" case, distinct from "wrong"."""
    client = APIClient()
    if token is not None:
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    if external_user_id is not None:
        client.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return client


def _payload(response):
    """Unwrap the project's ``{"data": ...}`` success envelope."""
    return response.json()["data"]


# ---------------------------------------------------------------------------
# §6 — Authentication
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuthentication:
    def test_no_bearer_is_refused(self):
        response = _api(token=None).get(URL)
        assert response.status_code == 403

    def test_wrong_bearer_is_refused(self):
        response = _api(token="definitely-not-the-token").get(URL)
        assert response.status_code == 403

    def test_empty_bearer_is_refused(self):
        response = _api(token="").get(URL)
        assert response.status_code == 403

    def test_missing_external_subject_is_refused(self):
        """Valid service token, no subject named → refusal, not a
        successful call resolving to nobody."""
        response = _api(external_user_id=None).get(URL)
        assert response.status_code == 403

    def test_malformed_external_subject_is_refused(self):
        response = _api(external_user_id="not a valid id").get(URL)
        assert response.status_code == 403

    def test_unconfigured_service_token_fails_closed(self, settings):
        """An empty ``AYLA_INTERNAL_API_TOKEN`` must never accept a
        bearer — a misconfigured deployment refuses everyone rather than
        everyone."""
        settings.AYLA_INTERNAL_API_TOKEN = ""
        response = _api(token="").get(URL)
        assert response.status_code == 403

    def test_valid_caller_with_valid_subject_succeeds(self):
        response = _api().get(URL)
        assert response.status_code == 200
        body = _payload(response)
        assert set(body) == {"ayla_user_id", "is_proxy"}

    def test_no_pii_in_response(self):
        """§2 minimality, enforced at the wire: exactly two keys. Any
        widening of the payload fails here before review."""
        body = _payload(_api().get(URL))
        assert set(body) == {"ayla_user_id", "is_proxy"}
        for leaked in ("phone", "email", "first_name", "last_name",
                       "username", "tenant", "tenant_id", "profile",
                       "consents", "personal_context"):
            assert leaked not in body

    def test_refused_call_does_not_provision_a_proxy(self):
        """A rejected caller must not leave an account behind — the
        denial path must never reach ``resolve_external_user``."""
        external_id = "bot:max:1043-never-provisioned"
        response = _api(
            token="wrong-token", external_user_id=external_id,
        ).get(URL)
        assert response.status_code == 403
        assert not User.objects.filter(username=external_id).exists()


# ---------------------------------------------------------------------------
# §6 — Identity resolution
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestIdentityResolution:
    def test_first_call_provisions_the_proxy(self):
        external_id = "bot:max:1043-brand-new"
        assert not User.objects.filter(username=external_id).exists()

        response = _api(external_user_id=external_id).get(URL)

        assert response.status_code == 200
        created = User.objects.get(username=external_id)
        assert created.is_proxy is True
        assert created.role == "client"
        assert created.is_guest is False
        assert _payload(response)["ayla_user_id"] == str(created.id)

    def test_existing_proxy_returns_the_same_identifier(self):
        existing = resolve_external_user(EXTERNAL_ID_A)

        body = _payload(_api(external_user_id=EXTERNAL_ID_A).get(URL))

        assert body["ayla_user_id"] == str(existing.id)
        assert body["is_proxy"] is True

    def test_repeat_call_is_stable_and_creates_nothing(self):
        client = _api(external_user_id=EXTERNAL_ID_A)

        first = _payload(client.get(URL))
        count_after_first = User.objects.count()
        second = _payload(client.get(URL))

        assert first["ayla_user_id"] == second["ayla_user_id"]
        assert User.objects.count() == count_after_first

    def test_provisioned_proxy_carries_no_personal_data(self):
        """§1 of the brief, re-proven here because this endpoint is now
        the thing that triggers provisioning: a proxy is an empty shell."""
        external_id = "bot:max:1043-empty-shell"
        _api(external_user_id=external_id).get(URL)

        created = User.objects.get(username=external_id)
        assert not created.phone
        assert not created.email
        assert not created.first_name
        assert not created.last_name

    def test_is_proxy_false_for_a_bound_real_account(self):
        """Phase C binding: the resolver follows ``linked_user`` to the
        REAL account, so the flag must report the model — not a constant."""
        real = User.objects.create_user(
            username="drf1043-real-customer", password="x", role="client",
            phone="+79995551043", is_proxy=False, is_verified=True,
        )
        external_id = "bot:max:1043-bound"
        bind_external_identity(external_id, real.id)

        body = _payload(_api(external_user_id=external_id).get(URL))

        assert body["ayla_user_id"] == str(real.id)
        assert body["is_proxy"] is False

    def test_distinct_subjects_get_distinct_identifiers(self):
        a = _payload(_api(external_user_id=EXTERNAL_ID_A).get(URL))
        b = _payload(_api(external_user_id=EXTERNAL_ID_B).get(URL))

        assert a["ayla_user_id"] != b["ayla_user_id"]


# ---------------------------------------------------------------------------
# §6 — Isolation: no arbitrary subject selector
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestIsolation:
    def test_query_parameters_cannot_name_another_subject(self):
        """Caller A asks for B every way the URL allows. The endpoint
        reads no query parameters at all, so every attempt resolves to A."""
        subject_a = resolve_external_user(EXTERNAL_ID_A)
        subject_b = resolve_external_user(EXTERNAL_ID_B)
        client = _api(external_user_id=EXTERNAL_ID_A)

        attempts = (
            f"?external_user_id={EXTERNAL_ID_B}",
            f"?ayla_user_id={subject_b.id}",
            f"?user_id={subject_b.id}",
            f"?username={EXTERNAL_ID_B}",
            f"?id={subject_b.id}",
        )
        for query in attempts:
            response = client.get(f"{URL}{query}")
            assert response.status_code == 200, query
            assert _payload(response)["ayla_user_id"] == str(subject_a.id), query

    def test_the_only_subject_selector_is_the_authenticated_header(self):
        subject_b = resolve_external_user(EXTERNAL_ID_B)

        body = _payload(_api(external_user_id=EXTERNAL_ID_B).get(URL))

        assert body["ayla_user_id"] == str(subject_b.id)

    def test_no_write_surface(self):
        """GET only. A body-carrying method would reopen the
        subject-substitution surface the GET shape closes."""
        client = _api()
        for method in ("post", "put", "patch", "delete"):
            response = getattr(client, method)(URL, {}, format="json")
            assert response.status_code == 405, method


# ---------------------------------------------------------------------------
# §6 — Compatibility with the booking-create contract
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBookingContractCompatibility:
    """The point of DRF-1035: the id this endpoint returns is exactly the
    one ``InternalBookingCreateView``'s ``client_id`` cross-check accepts.

    Proven at the cross-check boundary rather than by creating a real
    booking (brief §6 allows this): the specialist/service ids are
    deliberately unknown, so a request that clears the cross-check fails
    *later*, on lookup. What matters is which of the two gates rejects it.
    """

    @staticmethod
    def _create_body(client_id) -> dict:
        return {
            "client_id": str(client_id),
            # Unknown-but-well-formed ids: the request must die on lookup
            # (after the cross-check), never on the cross-check itself.
            "specialist_id": "00000000-0000-0000-0000-0000000000aa",
            "service_id": "00000000-0000-0000-0000-0000000000bb",
            "start_datetime": "2099-01-01T10:00:00Z",
        }

    def test_returned_id_passes_the_client_id_cross_check(self):
        client = _api(external_user_id=EXTERNAL_ID_A)
        ayla_user_id = _payload(client.get(URL))["ayla_user_id"]

        response = client.post(
            BOOKING_CREATE_URL, self._create_body(ayla_user_id),
            format="json",
        )

        # Not a CLIENT_MISMATCH: the cross-check accepted the id and the
        # request proceeded into booking logic (where the fake specialist
        # is what kills it).
        assert response.status_code != 403, response.content
        assert b"CLIENT_MISMATCH" not in response.content

    def test_another_subjects_id_is_still_rejected(self):
        subject_b = resolve_external_user(EXTERNAL_ID_B)
        client = _api(external_user_id=EXTERNAL_ID_A)

        response = client.post(
            BOOKING_CREATE_URL, self._create_body(subject_b.id),
            format="json",
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CLIENT_MISMATCH"

    def test_cross_check_is_not_weakened_by_this_change(self):
        """Regression guard: a random UUID that belongs to nobody is
        rejected the same way a real other-subject id is."""
        client = _api(external_user_id=EXTERNAL_ID_A)

        response = client.post(
            BOOKING_CREATE_URL,
            self._create_body("11111111-2222-3333-4444-555555555555"),
            format="json",
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CLIENT_MISMATCH"


# ---------------------------------------------------------------------------
# Backward compatibility with the CURRENT bot release (brief §4)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBackwardCompatibility:
    """The bot version running in the pilot does not know this endpoint.
    Deploying must be invisible to it — these assert the neighbouring
    surfaces it *does* call are untouched."""

    def test_sibling_me_surface_still_answers(self):
        response = _api().get("/api/v1/internal/me/bookings/")
        assert response.status_code == 200

    def test_new_route_does_not_shadow_the_bookings_include(self):
        """``me/identity/`` and ``me/bookings/`` are separate prefixes;
        adding one must not capture the other's paths."""
        from django.urls import resolve

        assert resolve("/api/v1/internal/me/identity/").url_name == (
            "internal-me-identity"
        )
        assert resolve("/api/v1/internal/me/bookings/").url_name == (
            "me-bookings-list"
        )

    def test_endpoint_is_exempt_from_x_app_type(self):
        """The bot sends no ``X-App-Type``. The ``/api/v1/internal/``
        prefix exemption must cover the new route — otherwise the
        middleware 403s every call."""
        response = _api().get(URL)
        assert response.status_code == 200
