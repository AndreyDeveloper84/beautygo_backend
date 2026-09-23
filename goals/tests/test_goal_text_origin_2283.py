"""Пометка происхождения слов цели — `user_stated` (DRF-2283, CD §73).

Контракт Goal этапа C (G-INV-15, AC-15): свободный текст цели хранится
дословно **с пометкой источника** `user_stated`. До этого листа у цели был
только `source_channel` — это КАНАЛ (бот или приложение), а не источник
слов: он отвечает «откуда пришло», а не «кто это сказал».

## Почему умолчание — «не знаем», а не «человек»

Если умолчанием сделать `user_stated`, все прежние строки молча объявят
себя словами человека, и пометка станет бесполезной ровно в тот момент,
когда родится. Пусто — «происхождение не установлено»; бэкфилл — отдельная
команда с сухим прогоном, и запускает её владелец.

## Что метится

Только там, где текст пришёл свободным вводом человека: прямой выбор со
свободным текстом и шаг цели анкеты. Чип (`goal_key` без текста) пометки не
получает — там слов человека нет.
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from goals.models import ClientGoal
from services.models import GoalOption, GoalOptionCategory, ServiceCategory
from users.models import User

VALID_TOKEN = "test-ayla-internal-token-goals"
SELECT_URL = "/api/v1/internal/me/goals/select/"
WORDS = "хочу −5 кг к лету"
EXTERNAL_ID = "bot:origin-2283"


@pytest.fixture
def token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def client_user(db, token):
    return User.objects.create_user(
        username=EXTERNAL_ID, password="x", role="client",
        phone="+79990002283", is_proxy=True,
    )


@pytest.fixture
def goal_option(db):
    category = ServiceCategory.objects.create(name="Массаж тела", slug="massage-2283")
    option = GoalOption.objects.create(key="relax", label="Расслабиться", sort_order=10)
    GoalOptionCategory.objects.create(goal_option=option, category=category, sort_order=0)
    return option


def _api(*, external_user_id: str = EXTERNAL_ID) -> APIClient:
    c = APIClient()
    c.credentials(
        HTTP_AUTHORIZATION=f"Bearer {VALID_TOKEN}", HTTP_X_EXTERNAL_USER_ID=external_user_id
    )
    return c


@pytest.mark.django_db
class TestTheMarkIsSetWhereThePersonSpoke:
    def test_free_text_from_the_bot_is_marked_user_stated(self, client_user):
        resp = _api().post(
            SELECT_URL, {"goal_text": WORDS, "source_channel": "bot"}, format="json"
        )
        assert resp.status_code == 200, resp.content[:300]

        goal = ClientGoal.objects.get(client=client_user)
        assert goal.goal_text == WORDS
        assert goal.text_origin == ClientGoal.TextOrigin.USER_STATED

    def test_free_text_from_the_miniapp_is_marked_too(self, client_user):
        resp = _api().post(
            SELECT_URL, {"goal_text": WORDS, "source_channel": "miniapp"}, format="json"
        )
        assert resp.status_code == 200, resp.content[:300]

        assert (
            ClientGoal.objects.get(client=client_user).text_origin
            == ClientGoal.TextOrigin.USER_STATED
        )


@pytest.mark.django_db
class TestTheMarkIsNotHandedOut:
    def test_a_chip_without_words_gets_no_mark(self, client_user, goal_option):
        """Слов человека нет — и пометке взяться неоткуда."""
        resp = _api().post(
            SELECT_URL, {"goal_key": goal_option.key, "source_channel": "bot"}, format="json"
        )
        assert resp.status_code == 200, resp.content[:300]

        goal = ClientGoal.objects.get(client=client_user)
        assert goal.goal_text is None
        assert goal.text_origin == ""

    def test_the_mark_cannot_be_claimed_from_outside(self, client_user):
        """Пометка — свойство сущности, а не поле сообщения.

        Отправитель, пытающийся выдать её себе сам, ничего не меняет:
        ручка такого поля не знает и знать не должна.
        """
        resp = _api().post(
            SELECT_URL,
            {"goal_key": "relax", "text_origin": "user_stated", "source_channel": "bot"},
            format="json",
        )

        # Ручка либо не принимает лишнее поле, либо игнорирует его — но
        # пометки без слов человека не появляется ни в одном случае.
        if resp.status_code == 200:
            assert ClientGoal.objects.get(client=client_user).text_origin == ""
        else:
            assert not ClientGoal.objects.filter(client=client_user).exists()

    def test_an_empty_mark_does_not_become_user_stated_by_itself(self, client_user):
        """Прежняя строка остаётся «не установлено», пока её не пометят явно."""
        goal = ClientGoal.objects.create(
            client=client_user, goal_text=WORDS, source_channel="bot"
        )

        goal.refresh_from_db()

        assert goal.text_origin == ""


@pytest.mark.django_db
class TestTheBackfillCommand:
    """Прежние строки метит человек с правом решать, а не слияние PR."""

    def _seed(self, client_user):
        from services.models import GoalOption

        GoalOption.objects.create(key="tone", label="Подтянуть", sort_order=20)
        words = ClientGoal.objects.create(
            client=client_user, goal_text=WORDS, source_channel="bot"
        )
        ClientGoal.objects.filter(pk=words.pk).update(state=ClientGoal.State.SUPERSEDED)
        chip = ClientGoal.objects.create(
            client=client_user, goal_key="tone", source_channel="miniapp"
        )
        return words, chip

    def test_the_default_is_a_dry_run_that_prints_no_words(self, client_user, capsys):
        from django.core.management import call_command

        words, chip = self._seed(client_user)

        call_command("mark_goal_text_origin")

        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "to_mark_total=1" in out
        assert WORDS not in out  # дословный текст не печатается
        words.refresh_from_db()
        chip.refresh_from_db()
        assert words.text_origin == ""
        assert chip.text_origin == ""

    def test_apply_marks_only_the_words_and_touches_nothing_else(self, client_user, capsys):
        from django.core.management import call_command

        words, chip = self._seed(client_user)

        call_command("mark_goal_text_origin", "--apply")

        assert "marked=1" in capsys.readouterr().out
        words.refresh_from_db()
        chip.refresh_from_db()
        assert words.text_origin == ClientGoal.TextOrigin.USER_STATED
        # Чип без слов пометки не получает, и значения целей не тронуты.
        assert chip.text_origin == ""
        assert words.goal_text == WORDS
        assert words.source_channel == "bot"
        assert words.state == ClientGoal.State.SUPERSEDED

    def test_a_second_run_marks_nothing_more(self, client_user, capsys):
        from django.core.management import call_command

        self._seed(client_user)
        call_command("mark_goal_text_origin", "--apply")
        capsys.readouterr()

        call_command("mark_goal_text_origin", "--apply")

        out = capsys.readouterr().out
        assert "to_mark_total=0" in out
        assert "already_marked=1" in out
