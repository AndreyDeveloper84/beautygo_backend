"""DRF-1955 — стоп по заявке на удаление смотрит на ЧЕЛОВЕКА, а не на строку.

Находка #21/#22: две рекомендательные ручки стоят за
``IsBotServiceWithVerifiedClient``, а он кладёт в ``request.user`` результат
``resolve_external_user`` — действующего резолвера. Привязка к
деактивированному или мягко удалённому аккаунту для него пуста, и он
возвращает изолированный прокси. Гейт ``deletion_block_for(request.user)``
смотрел ровно на эту строку и не видел открытую заявку аккаунта: вместо 423
уезжали полки. Окно настоящее — после стирания каталога (``is_active=False``,
``deleted_at``) заявка остаётся PROCESSING до подтверждения бота.

Правило (решение главного окна 15.09): корень — ``linked_user`` привязанного
прокси независимо от состояния аккаунта; строки — ``subject_users(root)`` и
сама строка; открытая заявка на любой из них — стоп. Служебный tombstone
корнем не раскрывается: иначе всё, что перевешено на него, стало бы «одним
человеком».

У каждого отказа — парная положительная стража: чужая заявка не запирает,
прокси без привязки и без заявок не заперт.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from users.deletion_executor import tombstone_user
from users.deletion_requests import deletion_block_for
from users.models import DeletionRequest, User

pytestmark = pytest.mark.django_db

TOKEN = "test-bearer-1955"  # pragma: allowlist secret
PROXY_ID = "bot:gate-1955"
CATALOG_URL = "/api/v1/internal/me/catalog/recommendations/"
RESOLVE_URL = "/api/v1/internal/recommendation/resolve/"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = TOKEN
    settings.RECOMMENDATION_CANDIDATE_SOURCE = (
        "recommendation.tests.test_resolve_endpoint.fixture_source_factory"
    )


def _api(external_user_id: str = PROXY_ID) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


def _account(username: str = "gate1955-account") -> User:
    return User.objects.create_user(
        username=username, password="x",  # pragma: allowlist secret
        role="client",
    )


def _proxy(username: str = PROXY_ID, *, linked_user: User | None = None) -> User:
    return User.objects.create(
        username=username, role="client", is_proxy=True, is_guest=False, linked_user=linked_user,
    )


def _open_request(user: User, status=DeletionRequest.Status.PROCESSING) -> DeletionRequest:
    return DeletionRequest.objects.create(
        user=user, initiator="bot", status=status,
        deadline_at=timezone.now() + timedelta(days=7),
    )


def _erase_state(account: User, state: str) -> None:
    if state == "deactivated":
        User.objects.filter(pk=account.pk).update(is_active=False)
    else:
        User.objects.filter(pk=account.pk).update(deleted_at=timezone.now())


def _assert_refusal(r, req: DeletionRequest) -> None:
    assert r.status_code == 423, r.content
    err = r.json()["error"]
    assert err["code"] == "DELETION_IN_PROGRESS"
    assert err["details"]["request_id"] == str(req.pk)


# ---------------------------------------------------------------------------
# Ручки — отказ при привязке к неактивному / удалённому аккаунту (#21/#22)
# ---------------------------------------------------------------------------


class TestTheFallbackProxyDoesNotHideTheAccountsRequest:
    @pytest.mark.parametrize("state", ["deactivated", "soft_deleted"])
    def test_catalog_recommendations_are_423(self, state):
        account = _account()
        _proxy(linked_user=account)
        req = _open_request(account)
        _erase_state(account, state)

        _assert_refusal(_api().post(CATALOG_URL, {"goal": "маникюр"}, format="json"), req)

    @pytest.mark.parametrize("state", ["deactivated", "soft_deleted"])
    def test_resolve_is_423_before_the_body_is_read(self, state):
        account = _account()
        _proxy(linked_user=account)
        req = _open_request(account)
        _erase_state(account, state)

        _assert_refusal(_api().post(RESOLVE_URL, {"garbage": True}, format="json"), req)


class TestAPreBindingProxyRequestIsThePersons:
    def test_the_bound_account_is_stopped_by_its_proxys_request(self):
        """Заявка, принятая на прокси ДО привязки, — заявка того же человека (DRF-1038)."""
        account = _account()
        proxy = _proxy(linked_user=account)
        req = _open_request(proxy, status=DeletionRequest.Status.REQUESTED)

        _assert_refusal(_api().post(CATALOG_URL, {"goal": "маникюр"}, format="json"), req)


class TestPositivePairs:
    def test_a_strangers_open_request_does_not_lock(self):
        account = _account()
        _proxy(linked_user=account)
        stranger = _account("gate1955-stranger")
        _open_request(stranger)

        assert _api().post(CATALOG_URL, {"goal": "маникюр"}, format="json").status_code == 200

    def test_an_unbound_proxy_without_requests_is_not_locked(self):
        _proxy()
        assert _api().post(CATALOG_URL, {"goal": "маникюр"}, format="json").status_code == 200

    def test_a_tombstone_root_is_not_expanded(self):
        """Прокси, искусственно привязанный к tombstone, не наследует заявки
        других строк, перевешенных на служебного пользователя."""
        tomb = tombstone_user()
        _proxy(linked_user=tomb)
        other = _proxy("bot:gate-1955-other", linked_user=tomb)
        _open_request(other)

        assert _api().post(CATALOG_URL, {"goal": "маникюр"}, format="json").status_code == 200


# ---------------------------------------------------------------------------
# deletion_block_for — единственный читатель состояния заявки
# ---------------------------------------------------------------------------


class TestDeletionBlockForReadsThePerson:
    def test_a_proxy_sees_its_bound_accounts_request_whatever_the_account_state(self):
        account = _account()
        proxy = _proxy(linked_user=account)
        req = _open_request(account)
        _erase_state(account, "soft_deleted")
        proxy.refresh_from_db()

        assert deletion_block_for(proxy) == req

    def test_an_account_sees_its_pre_binding_proxys_request(self):
        account = _account()
        proxy = _proxy(linked_user=account)
        req = _open_request(proxy, status=DeletionRequest.Status.REQUESTED)

        assert deletion_block_for(account) == req

    def test_a_stranger_is_none(self):
        account = _account()
        _open_request(_account("gate1955-stranger-2"))

        assert deletion_block_for(account) is None

    def test_a_tombstone_root_is_none(self):
        tomb = tombstone_user()
        proxy = _proxy(linked_user=tomb)
        _open_request(_proxy("bot:gate-1955-tomb-other", linked_user=tomb))

        assert deletion_block_for(proxy) is None

    def test_an_accounts_own_request_is_found(self):
        account = _account()
        req = _open_request(account, status=DeletionRequest.Status.REQUESTED)

        assert deletion_block_for(account) == req
