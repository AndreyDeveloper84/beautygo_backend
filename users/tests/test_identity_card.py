"""§12 (owner 11.09), DRF-1693 — the catalog half of the read-only identity card.

Both sides of every rule: what the operator sees AND what they do not. The
harm is a value that leaks, and a presence-only test passes on a card that
prints everything.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone as dt_tz

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from appointments.models import Appointment
from goals.models import ClientGoal, GoalAnketaRun
from nutrition.models import FoodLog
from services.models import Service, ServiceCategory
from users.identity_card import (
    BLOCKED_BY_IDENTITY,
    HELD_IN_PRODUCT,
    build_card,
    mask_name,
    mask_phone,
    render_for_operator,
)
from users.models import SpecialistProfile, User, UserPersonalContext
from users.services import bind_external_identity, resolve_external_user

pytestmark = pytest.mark.django_db

CID = "999000777"
PROXY = f"bot:max:{CID}"
NAME = "Анастасия"
PHONE = "+79991234567"


def _appointment(client: User, status: str) -> Appointment:
    specialist_user, _ = User.objects.get_or_create(
        username="card-specialist", defaults={"role": "specialist", "phone": "+79990000009"}
    )
    profile = SpecialistProfile.objects.get(user=specialist_user)
    category, _ = ServiceCategory.objects.get_or_create(name="Card cat", slug="card-cat")
    service, _ = Service.objects.get_or_create(
        specialist=profile,
        category=category,
        name="Card service",
        defaults={"price": "1500.00", "duration_minutes": 60, "is_active": True},
    )
    start = timezone.now() + timezone.timedelta(hours=2)
    return Appointment.objects.create(
        client=client,
        specialist=profile,
        service=service,
        start_datetime=start,
        end_datetime=start + timezone.timedelta(minutes=60),
        price=service.price,
        status=status,
        snapshot_service_name=service.name,
        snapshot_price=service.price,
        snapshot_duration_minutes=60,
    )


def _proxy_with_history() -> User:
    proxy = resolve_external_user(PROXY)
    UserPersonalContext.objects.create(user=proxy, diet_type="vegan")
    ClientGoal.objects.create(client=proxy, goal_key="relax", source_channel="bot")
    GoalAnketaRun.objects.create(client=proxy)
    FoodLog.objects.create(
        user=proxy, dish_name="Борщ", portion_multiplier=1.0, calories=250, protein_g=8,
        fat_g=6, carbs_g=35, meal_type="lunch", logged_at=datetime.now(dt_tz.utc),
    )
    _appointment(proxy, Appointment.Status.COMPLETED)
    _appointment(proxy, Appointment.Status.CANCELLED)
    return proxy


def _bound_to_real(proxy: User) -> User:
    real = User.objects.create_user(
        username="real-card-person", password="x", role="client", phone=PHONE, first_name=NAME,
    )
    bind_external_identity(PROXY, real.id)
    return real


class TestMasks:
    def test_name(self):
        assert mask_name(NAME) == "А… (9)"
        assert mask_name("") == "нет"

    def test_phone_never_a_digit(self):
        assert mask_phone(PHONE) == "есть, длина 12"
        assert PHONE[-4:] not in mask_phone(PHONE)
        assert mask_phone(None) == "нет"


class TestTheCard:
    def test_the_proxy_row_carries_the_catalog_context(self):
        _proxy_with_history()
        card = build_card("max", CID)
        assert card.found
        assert [r.kind for r in card.rows] == ["proxy"]
        row = card.rows[0]
        assert row.username_masked == PROXY, "the proxy name IS the id the operator typed"
        assert row.personal_context is True
        assert (row.goals, row.anketa_runs, row.food_logs) == (1, 1, 1)
        assert row.appointments_total == 2
        assert row.appointments_held == 1, "the cancelled one does not hold a reset (§16.3)"
        assert card.status == BLOCKED_BY_IDENTITY

    def test_the_binding_is_followed_one_hop(self):
        proxy = _proxy_with_history()
        real = _bound_to_real(proxy)
        card = build_card("max", CID)
        assert [r.kind for r in card.rows] == ["proxy", "real"]
        assert card.rows[1].user_id == real.id
        assert card.rows[1].first_name == NAME, "the model carries the value; the renderer masks it"

    def test_unknown_is_a_named_absence(self):
        card = build_card("max", "404")
        assert not card.found
        assert "в каталоге нет" in render_for_operator(card)

    def test_building_the_card_writes_nothing(self):
        proxy = _proxy_with_history()
        _bound_to_real(proxy)
        with CaptureQueriesContext(connection) as ctx:
            build_card("max", CID)
        verbs = {q["sql"].lstrip().split(" ", 1)[0].upper() for q in ctx.captured_queries}
        assert verbs, "the card must have READ something"
        assert verbs <= {"SELECT", "SAVEPOINT", "RELEASE"}, sorted(verbs)

    def test_held_statuses_are_the_owners_reading_of_16_3(self):
        assert set(HELD_IN_PRODUCT) == {
            Appointment.Status.COMPLETED,
            Appointment.Status.CONFIRMED,
            Appointment.Status.PENDING,
            Appointment.Status.AWAITING_PAYMENT,
        }
        assert Appointment.Status.CANCELLED not in HELD_IN_PRODUCT
        assert Appointment.Status.NO_SHOW not in HELD_IN_PRODUCT


class TestTheOperatorSeesMasks:
    def test_counts_yes_values_no(self):
        proxy = _proxy_with_history()
        _bound_to_real(proxy)
        text = render_for_operator(build_card("max", CID))
        # Present.
        assert PROXY in text
        assert "[real]" in text
        assert "А… (9)" in text
        assert "есть, длина 12" in text
        assert "целей: 1" in text and "записей еды: 1" in text
        assert "всего 2, из них держат сброс по §16.3: 1" in text
        assert BLOCKED_BY_IDENTITY in text
        # Absent.
        assert NAME not in text
        assert PHONE not in text
        assert PHONE[-4:] not in text
        assert "real-card-person" not in text, "a real account's username is a personal value"

    def test_the_command_prints_it(self):
        proxy = _proxy_with_history()
        _bound_to_real(proxy)
        out = io.StringIO()
        call_command("identity_card", "--account", f"max:{CID}", stdout=out)
        text = out.getvalue()
        assert PROXY in text
        assert NAME not in text
        assert PHONE not in text

    def test_the_command_refuses_a_bare_id(self):
        with pytest.raises(CommandError):
            call_command("identity_card", "--account", CID, stdout=io.StringIO())
