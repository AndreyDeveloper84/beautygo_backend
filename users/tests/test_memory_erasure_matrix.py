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
from users.personal_context_inference import infer_for_user

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
        """GAP, P0 — the whole of P0-2, on real backend code.

        The internal contract has no DELETE and no «clear everything» verb; the
        bot can only name fields, and it names three.
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
        """The bridge refuses to guess an encoding — this is why.

        ``null`` is rejected by the serializer's JSONField and ``""`` blows up
        the Decimal column. There is no honest clear value for a price field on
        this contract.
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

    def test_there_is_no_internal_erase_verb(self, user, ctx):
        """The bot has no way to say «wipe the row» — only DELETE on the
        authenticated ``/me/`` surface can, and the bot does not hold a user
        JWT for the pilot MAX path.
        """
        assert _internal().delete(_url(user.id)).status_code in (403, 404, 405)
        assert _internal().post(_url(user.id, "wipe/")).status_code in (403, 404, 405)


# ---------------------------------------------------------------------------
# Resurrection — nightly inference refills what erasure emptied
# ---------------------------------------------------------------------------


class TestNightlyInferenceResurrection:
    def test_inference_refills_favorites_after_a_full_wipe(self, user):
        """GAP, P0 — the loudest hole. Erasure is not a terminal state.

        ``users.infer_user_patterns`` is registered in ``CELERY_BEAT_SCHEDULE``
        (settings/base.py, ``infer-user-personal-context``). It lazy-creates the
        row and refills ``favorite_masters`` + ``busy_days`` from booking
        history — which is precisely the class of fact the owner ruled must NOT
        travel (OD_MEMORY.md §3: «узнали о нём — не переходит»). The next GET
        the bot makes puts the master back in the prompt.
        """
        spec = _make_specialist("1")
        for i in range(3):
            _book(user, spec, day_offset=10 + i)

        # The user wipes everything through the authenticated surface.
        UserPersonalContext.objects.filter(user=user).delete()
        assert not UserPersonalContext.objects.filter(user=user).exists()

        infer_for_user(user)

        ctx = UserPersonalContext.objects.get(user=user)
        assert ctx.favorite_masters == [str(spec.id)]
        assert ctx.data_sources["favorite_masters"] == "inferred"

    def test_inference_refills_busy_days_after_a_full_wipe(self, user):
        spec = _make_specialist("2")
        # Eight Mondays — enough history, and only one weekday ever booked.
        monday = datetime.now(timezone.utc) - timedelta(days=60)
        monday -= timedelta(days=monday.weekday())
        for i in range(8):
            offset = (datetime.now(timezone.utc) - (monday - timedelta(days=7 * i))).days
            _book(user, spec, day_offset=offset)

        UserPersonalContext.objects.filter(user=user).delete()
        infer_for_user(user)

        ctx = UserPersonalContext.objects.get(user=user)
        assert ctx.busy_days  # непустой — дни «вернулись»
        assert "mon" not in ctx.busy_days
        assert ctx.data_sources["busy_days"] == "inferred"

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
    def test_mobile_account_delete_leaves_the_whole_context_intact(self, user, ctx):
        """GAP, P0 — ``AuthService.delete_account`` (the DELETE
        ``/api/v1/auth/users/me/`` path used by the mobile app) anonymizes the
        ``User`` row and blacklists tokens, but never touches
        ``UserPersonalContext``. Districts, budget, diet, sensitivities and
        favourite masters all survive «удалить аккаунт».
        """
        from users.services import AuthService

        AuthService.delete_account(user=user, reason="test")

        user.refresh_from_db()
        assert user.deleted_at is not None
        assert user.first_name == "Удалён"
        ctx.refresh_from_db()
        assert ctx.diet_type == "vegan"
        assert ctx.skin_sensitivities == ["ретинол"]
        assert ctx.favorite_masters == ["7f1d0f2e-0000-4000-8000-000000000001"]

    def test_the_bot_can_still_read_a_deleted_users_context(self, user, ctx):
        """GAP, P0 — and it is reachable. ``internal_personal_context_api``'s
        ``_resolve_user`` has no ``deleted_at`` filter (unlike
        ``personal_data_api._get_live_user``), so the internal GET the prompt
        block is built from answers 200 for a deleted account.
        """
        from users.services import AuthService

        AuthService.delete_account(user=user, reason="test")

        resp = _internal().get(_url(user.id))

        assert resp.status_code == 200
        assert resp.data["data"]["context"]["diet_type"] == "vegan"

    def test_bot_initiated_delete_does_wipe_the_row(self, user, ctx):
        """Refutation — the OTHER account-delete flow is complete for this
        storage. ``apps.identity.services.privacy.delete_personal_data`` calls
        this endpoint, and it drops the row entirely.
        """
        resp = _internal().delete(f"/api/v1/internal/users/{user.id}/personal-data/")

        assert resp.status_code == 200
        assert not UserPersonalContext.objects.filter(user=user).exists()

    def test_bot_initiated_delete_refuses_a_deleted_user(self, user, ctx):
        """GAP — and the two flows do not compose. If the mobile delete ran
        first, the bot's erasure step can no longer address the user (404
        ``USER_NOT_FOUND`` on ``_get_live_user``) and the row it would have
        dropped stays forever.
        """
        from users.services import AuthService

        AuthService.delete_account(user=user, reason="test")

        resp = _internal().delete(f"/api/v1/internal/users/{user.id}/personal-data/")

        assert resp.status_code == 404
        assert UserPersonalContext.objects.filter(user=user).exists()


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
        """GAP, P0 — «забудь всё» in MAX leaves the backend chat's prompt
        block populated too, through the same nine surviving fields.
        """
        from ai.personal_context_hint import format_personal_context_hint

        _internal().patch(_url(user.id), {"updates": BOT_FORGET_ALL_UPDATES}, format="json")
        ctx.refresh_from_db()

        hint = format_personal_context_hint(ctx)

        assert hint != ""
        assert "чувствительность / аллергии: ретинол" in hint
        assert "бюджет" in hint
        assert "диета" not in hint
