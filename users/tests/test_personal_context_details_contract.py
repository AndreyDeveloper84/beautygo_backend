"""Golden contract: ``details`` for a list-validation failure on
``PATCH /internal/users/{id}/personal-context/`` (DRF-1715, DRF-1714 §8).

Recorded on both DRF versions before the normaliser existed
(``Ayla/docs/DRF_318_GATE_2026-09-12.md`` — DRF 3.16.1 renders a positional
list with ``{}`` for passing items, DRF 3.18.0 a dict keyed by the item index
as a string with passing items omitted). The public contract
(``docs/PERSONAL_CONTEXT_INTERNAL_API_CONTRACT.md``) promises an object, so
the dict form is canonical **on every DRF version**.

Positive control (checked by hand, see PR): on DRF 3.16 these cases go red
the moment ``_normalize_validation_details`` is bypassed; on DRF 3.18 they
stay green with or without it — so the test pins the contract, not the
normaliser.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from users.models import User

from .conftest import name_subject

pytestmark = pytest.mark.django_db

TOKEN = "details-contract-token"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = TOKEN


@pytest.fixture
def user() -> User:
    return User.objects.create_user(
        username="pc_details_contract", password="x", role="client", phone="+79995550777",
    )


def _patch(user: User, updates: list) -> tuple[int, dict]:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
    # DRF-1617: субъект в URL обязан совпадать с X-External-User-ID.
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = name_subject(user)
    resp = c.patch(
        f"/api/v1/internal/users/{user.id}/personal-context/",
        {"updates": updates},
        format="json",
    )
    return resp.status_code, resp.json()


def test_every_item_failing_is_an_object_keyed_by_index(user):
    """Case A of the gate: two items, both wrong."""
    status, body = _patch(user, [{"field": "nope", "value": 1}, {"field": "diet_type"}])
    assert status == 400
    assert body["error"]["code"] == "VALIDATION_ERROR"
    details = body["error"]["details"]
    assert isinstance(details, dict), details
    assert set(details) == {"0", "1"}
    assert "field" in details["0"]
    assert "value" in details["1"]


def test_a_passing_item_is_omitted_not_an_empty_object(user):
    """Case B of the gate: first item valid, second wrong.

    DRF 3.16 put ``{}`` in position 0; the canonical form has no key "0"
    at all — absence of errors is absence, not an empty object.
    """
    status, body = _patch(
        user, [{"field": "diet_type", "value": "vegan"}, {"field": "nope", "value": 1}],
    )
    assert status == 400
    details = body["error"]["details"]
    assert isinstance(details, dict), details
    assert set(details) == {"1"}, details
    assert "field" in details["1"]


def test_a_valid_list_is_unaffected(user):
    """Case C: the success path does not pass through the normaliser."""
    status, body = _patch(user, [{"field": "diet_type", "value": "vegan"}])
    assert status == 200
    assert body["data"]["context"]["diet_type"] == "vegan"


def test_a_single_serializer_error_keeps_its_field_keyed_shape(user):
    """Regression guard for the shape the normaliser must NOT touch: a dict
    from a single (non-list) serializer stays field-keyed."""
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
    # DRF-1617: субъект в URL обязан совпадать с X-External-User-ID.
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = name_subject(user)
    resp = c.patch(
        f"/api/v1/internal/users/{user.id}/personal-context/",
        {"updates": "not-a-list"},
        format="json",
    )
    assert resp.status_code == 400
    details = resp.json()["error"].get("details")
    # Either no details (view-level refusal) or a field-keyed dict — never
    # an index-keyed dict, because there is no list here.
    if details is not None:
        assert isinstance(details, dict)
        assert not any(k.isdigit() for k in details), details
