"""Erasure matrix — the backend half.

The bot half lives in ``ai-bot-platform`` at
``apps/orchestrator/tests/test_memory_erasure_matrix.py``. That side proves what
the bot SENDS on «забудь всё» and «удалить аккаунт»; this side proves what the
backend DOES with it, using the real views and the real ORM.

``users.UserPersonalContext`` is the source of truth for declared preferences
(owner ruling 2026-08-24, ``Ayla/docs/OD_MEMORY.md`` §1). So a remnant here is
not a mirror drifting — it is the authoritative value still being authoritative.

Tests marked ``GAP`` assert a defect. Invert them when it is fixed; do not
delete them — the storage still needs a cell in the matrix.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from appointments.models import Appointment
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, UserPersonalContext
from users.personal_context_erasure import (
    ERASED,
    declared_fields,
    default_for,
    erase_personal_context,
)
from users.personal_context_inference import infer_for_active_users, infer_for_user

pytestmark = pytest.mark.django_db

User = get_user_model()

VALID_TOKEN = "test-internal-token"  # noqa: S105 — test constant

# The EXACT payload apps/orchestrator/memory/ayla_bridge.py:_CLEARABLE_FIELDS
# produces for «забудь всё» (sorted key order, source=explicit).
BOT_FORGET_ALL_UPDATES = [
    {"field": "diet_type", "value": "", "source": "explicit"},
    {"field": "preferred_districts", "value": [], "source": "explicit"},
    {"field": "preferred_time_slots", "value": [], "source": "explicit"},
]

FILLED = {
    "preferred_districts": ["Арбат"],
    "preferred_time_slots": ["evening"],
    "price_range_min": "1000.00",
    "price_range_max": "3500.00",
    "diet_type": "vegan",
    "skin_sensitivities": ["ретинол"],
    "prefers_flexible_cancellation": True,
    "workplace_district": "Тверская",
    "home_district": "Сокол",
    "favorite_masters": ["7f1d0f2e-0000-4000-8000-000000000001"],
    "min_rating_preference": 4.5,
    "busy_days": ["mon"],
}


@pytest.fixture(autouse=True)
def _set_token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def user():
    return User.objects.create_user(
        username="erasure_owner", password="x", role="client", phone="+79995559001",
    )


@pytest.fixture
def ctx(user):
    row = UserPersonalContext.objects.create(user=user, **FILLED)
    row.refresh_from_db()  # Decimal columns come back as Decimal, not str
    return row


def _internal(*, bearer: str | None = VALID_TOKEN) -> APIClient:
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    return c


def _url(user_id, suffix: str = "") -> str:
    return f"/api/v1/internal/users/{user_id}/personal-context/{suffix}"


APP_PC_URL = "/api/v1/auth/users/me/personal-context/"


def _app(user) -> APIClient:
    """The authenticated mobile surface — X-App-Type per AppTypeMiddleware."""
    api = APIClient(HTTP_X_APP_TYPE="client")
    api.force_authenticate(user=user)
    return api


def _assert_all_twelve_at_default(ctx: UserPersonalContext) -> None:
    """The matrix's definition of "erased": every declared field, from the
    model, back at the model's own default. Not a list written down here —
    a list written down here is the defect DRF-1367 describes.
    """
    remnants = {
        name: getattr(ctx, name)
        for name in declared_fields()
        if getattr(ctx, name) != default_for(name)
    }
    assert remnants == {}, f"survived erasure: {remnants}"
    assert len(declared_fields()) == 12


def _make_specialist(suffix: str) -> SpecialistProfile:
    spec_user = User.objects.create_user(
        username=f"erasure_spec_{suffix}", password="x",
        role="specialist", phone=f"+7999666{int(suffix) % 10000:04d}",
    )
    spec, _ = SpecialistProfile.objects.get_or_create(
        user=spec_user, defaults={"display_name": f"Мастер {suffix}", "bio": "t"},
    )
    return spec


def _book(client_user, spec, *, day_offset: int, hour: int = 10,
          status: str = Appointment.Status.COMPLETED):
    service = Service.objects.filter(specialist=spec).first()
    if service is None:
        cat, _ = ServiceCategory.objects.get_or_create(
            slug="erasure-cat", defaults={"name": "Erasure Cat"},
        )
        service = Service.objects.create(
            specialist=spec, category=cat, name="Svc", price=1000, duration_minutes=60,
        )
    base = (datetime.now(timezone.utc) - timedelta(days=day_offset)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return Appointment.objects.create(
        client=client_user, specialist=spec, service=service,
        start_datetime=base, end_datetime=base + timedelta(hours=1),
        price=1000, status=status,
    )


# ---------------------------------------------------------------------------
# «забудь всё» — what the bot's payload actually does to the row
# ---------------------------------------------------------------------------


class TestForgetAllPayload:
    def test_bot_forget_all_leaves_nine_of_twelve_fields_populated(self, user, ctx):
        """STILL A GAP after DRF-1367 — and it is now the bot's half.

        The backend has the verb (see ``TestForgetAllPayload`` below). This
        cell pins what the CURRENT bridge payload does, and it still leaves
        nine fields standing, because ``apps/orchestrator/memory/
        ayla_bridge.py:_CLEARABLE_FIELDS`` still names three fields instead of
        calling DELETE. Invert this cell in the bot repo's PR, not here.
        """
        resp = _internal().patch(
            _url(user.id), {"updates": BOT_FORGET_ALL_UPDATES}, format="json"
        )
        assert resp.status_code == 200

        ctx.refresh_from_db()
        assert ctx.diet_type == ""
        assert ctx.preferred_districts == []
        assert ctx.preferred_time_slots == []
        # Nine survivors, on the source of truth.
        assert str(ctx.price_range_min) == "1000.00"
        assert str(ctx.price_range_max) == "3500.00"
        assert ctx.favorite_masters == ["7f1d0f2e-0000-4000-8000-000000000001"]
        assert ctx.skin_sensitivities == ["ретинол"]
        assert ctx.prefers_flexible_cancellation is True
        assert ctx.workplace_district == "Тверская"
        assert ctx.home_district == "Сокол"
        assert ctx.min_rating_preference == 4.5
        assert ctx.busy_days == ["mon"]

    def test_the_survivors_are_served_straight_back_to_the_prompt(self, user, ctx):
        """The bot reads the prompt block off this exact GET."""
        _internal().patch(_url(user.id), {"updates": BOT_FORGET_ALL_UPDATES}, format="json")

        resp = _internal().get(_url(user.id))

        assert resp.status_code == 200
        context = resp.data["data"]["context"]
        assert context["favorite_masters"] == ["7f1d0f2e-0000-4000-8000-000000000001"]
        assert str(context["price_range_max"]) == "3500.00"
        assert resp.data["data"]["meta"]["filled_fields"] == 9

    def test_price_cannot_be_cleared_through_the_contract_at_all(self, user, ctx):
        """Still true after DRF-1367, and still the reason the verb exists.

        ``null`` is rejected by the serializer's JSONField and ``""`` blows up
        the Decimal column. There is no honest clear value for a price field on
        the PATCH contract — which is why erasure is not a PATCH. The bridge
        must call DELETE (see ``test_the_erase_verb_clears_the_price_...``),
        not learn a better encoding.
        """
        null_resp = _internal().patch(
            _url(user.id),
            {"updates": [{"field": "price_range_max", "value": None, "source": "explicit"}]},
            format="json",
        )
        assert null_resp.status_code == 400

        with pytest.raises(Exception):  # noqa: B017,PT011 — the column itself refuses
            _internal().patch(
                _url(user.id),
                {"updates": [{"field": "price_range_max", "value": "", "source": "explicit"}]},
                format="json",
            )

    def test_the_internal_contract_has_an_erase_verb(self, user, ctx):
        """INVERTED by DRF-1367. The bot used to have no way to say «wipe the
        row» — only DELETE on the authenticated ``/me/`` surface could, and
        the bot holds no user JWT on the pilot MAX path. Now one call does it.
        """
        resp = _internal().delete(_url(user.id))

        assert resp.status_code == 200
        assert resp.data["data"]["erased"] == ["personal_context"]
        ctx.refresh_from_db()
        _assert_all_twelve_at_default(ctx)

    def test_the_erase_verb_clears_the_price_the_contract_cannot(self, user, ctx):
        """The sharpest edge of P0-2, closed.

        ``price_range_max`` has no honest clear value on the PATCH contract
        (see the test above: ``null`` → 400, ``""`` → Decimal blows up). The
        verb never encodes a value at all — it asks the column for its own
        default — so the field the bridge could not touch is gone.
        """
        _internal().delete(_url(user.id))

        ctx.refresh_from_db()
        assert ctx.price_range_min is None
        assert ctx.price_range_max is None

    def test_the_erase_verb_empties_what_the_bot_reads_for_the_prompt(
        self, user, ctx,
    ):
        """The bot builds its memory block off this GET. Checked on the wire,
        not in the table — a value nobody reads is not the thing that leaked.
        """
        _internal().delete(_url(user.id))

        resp = _internal().get(_url(user.id))

        assert resp.status_code == 200
        assert resp.data["data"]["meta"]["filled_fields"] == 0
        context = resp.data["data"]["context"]
        assert not any(context.values()), f"still in the prompt source: {context}"

    def test_the_erase_verb_empties_the_backend_chat_prompt_block(self, user, ctx):
        """The second consumer of the same row. Checked through the very
        function that assembles the prompt, per DRF-1367.
        """
        from ai.personal_context_hint import format_personal_context_hint

        _internal().delete(_url(user.id))
        ctx.refresh_from_db()

        assert format_personal_context_hint(ctx) == ""

    def test_the_erase_verb_empties_the_bot_prompt_block_itself(self, user, ctx):
        """The strongest form of the DRF-1367 proof: not a table read, and not
        even the backend's own renderer — the exact function the bot assembles
        its memory block with, fed the exact dict the internal GET returns.

        ``ayla_ai_core`` is a declared dependency of this repo
        (requirements.txt), so the two halves of the pilot can be checked in
        one process. Before the verb, seven of the nine survivors rendered
        here: budget, favourite master, home district, work district, days
        avoided, minimum rating, flexible cancellation.
        """
        from ayla_ai_core import build_memory_block

        before = _internal().get(_url(user.id)).data["data"]["context"]
        assert build_memory_block(before) != ""

        _internal().delete(_url(user.id))

        after = _internal().get(_url(user.id)).data["data"]["context"]
        assert build_memory_block(after) == ""

    def test_the_erase_verb_is_idempotent(self, user, ctx):
        """Repeat honestly reports that there was nothing left to erase."""
        assert _internal().delete(_url(user.id)).data["data"]["erased"] == [
            "personal_context",
        ]
        second = _internal().delete(_url(user.id))
        assert second.status_code == 200
        assert second.data["data"]["erased"] == []

    def test_the_erase_verb_does_not_leak_into_the_single_field_reset(
        self, user, ctx,
    ):
        """The negative. Deleting ONE field from the app still deletes one.
        A «forget everything» that fires on «forget my diet» is a worse bug
        than the one being fixed.
        """
        resp = _app(user).delete(f"{APP_PC_URL}diet_type/")
        assert resp.status_code == 204

        ctx.refresh_from_db()
        assert ctx.diet_type == ""
        assert ctx.home_district == "Сокол"
        assert ctx.preferred_districts == ["Арбат"]
        assert str(ctx.price_range_max) == "3500.00"


# ---------------------------------------------------------------------------
# Resurrection — nightly inference refills what erasure emptied
# ---------------------------------------------------------------------------


class TestNightlyInferenceResurrection:
    def test_inference_does_not_refill_favorites_after_a_full_wipe(self, user):
        """INVERTED by DRF-1366 — the loudest hole. Erasure is terminal now.

        ``users.infer_user_patterns`` is registered in ``CELERY_BEAT_SCHEDULE``
        (settings/base.py, ``infer-user-personal-context``). It used to
        lazy-create the row and refill ``favorite_masters`` + ``busy_days``
        from booking history — precisely the class of fact the owner ruled
        must NOT travel (OD_MEMORY.md §3: «узнали о нём — не переходит»).

        The wipe now leaves a tombstone and inference refuses to write a
        field the subject decided. The booking history is untouched: this is
        a decision about what we may derive, not a rewrite of the record.
        """
        spec = _make_specialist("1")
        for i in range(3):
            _book(user, spec, day_offset=10 + i)

        # The user wipes everything through the authenticated surface.
        assert _app(user).delete(APP_PC_URL).status_code == 204

        infer_for_user(user)

        ctx = UserPersonalContext.objects.get(user=user)
        assert ctx.favorite_masters == []
        assert ctx.data_sources["favorite_masters"] == ERASED
        # The history that fed the inference is still there — we refused to
        # derive from it, we did not destroy it.
        assert Appointment.objects.filter(client=user).count() == 3

    def test_inference_does_not_refill_busy_days_after_a_full_wipe(self, user):
        spec = _make_specialist("2")
        # Eight Mondays — enough history, and only one weekday ever booked.
        monday = datetime.now(timezone.utc) - timedelta(days=60)
        monday -= timedelta(days=monday.weekday())
        for i in range(8):
            offset = (datetime.now(timezone.utc) - (monday - timedelta(days=7 * i))).days
            _book(user, spec, day_offset=offset)

        assert _app(user).delete(APP_PC_URL).status_code == 204
        infer_for_user(user)

        ctx = UserPersonalContext.objects.get(user=user)
        assert ctx.busy_days == []
        assert ctx.data_sources["busy_days"] == ERASED

    def test_inference_still_fills_a_user_who_never_erased(self, user):
        """The negative. A fix that silenced inference for everybody is a
        botch — it is useful to the people who never asked us to forget.
        """
        spec = _make_specialist("4")
        for i in range(3):
            _book(user, spec, day_offset=10 + i)

        infer_for_user(user)

        ctx = UserPersonalContext.objects.get(user=user)
        assert ctx.favorite_masters == [str(spec.id)]
        assert ctx.data_sources["favorite_masters"] == "inferred"

    def test_a_bare_row_deletion_is_not_an_erasure_the_verb_is(self, user):
        """The tombstone is what protects, not the absence of a row.

        No product path deletes the row for a live account any more — both go
        through ``erase_personal_context``. This cell pins WHY, so the next
        person reaching for ``.filter(user=...).delete()`` sees the answer:
        a deleted row is re-created by the nightly pass, a tombstone is not.
        """
        spec = _make_specialist("5")
        for i in range(3):
            _book(user, spec, day_offset=10 + i)

        UserPersonalContext.objects.filter(user=user).delete()
        infer_for_user(user)

        ctx = UserPersonalContext.objects.get(user=user)
        assert ctx.favorite_masters == [str(spec.id)]

    def test_the_erase_verb_protects_a_user_who_never_had_a_row(self, user):
        """«Забудь всё» from someone who never personalised still has to be
        terminal — otherwise tonight's pass invents favourite masters for a
        person who just asked us to forget them.
        """
        spec = _make_specialist("6")
        for i in range(3):
            _book(user, spec, day_offset=10 + i)
        assert not UserPersonalContext.objects.filter(user=user).exists()

        erase_personal_context(user, initiator="app")
        infer_for_user(user)

        ctx = UserPersonalContext.objects.get(user=user)
        assert ctx.favorite_masters == []

    def test_a_field_reset_from_the_app_survives_the_night(self, user, ctx):
        """One field wide. «Забудь моих любимых мастеров» is the same promise
        as «забудь всё», and inference used to undo it just as cheaply.
        """
        spec = _make_specialist("7")
        for i in range(3):
            _book(user, spec, day_offset=10 + i)

        resp = _app(user).delete(f"{APP_PC_URL}favorite_masters/")
        assert resp.status_code == 204

        infer_for_user(user)

        ctx.refresh_from_db()
        assert ctx.favorite_masters == []
        assert ctx.data_sources["favorite_masters"] == ERASED

    def test_the_nightly_pass_will_not_resurrect_a_deleted_account(
        self, user, ctx,
    ):
        """DRF-1366 third proof — a deleted account comes back in no form at
        all: not as a row, not as a field.

        The booking history survives the deletion (statutory retention), so
        without this guard the nightly pass has everything it needs to
        re-derive a person who asked to be gone. The ``User`` row is its own
        tombstone here — no context row is needed to protect it.
        """
        spec = _make_specialist("8")
        for i in range(3):
            _book(user, spec, day_offset=10 + i)

        user.deleted_at = datetime.now(timezone.utc)
        user.save(update_fields=["deleted_at"])
        UserPersonalContext.objects.filter(user=user).delete()

        counters = infer_for_active_users()
        infer_for_user(user)  # and the single-user entry point too

        assert not UserPersonalContext.objects.filter(user=user).exists()
        assert counters["processed_users"] == 0

    def test_the_engine_does_not_re_interview_an_erased_field(self, user):
        """«Забудь всё» must not be answered by «а какая у тебя диета?» on
        the next turn. Rule 5 treats an erasure like any other decision the
        subject made about the field.
        """
        from users import personalization_engine as engine

        user.onboarding_completed = True
        user.save(update_fields=["onboarding_completed"])
        erase_personal_context(user, initiator="app")

        verdict = engine.should_ask_question(user, "diet_type")

        assert verdict.allowed is False
        assert verdict.reason == "already_have_data"

    def test_a_bot_cleared_field_is_marked_explicit_and_is_protected(self, user, ctx):
        """Refutation — the clear is not silently overwritten.

        The bot's clear PATCH stamps ``data_sources[field]="explicit"``, and
        inference refuses to touch an explicit field. So diet/districts/slots
        stay cleared. The hole is only in the fields the bot never names.
        """
        _internal().patch(_url(user.id), {"updates": BOT_FORGET_ALL_UPDATES}, format="json")
        ctx.refresh_from_db()
        assert ctx.data_sources["diet_type"] == "explicit"

        spec = _make_specialist("3")
        for i in range(3):
            _book(user, spec, day_offset=10 + i)
        infer_for_user(user)

        ctx.refresh_from_db()
        assert ctx.diet_type == ""
        assert ctx.favorite_masters == [str(spec.id)]  # а этот — вернулся


# ---------------------------------------------------------------------------
# Account deletion — two different flows, two different completeness levels
# ---------------------------------------------------------------------------


class TestAccountDeletion:
    def test_mobile_account_delete_erases_the_whole_context(self, user, ctx):
        """INVERTED by DRF-1368. ``AuthService.delete_account`` (the DELETE
        ``/api/v1/auth/users/me/`` path used by the mobile app) anonymized the
        ``User`` row and blacklisted tokens but never touched
        ``UserPersonalContext`` — districts, budget, diet, sensitivities and
        favourite masters all survived «удалить аккаунт». It was the only
        deletion flow a person sees in the app, and it deleted no memory.

        Owner ruling ``Ayla/docs/OD_MEMORY.md`` §4: «удалить всё» = удалить
        память и профиль. The account is gone, so nothing is left behind at
        all — not even a tombstone.
        """
        from users.services import AuthService

        AuthService.delete_account(user=user, reason="test")

        user.refresh_from_db()
        # The anonymization that already worked is untouched.
        assert user.deleted_at is not None
        assert user.first_name == "Удалён"
        assert not UserPersonalContext.objects.filter(user=user).exists()

    def test_the_bot_cannot_read_a_deleted_users_context(self, user, ctx):
        """INVERTED by DRF-1368 — the second half, and the worse one.

        ``internal_personal_context_api._resolve_user`` had no ``deleted_at``
        filter while its neighbour ``personal_data_api._get_live_user`` did,
        so the internal GET the prompt block is built from answered 200 for a
        deleted account. The filter is half the fix, not decoration: without
        it a row that survived for any other reason is still readable.
        """
        from users.services import AuthService

        AuthService.delete_account(user=user, reason="test")

        resp = _internal().get(_url(user.id))

        assert resp.status_code == 404
        assert resp.data["error"]["code"] == "USER_NOT_FOUND"

    def test_every_internal_context_route_refuses_a_deleted_user(self, user, ctx):
        """One resolver, four doors. A filter on GET only would be a hole on
        PATCH — the write path can re-create what the read path refuses.
        """
        from users.services import AuthService

        AuthService.delete_account(user=user, reason="test")
        api = _internal()

        assert api.get(_url(user.id)).status_code == 404
        assert api.patch(
            _url(user.id), {"updates": BOT_FORGET_ALL_UPDATES}, format="json",
        ).status_code == 404
        assert api.delete(_url(user.id)).status_code == 404
        assert api.get(_url(user.id, "ask-eligibility/")).status_code == 404
        assert not UserPersonalContext.objects.filter(user=user).exists()

    def test_bot_initiated_delete_leaves_a_tombstone_not_a_dropped_row(
        self, user, ctx,
    ):
        """INVERTED by DRF-1366 — the cell moved, the guarantee got stronger.

        ``apps.identity.services.privacy.delete_personal_data`` calls this
        endpoint. It used to drop the row; a dropped row was re-created by the
        nightly inference the same night. Now it leaves a tombstone: all
        twelve declared fields at their model default and every one of them
        marked ``erased``, which inference refuses to write.
        """
        resp = _internal().delete(f"/api/v1/internal/users/{user.id}/personal-data/")

        assert resp.status_code == 200
        row = UserPersonalContext.objects.get(user=user)
        _assert_all_twelve_at_default(row)
        assert set(row.data_sources.values()) == {ERASED}

    def test_bot_initiated_delete_accepts_an_already_deleted_user(self, user, ctx):
        """INVERTED by DRF-1368 — the two flows compose now.

        If the mobile delete ran first, the bot's erasure step could no longer
        address the user (404 ``USER_NOT_FOUND``) and gave up on a row that,
        before this change, it had never emptied. The order in which a person
        happened to press the buttons decided whether they were erased.
        A delete addressed to someone already gone is not an error — it is an
        erasure with nothing left to erase, and it says so.
        """
        from users.services import AuthService

        AuthService.delete_account(user=user, reason="test")

        resp = _internal().delete(f"/api/v1/internal/users/{user.id}/personal-data/")

        assert resp.status_code == 200
        assert resp.json()["data"]["deleted"] == []
        assert not UserPersonalContext.objects.filter(user=user).exists()

    def test_both_deletion_orders_end_in_the_same_place(self, user, ctx):
        """The whole point of DRF-1368, in one cell. Deleted from the app or
        deleted from the bot — same end state, and neither route depends on
        the other having run first.
        """
        from users.services import AuthService

        # Route A — the app first, then the bot's cascade.
        AuthService.delete_account(user=user, reason="test")
        _internal().delete(f"/api/v1/internal/users/{user.id}/personal-data/")
        assert not UserPersonalContext.objects.filter(user=user).exists()

        # Route B — the bot's cascade first, then the app.
        other = User.objects.create_user(
            username="erasure_owner_b", password="x", role="client",
            phone="+79995559002",
        )
        UserPersonalContext.objects.create(user=other, **FILLED)
        _internal().delete(f"/api/v1/internal/users/{other.id}/personal-data/")
        AuthService.delete_account(user=other, reason="test")
        assert not UserPersonalContext.objects.filter(user=other).exists()


# ---------------------------------------------------------------------------
# The second prompt consumer — backend AI chat
# ---------------------------------------------------------------------------


class TestBackendPromptConsumer:
    def test_backend_chat_prompt_renders_sensitivities_the_bot_never_shows(self, ctx):
        """P0-4, confirmed. Two consumers of one row disagree about what is
        renderable: ``ayla_ai_core.build_memory_block`` has no branch for
        ``skin_sensitivities``, this one has.
        """
        from ai.personal_context_hint import format_personal_context_hint

        hint = format_personal_context_hint(ctx)

        assert "чувствительность / аллергии: ретинол" in hint
        assert "диета" in hint
        assert "бюджет" in hint

    def test_the_hint_survives_the_bot_forget_all(self, user, ctx):
        """STILL A GAP after DRF-1367 — the bot half again. The current
        bridge payload leaves the backend chat's prompt block populated too,
        through the same nine surviving fields. The verb empties it (see
        ``test_the_erase_verb_empties_the_backend_chat_prompt_block``); the
        bridge has to start calling the verb.
        """
        from ai.personal_context_hint import format_personal_context_hint

        _internal().patch(_url(user.id), {"updates": BOT_FORGET_ALL_UPDATES}, format="json")
        ctx.refresh_from_db()

        hint = format_personal_context_hint(ctx)

        assert hint != ""
        assert "чувствительность / аллергии: ретинол" in hint
        assert "бюджет" in hint
        assert "диета" not in hint


# ---------------------------------------------------------------------------
# The pin itself — what the bot's renderer makes of what this backend serves
# ---------------------------------------------------------------------------


class TestBotPromptRendererPin:
    """DRF-1441. This repo pins ``ayla-ai-core`` by SHA, and the pin sat two
    commits behind the SHA bot-platform runs, so the same client could be
    described differently on the two channels.

    Nothing in this repo's production path calls ``build_memory_block`` — the
    backend chat renders its own hint (``ai.personal_context_hint``). What
    this repo owns is the renderer's *input*: the internal personal-context
    endpoint is where the bot's memory block comes from. So the pin is
    provable from here, and only from here — feed the renderer the exact
    payload this backend serves, assert on what comes out.

    Both tests below FAIL on the previous pin (f773e7d) and pass on the
    current one (ee6425a). They are why this bump is not a no-op.
    """

    def test_a_full_profile_keeps_the_budget_the_backend_serves(self, user, ctx):
        """ee6425a. ``price_range_min`` / ``price_range_max`` had no declared
        priority in the renderer's field order — the row that *was* declared,
        ``price_range``, is not a key of this payload at all. Both keys
        therefore sorted to the tail and the budget line fell off the top-8
        cut on a full profile, while ``min_rating_preference`` — a search
        filter, not a memory about the person — made it in.

        ``FILLED`` is exactly such a profile: eleven renderable keys, ten
        lines, eight slots. On f773e7d this block has no budget in it.
        """
        from ayla_ai_core import build_memory_block

        payload = _internal().get(_url(user.id)).data["data"]
        block = build_memory_block(payload["context"])

        assert "Бюджет" in block, block
        # Not merely present — present at its declared rank, ahead of the
        # districts. Presence alone would also pass if the cut merely grew.
        assert block.index("Бюджет") < block.index("Ищет рядом с работой"), block

    def test_an_inferred_fact_is_not_offered_as_the_clients_own_words(
        self, user, ctx,
    ):
        """af620ba, and the half this repo already built: the internal GET
        ships ``data_sources`` beside ``context`` precisely so the renderer
        can tell a nightly inference from something the person typed. Until
        this pin the renderer had nowhere to put it — ``sources=`` did not
        exist, and the call below raised TypeError on f773e7d.

        ``busy_days`` is the real case: ``personal_context_inference`` stamps
        it from booking history, and it used to arrive looking user-stated.
        """
        from ayla_ai_core import (
            INFERRED_MARK,
            MEMORY_INFERRED_HEADER,
            build_memory_block,
        )

        ctx.data_sources = {"busy_days": "inferred"}
        ctx.save(update_fields=["data_sources"])

        payload = _internal().get(_url(user.id)).data["data"]
        assert payload["data_sources"]["busy_days"] == "inferred"

        block = build_memory_block(
            payload["context"], sources=payload["data_sources"],
        )

        assert MEMORY_INFERRED_HEADER in block, block
        assert f"{INFERRED_MARK} Избегает: понедельник" in block, block
        # The line moved into the derived section — it no longer sits in the
        # plain list, where it would read as the client's own words.
        assert "\n- Избегает: понедельник" not in block, block
