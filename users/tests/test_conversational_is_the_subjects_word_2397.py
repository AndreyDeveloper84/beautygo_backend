"""Сказанное в разговоре — слова человека, а не наша догадка (DRF-2397).

Ночная инференция молчит о поле, которое решил сам субъект: напечатал
(`explicit`) или стёр (`erased`) — `_SUBJECT_OWNED`. Но `busy_days` человек
может назвать и в разговоре с ботом, отвечая на прямой вопрос «есть дни,
которые лучше избегать?»; такой ответ приходит по внутреннему PATCH и
ложится с пометкой из списка `_SOURCE_CHOICES`. Пока `conversational` не
считается решением субъекта, ночной проход перезаписывает названный человеком
ответ догадкой из истории броней — молча и каждую ночь.

`behavioral` и `transactional` здесь НЕ трогаем: это тоже наши выводы, из
поведения и из сделок. Спор идёт только про сказанное словами.

* g1 — `busy_days` с пометкой `conversational` не перезаписывается;
* g2 — `explicit` по-прежнему не перезаписывается (прежнее поведение);
* g3 — `behavioral` остаётся нашим выводом: перезапись разрешена.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model

from appointments.models import Appointment
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, UserPersonalContext
from users.personal_context_inference import BUSY_DAYS_MIN_HISTORY, infer_for_user

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def person(db):
    return User.objects.create_user(
        username="conv2397", password="x", role="client", phone="+79992234001",
    )


@pytest.fixture
def weekday_only_history(person):
    """История только по будням — суббота и воскресенье выводятся «занятыми»."""
    spec_user = User.objects.create_user(
        username="conv2397_spec", password="x", role="specialist",
        phone="+79992234002",
    )
    spec, _ = SpecialistProfile.objects.get_or_create(
        user=spec_user, defaults={"display_name": "Master", "bio": "t"},
    )
    cat, _ = ServiceCategory.objects.get_or_create(
        slug="conv2397-cat", defaults={"name": "Cat"},
    )
    service = Service.objects.create(
        specialist=spec, category=cat, name="Svc", price=1000, duration_minutes=60,
    )
    anchor = datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc)  # понедельник
    for i in range(BUSY_DAYS_MIN_HISTORY + 2):
        day = i % 5  # только Mon..Fri
        ts = anchor + timedelta(days=(i // 5) * 7 + day)
        Appointment.objects.create(
            client=person, specialist=spec, service=service,
            start_datetime=ts, end_datetime=ts + timedelta(hours=1),
            price=1000, status=Appointment.Status.COMPLETED,
        )
    return spec


def _ctx(person, source: str) -> UserPersonalContext:
    return UserPersonalContext.objects.create(
        user=person,
        busy_days=["mon"],  # человек назвал один день
        data_sources={"busy_days": source},
    )


class TestG1SaidInConversationStays:
    def test_a_conversational_answer_is_not_overwritten(
        self, person, weekday_only_history
    ) -> None:
        ctx = _ctx(person, "conversational")

        outcome = infer_for_user(person)

        assert "busy_days" in outcome.skipped_explicit
        ctx.refresh_from_db()
        assert ctx.busy_days == ["mon"]
        assert ctx.data_sources["busy_days"] == "conversational"


class TestG2TypedStaysAsBefore:
    def test_an_explicit_answer_is_not_overwritten(
        self, person, weekday_only_history
    ) -> None:
        ctx = _ctx(person, "explicit")

        outcome = infer_for_user(person)

        assert "busy_days" in outcome.skipped_explicit
        ctx.refresh_from_db()
        assert ctx.busy_days == ["mon"]


class TestG3OurOwnDerivationIsOursToRedo:
    def test_a_behavioral_value_may_be_overwritten(
        self, person, weekday_only_history
    ) -> None:
        """`behavioral` — вывод из поведения, не слова человека. Ночной проход
        вправе пересчитать свой же вывод, иначе первая догадка становится
        вечной."""
        ctx = _ctx(person, "behavioral")

        outcome = infer_for_user(person)

        assert "busy_days" not in outcome.skipped_explicit
        ctx.refresh_from_db()
        assert set(ctx.busy_days) >= {"sat", "sun"}
        assert ctx.data_sources["busy_days"] == "inferred"
