"""C5.3 / AMD-020 — readback стирания личного профиля (DRF-1984).

``GET /api/v1/internal/users/{id}/personal-data/erasure-status/``

Бот повторяет стирание в Ayla (ai-bot-platform DRF-1950) и пишет человеку
«удалено» только после этого чтения. Ответ DELETE не годится: ``deleted: []``
одинаков для «уже стёрто» и «ничего не было». Экспорт не годится: отдаёт все
персданные, а удалённому субъекту отвечает 403. Здесь — состояние строки по
каждой личности субъекта и вердикт, без значений.

Правило вердикта — то, что делает C5.2 (``erase_personal_context``):

* живой аккаунт — стёрт, только если строка стала tombstone (стирание создаёт
  его и там, где строки не было, — значит «строки нет» у живого = не стирали);
* удалённый (``deleted_at``) — стёрт, только если строки нет вовсе;
* живой связанный прокси — tombstone или строки нет (стирание пропускает
  прокси без строки, не создавая tombstone).
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from .conftest import name_subject
from users.models import Profile, User, UserPersonalContext


STATUS_URL = "/api/v1/internal/users/{user_id}/personal-data/erasure-status/"
DELETE_URL = "/api/v1/internal/users/{user_id}/personal-data/"

pytestmark = pytest.mark.django_db


@pytest.fixture
def bearer_token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = "test-bearer"  # noqa: S105  # pragma: allowlist secret
    return "test-bearer"


@pytest.fixture
def user(db):
    u = User.objects.create_user(
        username="es-user", password="pass",  # pragma: allowlist secret
        role="client", phone="+79992230001", email="es@example.com",
    )
    Profile.objects.filter(user=u).update(full_name="Эля Статусова", city="Пенза")
    return u


@pytest.fixture
def api(bearer_token, user):
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {bearer_token}",
        HTTP_X_EXTERNAL_USER_ID=name_subject(user),
    )
    return client


def _with_values(user):
    return UserPersonalContext.objects.create(
        user=user,
        diet_type="vegan",
        preferred_districts=["Центр"],
        price_range_min=Decimal("500.00"),
        workplace_district="Заводской",
        data_sources={"diet_type": "explicit"},
    )


def _status(api, user):
    resp = api.get(STATUS_URL.format(user_id=user.pk))
    assert resp.status_code == 200, resp.content
    return resp.json()["data"]


def _account(data):
    rows = [item for item in data["identities"] if item["kind"] == "account"]
    assert len(rows) == 1, data
    return rows[0]


class TestVerdict:
    def test_a_live_account_that_holds_values_is_not_erased(self, api, user):
        _with_values(user)
        data = _status(api, user)
        assert _account(data) == {"kind": "account", "context_row": "holds_values", "erased": False}
        assert data["erased"] is False

    def test_after_the_delete_the_account_is_a_tombstone_and_erased(self, api, user):
        _with_values(user)
        assert api.delete(DELETE_URL.format(user_id=user.pk)).status_code == 200
        data = _status(api, user)
        assert _account(data) == {"kind": "account", "context_row": "tombstone", "erased": True}
        assert data["erased"] is True

    def test_a_live_account_with_no_row_was_never_erased(self, api, user):
        """«Строки нет» у живого — не «стёрто»: стирание оставило бы tombstone."""
        data = _status(api, user)
        assert _account(data) == {"kind": "account", "context_row": "absent", "erased": False}
        assert data["erased"] is False

    def test_an_empty_row_that_was_never_erased_is_not_a_tombstone(self, api, user):
        """Пустую строку лениво создаёт чтение personal-context; ночной вывод
        волен её заполнить — назвать её стёртой значило бы подтвердить
        стирание, которого не было."""
        UserPersonalContext.objects.create(user=user)
        data = _status(api, user)
        assert _account(data) == {"kind": "account", "context_row": "not_erased", "erased": False}
        assert data["erased"] is False

    def test_a_soft_deleted_account_after_the_delete_has_no_row_and_is_erased(self, api, user):
        _with_values(user)
        user.deleted_at = timezone.now()
        user.save(update_fields=["deleted_at"])
        assert api.delete(DELETE_URL.format(user_id=user.pk)).status_code == 200
        data = _status(api, user)
        assert _account(data) == {"kind": "account", "context_row": "absent", "erased": True}
        assert data["erased"] is True

    def test_a_soft_deleted_account_whose_row_survived_is_not_erased(self, api, user):
        _with_values(user)
        user.deleted_at = timezone.now()
        user.save(update_fields=["deleted_at"])
        data = _status(api, user)
        assert _account(data) == {"kind": "account", "context_row": "holds_values", "erased": False}
        assert data["erased"] is False

    def test_a_linked_proxy_with_values_keeps_the_subject_unerased_until_the_delete(self, api, user):
        proxy = User.objects.create(
            username="bot:max:es-prebind", role="client", is_proxy=True, is_guest=False,
            linked_user=user,
        )
        _with_values(proxy)
        UserPersonalContext.objects.create(user=user, data_sources={}).delete()
        before = _status(api, user)
        kinds = sorted(item["kind"] for item in before["identities"])
        # Аккаунт + прокси бота (name_subject) + прокси с данными до привязки.
        assert kinds == ["account", "linked_identity", "linked_identity"], before
        assert {"kind": "linked_identity", "context_row": "holds_values", "erased": False} in before["identities"]
        assert before["erased"] is False

        assert api.delete(DELETE_URL.format(user_id=user.pk)).status_code == 200
        after = _status(api, user)
        assert {"kind": "linked_identity", "context_row": "tombstone", "erased": True} in after["identities"]
        assert after["erased"] is True


class TestWhatTheReadDoesNot:
    def test_the_body_carries_no_personal_values_and_no_external_ids(self, api, user):
        _with_values(user)
        resp = api.get(STATUS_URL.format(user_id=user.pk))
        assert resp.status_code == 200, resp.content
        assert resp.json()["data"]["identities"], resp.content
        text = resp.content.decode()
        for value in ("vegan", "Центр", "Заводской", "500", "es@example.com", "79992230001",
                      "Эля", "Пенза", "bot:test:", "es-user"):
            assert value not in text, value

    def test_reading_the_status_creates_nothing(self, api, user):
        before_users = User.objects.count()
        before_rows = UserPersonalContext.objects.count()
        resp = api.get(STATUS_URL.format(user_id=user.pk))
        assert resp.status_code == 200, resp.content
        assert User.objects.count() == before_users
        assert UserPersonalContext.objects.count() == before_rows == 0

    def test_the_read_is_journalled_as_its_own_operation(self, api, user):
        from privacy_audit.models import PersonalDataAccessLog

        op = PersonalDataAccessLog.Operation.ERASURE_STATUS_READ
        assert not PersonalDataAccessLog.objects.filter(operation=op).exists()
        resp = api.get(STATUS_URL.format(user_id=user.pk))
        assert resp.status_code == 200, resp.content
        row = PersonalDataAccessLog.objects.get(operation=op)
        assert row.object_id == user.pk
        assert row.result == PersonalDataAccessLog.Result.ALLOWED


class TestDeletedSubject:
    def test_a_deleted_subject_reads_its_status_through_the_bot(self, api, user):
        """Подтвердить стирание уже удалённого можно только чтением у удалённого:
        общий сторож принимает 404 у ручек стирания, здесь — строго 200."""
        user.is_active = False
        user.deleted_at = timezone.now()
        user.save(update_fields=["is_active", "deleted_at"])
        resp = api.get(STATUS_URL.format(user_id=user.pk))
        assert resp.status_code == 200, resp.content
        assert _account(resp.json()["data"])["context_row"] == "absent"
