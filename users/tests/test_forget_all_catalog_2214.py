"""«Забудь всё» в каталоге стирает то, что запомнено о человеке, — не только UPC (DRF-2214, PR-1).

Каталожное «забудь всё» (``users/personal_context_erasure.py::
erase_personal_context``) трогало ровно одну строку — ``UserPersonalContext``.
Цель с дословным ``goal_text``, анкета цели со свободными ответами, план и всё
wellness вокруг него, профиль питания с весом, ростом и ``health_flags``
оставались лежать. Удаление аккаунта (D3, ``users/deletion_executor.py::
_erase_catalog``) всё это стирает; «забудь всё» отставало именно от него.

# Договор с человеком — текст команды

``FORGET_ALL_PROMPT`` бота говорит: «я забуду всё, что запомнила о тебе из
наших разговоров, и анкету предпочтений», а остаются ТОЛЬКО «бронирования и
оплаты (это по закону) и настройки уведомлений с датой рождения». Цель, анкета
цели, план, профиль питания в список остающегося не входят — по обещанию они
уходят.

# Чего здесь нет — дневника

``FoodLog``, ``WaterEntry``, ``SavedMeal``, ``FoodScan`` с фото — вопрос к
владельцу (текст обещает стереть и их, но человек вносил их сам и может не
ждать, что пропадут вместе с «разговорами»). Отдельным коммитом по ответу.
Узлов о дневнике здесь нет в обе стороны: пришпилить «дневник остаётся» значило
бы зашить ответ, которого ещё нет.

# Главное различие с D3

«Забудь всё» — не удаление аккаунта. Записи на приём и оплаты ОСТАЮТСЯ, и
остаются нетронутыми: D3 обезличивает запись (``client`` → надгробие, ``notes``
→ ""), а здесь такого быть не должно — человек остаётся клиентом салона, и
запись по-прежнему его. Аккаунт живёт.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.utils import timezone

from analytics.models import AnalyticsEvent
from appointments.models import Appointment
from goals.models import ClientGoal, GoalAnketaAnswer, GoalAnketaRun
from nutrition.models import NutritionProfile
from payments.models import Payment
from services.models import Service, ServiceCategory
from users.tests.test_memory_erasure_matrix import (  # noqa: F401 — фикстуры по имени
    APP_PC_URL,
    _app,
    _internal,
    _set_token,
    _url,
    user,
)
from wellness.models import (
    DesiredOutcome,
    PersonalPlan,
    PlanAction,
    PlanOutcomeLink,
    ProgressObservation,
)

User = get_user_model()
pytestmark = pytest.mark.django_db

#: Дословные слова человека — именно их «забудь всё» обязано снять.
GOAL_TEXT = "хочу к свадьбе сестры в мае влезть в синее платье"
ANSWER_TEXT = "после родов тяжело даётся, спина болит"


def _seed_remembered(u) -> None:
    """Всё, что каталог запомнил о человеке, по строке в каждой модели.

    Каждая строка объявлена явно, чтобы «ноль после» нельзя было получить
    пустым стендом — тот же приём, что у фикстуры D3.
    """
    now = timezone.now()
    goal = ClientGoal.objects.create(
        client=u, goal_key="weight", source_channel="bot", goal_text=GOAL_TEXT
    )
    run = GoalAnketaRun.objects.create(client=u, goal=goal)
    GoalAnketaAnswer.objects.create(run=run, step_key="context", answer_text=ANSWER_TEXT)
    outcome = DesiredOutcome.objects.create(
        user=u,
        target="body_weight",
        statement_text="хочу сбросить вес",
        direction=DesiredOutcome.Direction.REDUCE,
        desired_state_numeric=Decimal("55"),
    )
    plan = PersonalPlan.objects.create(user=u)
    PlanOutcomeLink.objects.create(plan=plan, outcome=outcome)
    PlanAction.objects.create(plan=plan, action_type="log_food", cadence="per_week", target_count=3)
    first = ProgressObservation.objects.create(
        user=u,
        observation_type=ProgressObservation.ObservationType.WEIGHT,
        value_numeric=Decimal("60"),
        observed_at=now,
    )
    ProgressObservation.objects.create(
        user=u,
        observation_type=ProgressObservation.ObservationType.WEIGHT,
        value_numeric=Decimal("59"),
        observed_at=now,
        superseded_by=first,
    )
    NutritionProfile.objects.create(
        user=u,
        weight_kg=60,
        height_cm=165,
        age=30,
        gender="female",
        activity_coefficient=1.6,
        goal="lose",
        daily_kcal=1800,
    )


def _remembered_counts(u) -> dict[str, int]:
    return {
        "ClientGoal": ClientGoal.objects.filter(client=u).count(),
        "GoalAnketaRun": GoalAnketaRun.objects.filter(client=u).count(),
        "GoalAnketaAnswer": GoalAnketaAnswer.objects.filter(run__client=u).count(),
        "DesiredOutcome": DesiredOutcome.objects.filter(user=u).count(),
        "PersonalPlan": PersonalPlan.objects.filter(user=u).count(),
        "PlanOutcomeLink": PlanOutcomeLink.objects.filter(plan__user=u).count(),
        "PlanAction": PlanAction.objects.filter(plan__user=u).count(),
        "ProgressObservation": ProgressObservation.objects.filter(user=u).count(),
        "NutritionProfile": NutritionProfile.objects.filter(user=u).count(),
    }


@pytest.fixture
def remembered(user):  # noqa: F811 — фикстура по имени
    _seed_remembered(user)
    before = _remembered_counts(user)
    # Наличие — первым: у каждой модели есть что стирать.
    assert all(n >= 1 for n in before.values()), before
    return user


@pytest.fixture
def booked(remembered):
    """Запись на приём с заметкой и оплата по ней — то, что обязано ОСТАТЬСЯ."""
    master = User.objects.create_user(
        username="fa2214_master",
        password="x",  # pragma: allowlist secret
        role="specialist",
        phone="+79990622140",
    )
    sp = master.specialist_profile
    sp.status = "active"
    sp.save()
    cat = ServiceCategory.objects.create(name="Ногти 2214")
    service = Service.objects.create(
        specialist=sp, category=cat, name="Маникюр", price=Decimal("1500"), duration_minutes=60
    )
    now = timezone.now()
    appt = Appointment.objects.create(
        client=remembered,
        specialist=sp,
        service=service,
        start_datetime=now - timedelta(hours=2),
        end_datetime=now - timedelta(hours=1),
        status="completed",
        price=service.price,
        notes="аллергия на лак",
    )
    Payment.objects.create(
        appointment=appt,
        amount=Decimal("1500.00"),
        status=Payment.Status.PAID,
        specialist_income=Decimal("1380.00"),
        platform_fee=Decimal("120.00"),
        provider="yookassa",
        provider_payment_id="yk-2214",
    )
    return remembered, appt


def _forget_via_bot(u) -> None:
    resp = _internal().delete(_url(u.id))
    assert resp.status_code == 200, resp.content


def _forget_via_app(u) -> None:
    resp = _app(u).delete(APP_PC_URL)
    assert resp.status_code == 204, resp.content


BOTH_PATHS = pytest.mark.parametrize(
    "forget", [_forget_via_bot, _forget_via_app], ids=["бот", "приложение"]
)


class TestWhatWasRememberedIsForgotten:
    @BOTH_PATHS
    def test_every_remembered_store_is_empty(self, remembered, forget) -> None:
        before = _remembered_counts(remembered)
        assert all(n >= 1 for n in before.values()), before

        forget(remembered)

        after = _remembered_counts(remembered)
        assert after == {k: 0 for k in after}, after

    @BOTH_PATHS
    def test_the_verbatim_words_are_gone(self, remembered, forget) -> None:
        """Сердце листа: дословная цель и свободный ответ анкеты не переживают."""
        assert ClientGoal.objects.filter(goal_text=GOAL_TEXT).exists()
        assert GoalAnketaAnswer.objects.filter(answer_text=ANSWER_TEXT).exists()

        forget(remembered)

        assert not ClientGoal.objects.filter(goal_text=GOAL_TEXT).exists()
        assert not GoalAnketaAnswer.objects.filter(answer_text=ANSWER_TEXT).exists()
        # Наличие рядом: строка пользователя на месте — стёрто содержимое, а
        # не человек.
        assert User.objects.filter(pk=remembered.pk).exists()


class TestTheAccountAndTheLawfulRecordsStay:
    @BOTH_PATHS
    def test_bookings_and_payments_stay_untouched(self, booked, forget) -> None:
        """Обязательный узел: «забудь всё» — не D3. Запись и оплата — нетронуты.

        D3 обезличивает запись (`client` → надгробие, `notes` → ""); здесь
        этого быть не должно: человек остаётся клиентом, запись — его.
        """
        u, appt = booked

        forget(u)

        appt.refresh_from_db()
        assert appt.client_id == u.pk
        assert appt.notes == "аллергия на лак"
        assert Payment.objects.filter(appointment=appt, provider_payment_id="yk-2214").exists()

    @BOTH_PATHS
    def test_the_account_lives(self, remembered, forget) -> None:
        forget(remembered)

        remembered.refresh_from_db()
        assert remembered.is_active is True
        assert remembered.phone == "+79995559001"


class TestOnlyThePersonsRowsGo:
    def test_a_neighbour_is_untouched(self, remembered) -> None:
        """Положительная пара к «пусто после»: стирается только этот человек."""
        neighbour = User.objects.create_user(
            username="fa2214_neighbour", password="x", role="client", phone="+79995559002"
        )
        _seed_remembered(neighbour)
        before = _remembered_counts(neighbour)

        _forget_via_bot(remembered)

        assert _remembered_counts(neighbour) == before
        assert all(n >= 1 for n in before.values())


class TestIdempotent:
    @BOTH_PATHS
    def test_a_second_forget_is_harmless(self, remembered, forget) -> None:
        before = _remembered_counts(remembered)
        assert all(n >= 1 for n in before.values()), before

        forget(remembered)
        forget(remembered)  # не падает: успех и пусто

        after = _remembered_counts(remembered)
        assert after == {k: 0 for k in after}, after


#: Путь → модуль, в котором вид зовёт ``erase_personal_context``.
PATHS_WITH_MODULE = pytest.mark.parametrize(
    "forget, view_module",
    [
        # DRF-2305 — оба пути зовут общий глагол ``users.forget_all_subject``.
        (_forget_via_bot, "users.forget_all_subject"),
        (_forget_via_app, "users.forget_all_subject"),
    ],
    ids=["бот", "приложение"],
)


class TestAllOrNothing:
    """Одна транзакция: стёрто всё или ничего — и ответ не врёт об исходе."""

    @PATHS_WITH_MODULE
    def test_a_failed_profile_erasure_keeps_everything(self, remembered, forget, view_module) -> None:
        """Профиль не стёрся — не стёрто и остальное; человек не остаётся с половиной."""
        before = _remembered_counts(remembered)
        assert all(n >= 1 for n in before.values()), before

        with mock.patch(f"{view_module}.erase_personal_context", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                forget(remembered)

        assert _remembered_counts(remembered) == before

    @BOTH_PATHS
    def test_a_failed_analytics_insert_does_not_undo_the_erasure(self, remembered, forget) -> None:
        """Сбой записи аналитики не откатывает стирание молча под ответом «успех».

        ``_emit`` глотает любую ошибку. Без своей точки сохранения ошибка БД
        внутри него помечает ВНЕШНЮЮ транзакцию к откату — и стирание
        откатывалось бы, а ответ всё равно говорил бы «стёрто».
        """
        before = _remembered_counts(remembered)
        assert all(n >= 1 for n in before.values()), before

        with mock.patch.object(AnalyticsEvent, "_do_insert", side_effect=DatabaseError("boom")):
            forget(remembered)  # ответ — успех

        after = _remembered_counts(remembered)
        assert after == {k: 0 for k in after}, after
