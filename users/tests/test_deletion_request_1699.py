"""Заявка на удаление аккаунта — приём и чтение (§7 свода, DRF-1699, D1).

§7: «устойчивый DeletionRequest создаётся до показа успеха»; человек
видит номер, точную крайнюю дату (≤ 30 дней) и статус. Здесь проверяется
ровно это — запись и её форма. Стирания тут НЕТ, и тест это утверждает
отдельно: ручка, которая заодно стирает, вернула бы §7 к прежнему
«нажал → стёрто» без следа.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import IntegrityError
from django.utils import timezone
from rest_framework.test import APIClient

from users.deletion_requests import ensure_deletion_request
from users.models import DeletionRequest, User, UserPersonalContext

pytestmark = pytest.mark.django_db


@pytest.fixture
def bearer_token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = "test-bearer-1699"
    return "test-bearer-1699"


@pytest.fixture
def api(bearer_token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer_token}")
    return client


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="dr-user", password="pass", role="client", phone="+79992221699",
    )


def _url(user_id, request_id=None) -> str:
    base = f"/api/v1/internal/users/{user_id}/deletion-requests/"
    return base if request_id is None else f"{base}{request_id}/"


class TestTheRequestIsARecordBeforeAnything:
    def test_post_creates_a_request_with_id_deadline_and_status(self, api, user):
        before = timezone.now()

        r = api.post(_url(user.pk), {"initiator": "bot"}, format="json")

        assert r.status_code == 201, r.content
        data = r.json()["data"]
        row = DeletionRequest.objects.get(pk=data["request_id"])
        assert row.user_id == user.pk
        assert data["status"] == "DELETION_REQUESTED"
        assert data["is_open"] is True
        assert data["completed_at"] is None
        # Крайняя дата — ровно 30 дней от приёма, посчитана один раз.
        assert row.deadline_at - row.requested_at == timedelta(days=30)
        assert row.requested_at >= before
        assert data["deadline_at"] == row.deadline_at.isoformat()

    def test_nothing_is_erased_by_the_request(self, api, user):
        """Заявка — запись, не действие. Контекст на месте, строка жива."""
        UserPersonalContext.objects.create(user=user, diet_type="vegan")

        r = api.post(_url(user.pk), {"initiator": "bot"}, format="json")

        assert r.status_code == 201, r.content
        user.refresh_from_db()
        assert user.deleted_at is None
        assert user.is_active is True
        assert UserPersonalContext.objects.get(user=user).diet_type == "vegan"

    def test_repeat_returns_the_same_request_as_200(self, api, user):
        first = api.post(_url(user.pk), {"initiator": "bot"}, format="json").json()["data"]

        second = api.post(_url(user.pk), {"initiator": "bot"}, format="json")

        assert second.status_code == 200, second.content
        assert second.json()["data"]["request_id"] == first["request_id"]
        assert second.json()["data"]["deadline_at"] == first["deadline_at"]
        assert DeletionRequest.objects.filter(user=user).count() == 1

    def test_a_failed_request_is_still_the_open_one(self, api, user):
        """Сбой исполнителя не заводит вторую заявку и не меняет номер."""
        failed = DeletionRequest.objects.create(
            user=user,
            status=DeletionRequest.Status.FAILED,
            initiator="bot",
            deadline_at=timezone.now() + timedelta(days=30),
        )

        r = api.post(_url(user.pk), {"initiator": "bot"}, format="json")

        assert r.status_code == 200, r.content
        assert r.json()["data"]["request_id"] == str(failed.pk)
        assert DeletionRequest.objects.filter(user=user).count() == 1

    def test_after_completion_a_new_request_can_be_opened(self, api, user):
        """Положительная стража идемпотентности: закрывает только COMPLETED."""
        DeletionRequest.objects.create(
            user=user,
            status=DeletionRequest.Status.COMPLETED,
            initiator="bot",
            deadline_at=timezone.now(),
            completed_at=timezone.now(),
        )

        r = api.post(_url(user.pk), {"initiator": "bot"}, format="json")

        assert r.status_code == 201, r.content
        assert DeletionRequest.objects.filter(user=user).count() == 2

    def test_a_soft_deleted_user_still_gets_a_request(self, api, user):
        """DRF-1368: удалил из приложения раньше — номер и срок всё равно его."""
        user.deleted_at = timezone.now()
        user.is_active = False
        user.save(update_fields=["deleted_at", "is_active"])

        r = api.post(_url(user.pk), {"initiator": "bot"}, format="json")

        assert r.status_code == 201, r.content

    def test_a_lost_race_reads_the_winner(self, user):
        winner = DeletionRequest.objects.create(
            user=user, initiator="bot", deadline_at=timezone.now() + timedelta(days=30),
        )
        from users import deletion_requests

        calls = {"n": 0}
        real_open = deletion_requests.open_request_for

        def _open(u):
            calls["n"] += 1
            # Первое чтение — «пусто» (гонка), второе — настоящее.
            return None if calls["n"] == 1 else real_open(u)

        with patch.object(deletion_requests, "open_request_for", _open), patch.object(
            deletion_requests.DeletionRequest.objects,
            "create",
            side_effect=IntegrityError("duplicate key"),
        ):
            ensured = ensure_deletion_request(user, initiator="bot")

        assert ensured.created is False
        assert ensured.request.pk == winner.pk


class TestTheGuardAndTheShape:
    def test_no_bearer_is_403_and_creates_nothing(self, user, settings):
        settings.AYLA_INTERNAL_API_TOKEN = "test-bearer-1699"
        r = APIClient().post(_url(user.pk), {"initiator": "bot"}, format="json")
        assert r.status_code in (401, 403), r.content
        assert DeletionRequest.objects.filter(user=user).count() == 0

    def test_unknown_user_is_404(self, api):
        r = api.post(_url(uuid4()), {"initiator": "bot"}, format="json")
        assert r.status_code == 404, r.content

    def test_unknown_initiator_is_400(self, api, user):
        r = api.post(_url(user.pk), {"initiator": "телефон"}, format="json")
        assert r.status_code == 400, r.content
        assert DeletionRequest.objects.filter(user=user).count() == 0

    def test_get_returns_the_same_shape_as_post(self, api, user):
        posted = api.post(_url(user.pk), {"initiator": "bot"}, format="json").json()["data"]

        got = api.get(_url(user.pk, posted["request_id"]))

        assert got.status_code == 200, got.content
        assert got.json()["data"] == posted

    def test_get_of_someone_elses_request_is_404(self, api, user):
        other = User.objects.create_user(
            username="dr-other", password="pass", role="client", phone="+79992221698",
        )
        theirs = DeletionRequest.objects.create(
            user=other, initiator="bot", deadline_at=timezone.now() + timedelta(days=30),
        )

        r = api.get(_url(user.pk, theirs.pk))

        assert r.status_code == 404, r.content
        # Положительная стража: свой номер тем же GET читается.
        mine = api.post(_url(user.pk), {"initiator": "bot"}, format="json").json()["data"]
        assert api.get(_url(user.pk, mine["request_id"])).status_code == 200


class TestOneOpenRequestPerUserIsEnforcedByTheDatabase:
    def test_second_open_row_is_refused_by_the_index(self, user):
        """Сторож — индекс, а не вежливость сервиса: прямое создание тоже отказ."""
        DeletionRequest.objects.create(
            user=user, initiator="bot", deadline_at=timezone.now() + timedelta(days=30),
        )
        from django.db import transaction

        with pytest.raises(IntegrityError), transaction.atomic():
            DeletionRequest.objects.create(
                user=user,
                initiator="app",
                status=DeletionRequest.Status.PROCESSING,
                deadline_at=timezone.now() + timedelta(days=30),
            )
        # Положительная стража: закрытая рядом с открытой — можно.
        DeletionRequest.objects.create(
            user=user,
            initiator="app",
            status=DeletionRequest.Status.COMPLETED,
            deadline_at=timezone.now(),
            completed_at=timezone.now(),
        )
        assert DeletionRequest.objects.filter(user=user).count() == 2
