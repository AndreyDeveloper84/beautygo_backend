"""«Забудь всё» стирает историю уведомлений, ИИ-чат приложения и outbox питания (DRF-2277).

Решение владельца §72 п.3: по «забудь всё» стирать историю уведомлений
(``notifications.Notification``; настройки остаются), ИИ-чат приложения
(``ai.Conversation``) и ``nutrition.NutritionOutboxEvent`` — и в удалении
аккаунта (D3) тоже; избранные мастера (``users.FavoriteSpecialist``) остаются.

Строка уведомления — не только история: два бита (``notifications.tasks``)
шлют напоминание за час и уход после визита, только если строки
``(user, template_id, data.appointment_id)`` НЕТ. «Забудь всё» оставляет
записи, и стирание в открытом окне бита дало бы человеку то же напоминание
второй раз. Поэтому (решение главного окна, вариант 2) маркеры ОТКРЫТЫХ окон
двух битов остаются обезличенными — ``title``/``body``/``deep_link``/``error``
пустые, ``data`` = ``{appointment_id}``, — остальное стирается. Окна — из
констант самих битов.

* n1 — история уведомлений стёрта, текста не осталось;
* n2 — напоминание в открытом окне: маркер обезличен, бит не шлёт второй раз;
* n3 — уход после визита в открытом окне: то же;
* n4 — маркер закрытого окна стирается, как вся история;
* n5 — ИИ-чат приложения (включая мягко удалённые) стёрт с сообщениями;
* n6 — outbox питания связанного прокси стёрт путём бота C5.2;
* n7 — журнал и ответ называют стёртое словами, C5.3 считает остаток;
* d1 — D3 стирает outbox по имени прокси ДО переименования, остатка нет.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.db import transaction
from django.utils import timezone

from ai.models import Conversation, Message
from appointments.models import Appointment
from notifications.models import Notification
from notifications.tasks import (
    AFTERCARE_TEMPLATE_ID,
    REMINDER_LEAD_MINUTES,
    REMINDER_TEMPLATE_ID,
    dispatch_appointment_reminders,
    dispatch_post_visit_aftercare,
)
from nutrition.models import NutritionOutboxEvent
from services.models import Service, ServiceCategory
from users.forget_all_catalog import erase_remembered_catalog, remembered_residue, remembered_scope
from users.models import SpecialistProfile, User
from users.tests.test_forget_all_diary_2214 import _bot_url
from users.tests.test_memory_erasure_matrix import (  # noqa: F401 — фикстуры по имени
    _internal,
    _set_token,
    user,
)

pytestmark = pytest.mark.django_db

SECRET_TITLE = "Напоминание: маникюр у Анны"  # текст истории — не должен пережить стирание
PROXY = "bot:max:2277001"


@pytest.fixture
def service(db):
    sp_user = User.objects.create_user(
        username="spec2277", password="x", role="specialist", phone="+79990002277",
    )
    profile = sp_user.specialist_profile
    profile.display_name = "Мастер 2277"
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.save(update_fields=["display_name", "status"])
    cat = ServiceCategory.objects.create(name="cat2277", slug="cat2277")
    return Service.objects.create(
        specialist=profile, category=cat, name="Маникюр", price=1000, duration_minutes=60,
        aftercare_text="Не мочить руки два часа.",
    )


def _appointment(client, service, *, start_in_min: int, status=Appointment.Status.CONFIRMED):
    start = timezone.now() + dt.timedelta(minutes=start_in_min)
    return Appointment.objects.create(
        client=client, specialist=service.specialist, service=service,
        start_datetime=start, end_datetime=start + dt.timedelta(minutes=60),
        status=status, price=service.price,
    )


def _notification(u, template_id: str, appointment=None) -> Notification:
    data = {"appointment_id": str(appointment.id), "service_name": "Маникюр"} if appointment else {"x": 1}
    return Notification.objects.create(
        user=u, template_id=template_id, channel=Notification.Channel.PUSH,
        title=SECRET_TITLE, body="Завтра в 10:00 у Анны", data=data,
        deep_link="ayla://booking/1", error="",
    )


class TestN1HistoryIsErased:
    def test_history_rows_are_gone(self, user) -> None:  # noqa: F811
        _notification(user, "promo_digest")
        _notification(user, "appointment_confirmed")
        assert Notification.objects.filter(user=user).count() == 2

        counts = erase_remembered_catalog(user, initiator="test")

        assert counts["notifications.Notification"] == 2
        assert not Notification.objects.filter(user=user).exists()


class TestN2ReminderMarkerInAnOpenWindow:
    def test_the_marker_is_anonymised_and_the_reminder_is_not_resent(self, user, service) -> None:  # noqa: F811
        appt = _appointment(user, service, start_in_min=REMINDER_LEAD_MINUTES)
        assert dispatch_appointment_reminders()["queued"] == 1  # наличие: бит отправил
        assert Notification.objects.filter(user=user, template_id=REMINDER_TEMPLATE_ID).count() == 1

        erase_remembered_catalog(user, initiator="test")

        (marker,) = Notification.objects.filter(user=user)
        assert marker.template_id == REMINDER_TEMPLATE_ID
        assert (marker.title, marker.body, marker.deep_link, marker.error) == ("", "", "", "")
        assert marker.data == {"appointment_id": str(appt.id)}
        again = dispatch_appointment_reminders()
        assert again == {"queued": 0, "skipped": 1}


class TestN3AftercareMarkerInAnOpenWindow:
    def test_the_marker_is_anonymised_and_the_aftercare_is_not_resent(self, user, service) -> None:  # noqa: F811
        appt = _appointment(user, service, start_in_min=-(60 + 2 * 60 + 10), status=Appointment.Status.COMPLETED)
        first = dispatch_post_visit_aftercare()
        assert first["queued"] == 1  # наличие: бит отправил

        erase_remembered_catalog(user, initiator="test")

        (marker,) = Notification.objects.filter(user=user)
        assert marker.template_id == AFTERCARE_TEMPLATE_ID
        assert (marker.title, marker.body) == ("", "")
        assert marker.data == {"appointment_id": str(appt.id)}
        assert dispatch_post_visit_aftercare()["queued"] == 0


class TestN4ClosedWindowMarkerIsErased:
    def test_a_reminder_for_a_past_visit_is_history(self, user, service) -> None:  # noqa: F811
        past = _appointment(user, service, start_in_min=-180)
        _notification(user, REMINDER_TEMPLATE_ID, past)
        assert Notification.objects.filter(user=user).count() == 1

        erase_remembered_catalog(user, initiator="test")

        assert not Notification.objects.filter(user=user).exists()


class TestN5AppAiChat:
    def test_conversations_and_messages_are_gone(self, user) -> None:  # noqa: F811
        gone = Conversation.objects.create(user=user)
        gone.mark_deleted()
        live = Conversation.objects.create(user=user)
        Message.objects.create(conversation=live, role="user", content="у меня секущиеся кончики")
        assert Conversation.all_objects.filter(user=user).count() == 2

        counts = erase_remembered_catalog(user, initiator="test")

        assert counts["ai.Conversation"] == 2
        assert not Conversation.all_objects.filter(user=user).exists()
        assert not Message.objects.filter(conversation_id=live.pk).exists()


class TestN6NutritionOutboxViaTheBot:
    def test_the_linked_proxys_outbox_is_gone(self, user) -> None:  # noqa: F811
        User.objects.create(username=PROXY, role="client", is_proxy=True, linked_user=user)
        NutritionOutboxEvent.objects.create(
            topic=NutritionOutboxEvent.Topic.PROFILE_UPDATED, external_user_id=PROXY,
            payload={"goal": "lose", "norms": {"kcal": 1800}},
        )
        stranger = NutritionOutboxEvent.objects.create(
            topic=NutritionOutboxEvent.Topic.WATER_LOGGED, external_user_id="bot:max:2277999",
            payload={"ml": 250},
        )
        assert NutritionOutboxEvent.objects.filter(external_user_id=PROXY).count() == 1

        assert _internal().delete(_bot_url(user)).status_code == 200

        assert not NutritionOutboxEvent.objects.filter(external_user_id=PROXY).exists()
        assert NutritionOutboxEvent.objects.filter(pk=stranger.pk).exists()


class TestN7WordsAndResidue:
    def test_scope_names_the_three_and_residue_is_zero(self, user, service) -> None:  # noqa: F811
        proxy = User.objects.create(username=PROXY, role="client", is_proxy=True, linked_user=user)
        _notification(user, "promo_digest")
        Conversation.objects.create(user=user)
        NutritionOutboxEvent.objects.create(
            topic=NutritionOutboxEvent.Topic.WATER_LOGGED, external_user_id=PROXY, payload={},
        )
        _appointment(user, service, start_in_min=REMINDER_LEAD_MINUTES)
        dispatch_appointment_reminders()
        assert remembered_residue(user) > 0 and remembered_residue(proxy) > 0

        words = remembered_scope(erase_remembered_catalog(user, initiator="test"))
        words += remembered_scope(erase_remembered_catalog(proxy, initiator="test"))

        assert {"notification_history", "app_ai_chat", "nutrition_outbox"} <= set(words)
        # Обезличенный маркер открытого окна — не остаток.
        assert Notification.objects.filter(user=user).count() == 1
        assert remembered_residue(user) == 0 and remembered_residue(proxy) == 0


class TestD1DeletionErasesTheOutbox:
    def test_d3_erases_by_the_proxy_name_before_renaming(self) -> None:
        from users.deletion_executor import _erase_catalog

        proxy = User.objects.create(username=PROXY, role="client", is_proxy=True)
        NutritionOutboxEvent.objects.create(
            topic=NutritionOutboxEvent.Topic.PROFILE_UPDATED, external_user_id=PROXY, payload={"goal": "lose"},
        )
        assert NutritionOutboxEvent.objects.filter(external_user_id=PROXY).exists()

        with transaction.atomic():
            steps = _erase_catalog(proxy)

        assert steps["deleted"]["nutrition.NutritionOutboxEvent"] == 1
        assert not NutritionOutboxEvent.objects.filter(external_user_id=PROXY).exists()
