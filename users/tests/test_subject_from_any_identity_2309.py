"""DRF-2309 — сторож прав: id прокси в URL и граница «до привязки».

Бот называет субъект заголовком ``X-External-User-ID: bot:<канал>:<id>``, а
``IsInternalBearerForSubject`` разрешает его через ``_follow_binding``
(``for_authorization=True``): для привязанного прокси — в аккаунт. Поэтому
запрос с id ПРИВЯЗАННОГО прокси в URL отказывается (403) раньше, чем вид
что-то сотрёт или прочитает — ни стирания одного прокси, ни ответа «стёрто» по
нему нет. Бот после привязки обязан слать ключ аккаунта (ai-bot-platform,
DRF-2309 — переспрос ключа перед стиранием).

И обратная граница: НЕПРИВЯЗАННЫЙ прокси видит и стирает только себя —
аккаунт появляется в его наборе лишь через ``linked_user``, который ставит
подтверждённая привязка. Только тесты: поведение прав не меняется, сторож
держит его от дрейфа.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from users.tests.test_forget_all_catalog_2214 import _remembered_counts, _seed_remembered
from users.tests.test_memory_erasure_matrix import VALID_TOKEN, _set_token  # noqa: F401

User = get_user_model()
pytestmark = pytest.mark.django_db

BASE = "/api/v1/internal/users/{id}/personal-data/"


def _as(external_user_id: str) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


def _all_zero(counts: dict) -> bool:
    return all(v == 0 for v in counts.values())


def _all_present(counts: dict) -> bool:
    return all(v >= 1 for v in counts.values())


@pytest.fixture
def linked():
    account = User.objects.create_user(
        username="subj2309_account", password="x", role="client", phone="+79995552309"
    )
    proxy = User.objects.create(
        username="bot:max:2309001", role="client", is_proxy=True, linked_user=account
    )
    _seed_remembered(account)
    _seed_remembered(proxy)
    return account, proxy


class TestALinkedProxyIdInTheUrlIsRefused:
    @pytest.mark.parametrize(
        ("method", "suffix"),
        [("delete", ""), ("get", "export/"), ("get", "erasure-status/")],
        ids=["C5.2 delete", "export", "C5.3 status"],
    )
    def test_403_before_the_view(self, linked, method, suffix) -> None:
        account, proxy = linked
        resp = getattr(_as(proxy.username), method)(BASE.format(id=proxy.pk) + suffix)
        assert resp.status_code == 403, resp.content
        # Ничего не стёрто — ни у прокси, ни у аккаунта.
        assert _all_present(_remembered_counts(proxy))
        assert _all_present(_remembered_counts(account))

    def test_positive_pair_the_account_id_with_the_same_header_passes(self, linked) -> None:
        """Тот же заголовок и id аккаунта — субъект целиком (аккаунт и прокси)."""
        account, proxy = linked
        resp = _as(proxy.username).delete(BASE.format(id=account.pk))
        assert resp.status_code == 200, resp.content
        assert _all_zero(_remembered_counts(account))
        assert _all_zero(_remembered_counts(proxy))


class TestAnUnlinkedProxySeesOnlyItself:
    def test_export_carries_no_account(self) -> None:
        account = User.objects.create_user(
            username="subj2309_other", password="x", role="client", phone="+79995552310"
        )
        lonely = User.objects.create(username="bot:max:2309003", role="client", is_proxy=True)
        _seed_remembered(account)
        _seed_remembered(lonely)

        resp = _as(lonely.username).get(BASE.format(id=lonely.pk) + "export/")
        assert resp.status_code == 200, resp.content
        data = resp.json()["data"]
        assert data["user_id"] == str(lonely.pk)  # положительно: выгрузка своя
        names = {i["external_user_id"] for i in data.get("linked_identities", [])}
        assert account.username not in names
        assert names == set()  # empty-assert-ok: user_id выше — своё на месте, чужих нет

    def test_erasure_touches_only_itself(self) -> None:
        bystander = User.objects.create_user(
            username="subj2309_bystander", password="x", role="client", phone="+79995552311"
        )
        lonely = User.objects.create(username="bot:max:2309004", role="client", is_proxy=True)
        _seed_remembered(bystander)
        _seed_remembered(lonely)

        resp = _as(lonely.username).delete(BASE.format(id=lonely.pk))
        assert resp.status_code == 200, resp.content
        assert _all_zero(_remembered_counts(lonely))
        assert _all_present(_remembered_counts(bystander))
