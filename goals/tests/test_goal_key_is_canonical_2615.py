"""DRF-2615: ``POST /goals/select`` takes a canonical goal key, not any slug.

``goal_key`` was a bare ``SlugField(max_length=64)``: any slug became
``ClientGoal.goal_key``. The questionnaire's goal step had the same disease
and was cured (``_answer_anketa``: «прежний вид проверки пропускал ЛЮБОЙ
слаг»); the direct entry kept it. Cured here by the SAME check —
``anketa.answerable_option_keys(anketa.goal_step())`` — not a second kind.

Why it matters: a junk key is indistinguishable from a real uncurated goal
(``goal.unresolved``), diluting the signal the owner curates the goal registry
by; and it takes the one active slot (``clientgoal_one_active_per_client``),
pushing the person's real goal into ``superseded``.

The three that must differ — the third is what keeps the fix honest:

* a key that is a ``GoalOption`` → the goal is created;
* a key that is not → refused by name, no row, the previous goal stays active;
* free words only (``goal_text``) → still accepted (OD-1: free wording is
  equal; only passing an arbitrary slug off as a canonical key is refused).
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from goals.models import ClientGoal
from services.models import GoalOption
from users.models import User

State = ClientGoal.State

TOKEN = "test-ayla-internal-token-2615"
EXTERNAL_USER_ID = "bot:goals-2615"
SELECT_URL = "/api/v1/internal/me/goals/select/"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = TOKEN


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client", phone="+79995026150", is_proxy=True
    )


@pytest.fixture
def relax(db):
    return GoalOption.objects.create(key="relax", label="Расслабиться", sort_order=10)


@pytest.fixture
def previous_goal(customer):
    """The person's real, active goal — the one a junk key would push out."""
    return ClientGoal.objects.create(
        client=customer, goal_text="Хочу высыпаться", source_channel="bot", state=State.ACTIVE
    )


def _select(body: dict):
    client = APIClient()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
    client.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
    return client.post(SELECT_URL, {**body, "source_channel": "bot"}, format="json")


class TestTheKeyIsCanonical:
    def test_a_curated_key_creates_the_goal(self, customer, relax, previous_goal):
        resp = _select({"goal_key": "relax"})
        assert resp.status_code in (200, 201), resp.content
        active = ClientGoal.objects.get(client=customer, state=State.ACTIVE)
        assert active.goal_key == "relax"
        previous_goal.refresh_from_db()
        assert previous_goal.state == State.SUPERSEDED  # the ordinary «chose a new one»

    def test_an_unknown_key_is_refused_and_nothing_moves(self, customer, relax, previous_goal):
        before = ClientGoal.objects.count()
        resp = _select({"goal_key": "relax_x"})
        assert resp.status_code == 400, resp.content
        assert "goal_key" in resp.content.decode()
        assert ClientGoal.objects.count() == before  # no row
        previous_goal.refresh_from_db()
        assert previous_goal.state == State.ACTIVE  # the real goal kept its place

    def test_free_words_alone_still_pass(self, customer, relax, previous_goal):
        resp = _select({"goal_text": "Хочу меньше уставать"})
        assert resp.status_code in (200, 201), resp.content
        active = ClientGoal.objects.get(client=customer, state=State.ACTIVE)
        assert (active.goal_key, active.goal_text) == (None, "Хочу меньше уставать")

    def test_a_retired_option_is_not_canonical(self, customer, relax, previous_goal):
        """``goal_step`` reads ACTIVE options; a retired key is refused like
        the questionnaire would refuse it — one source, two entries."""
        GoalOption.objects.filter(pk=relax.pk).update(is_active=False)
        assert _select({"goal_key": "relax"}).status_code == 400
        previous_goal.refresh_from_db()
        assert previous_goal.state == State.ACTIVE
