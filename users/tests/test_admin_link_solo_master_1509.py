"""Кнопка «Связать с Ayla» делает то же, что ручка bind-external (DRF-1509, §148).

§148: регистрация соло-мастера ПРОБУЕТ связаться сама → не вышло →
``SETUP_PENDING`` → оператор добивает руками. Ручка
``POST /internal/users/bind-external/`` для оператора закрыта (токена
провижининга на пилоте нет), и «руками» до этого PR означало
Django-shell.

Здесь закрепляется, что действие админки:

* связывает **тем же сервисом** и тем же путём — ``linked_user`` на
  прокси-строке, — так что ``GET /internal/me/identity/`` для той же
  MAX-личности после нажатия возвращает ``is_proxy=false`` и настоящий
  ключ мастера (это читает автоматика бота, S2);
* пишет аудит **с актором** и без актора не выполняется (§143,
  положительный контроль: с актором — связывает, без — нет);
* на повторе отказывает с названной причиной и не заводит второй
  привязки (§148 retryable);
* показывает в списке только ждущие строки;
* три отказа — три разных сообщения.

Целевое доказательство — подменой (см. отчёт PR): при снятии проверки
актора в сервисе краснеют ровно тесты про актора, при обходе сервиса
прямой записью ``linked_user`` — тесты про аудит.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory
from django.urls import reverse
from rest_framework.test import APIClient

from analytics import event_catalogue
from analytics.models import AnalyticsEvent
from users import admin_actions
from users.models import PendingExternalIdentity, SpecialistProfile, User
from users.services import (
    BindTargetNotFoundError,
    ExternalIdentityAlreadyBoundError,
    ExternalIdentityNotFoundError,
    IdentityBindingActorRequiredError,
    INITIATOR_ADMIN_LINK_SOLO_MASTER,
    bind_external_identity_by_operator,
    resolve_external_user,
)

pytestmark = pytest.mark.django_db

INTERNAL_TOKEN = "test-ayla-internal-token-1509"  # pragma: allowlist secret
IDENTITY_URL = "/api/v1/internal/me/identity/"
CHANGELIST = "admin:users_pendingexternalidentity_changelist"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = INTERNAL_TOKEN


def _operator(username="op-1509"):
    return User.objects.create_superuser(
        username=username, password="pw", email=f"{username}@x.y",  # pragma: allowlist secret
        role="admin",
    )


def _master(username="master-1509", phone="+79001509001"):
    """Соло-мастер как он заводится формой админки: User(role=specialist)
    + SpecialistProfile (сигнал создаёт профиль сам)."""
    user = User.objects.create_user(
        username=username, password="x", role="specialist", phone=phone,  # pragma: allowlist secret
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.display_name = f"Мастер {username}"
    profile.save(update_fields=["display_name"])
    return profile


def _bot_presents(external_id):
    """Бот обратился в Ayla от имени MAX-личности — прокси заведён лениво,
    ровно как это делает автоматическая попытка S2 через
    ``GET /internal/me/identity/``."""
    resp = _bot_api(external_id).get(IDENTITY_URL)
    assert resp.status_code == 200, resp.content
    return User.objects.get(username=external_id)


def _bot_api(external_id) -> APIClient:
    client = APIClient()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {INTERNAL_TOKEN}"
    client.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_id
    return client


def _audit(external_id):
    return list(
        AnalyticsEvent.objects.filter(
            event_name=event_catalogue.EXTERNAL_IDENTITY_BOUND,
            payload__external_user_id=external_id,
        ).order_by("created_at")
    )


# ── сервис: тот же путь, что ручка ────────────────────────────────


def test_operator_link_writes_audit_with_actor():
    op = _operator()
    master = _master()
    proxy = _bot_presents("bot:max:1509-a")

    bound = bind_external_identity_by_operator(
        "bot:max:1509-a", master.user_id, actor=op, request_id="req-1509",
    )

    assert bound.pk == proxy.pk
    proxy.refresh_from_db()
    assert proxy.linked_user_id == master.user_id
    (event,) = _audit("bot:max:1509-a")
    assert event.payload["result"] == "created"
    assert event.payload["initiator"] == INITIATOR_ADMIN_LINK_SOLO_MASTER
    assert event.payload["initiator_user_id"] == str(op.pk)
    assert event.payload["initiator_role"] == "admin"
    assert event.payload["request_id"] == "req-1509"
    assert event.payload["target_user_id"] == str(master.user_id)


def test_identity_endpoint_returns_real_key_after_link():
    """Требование ayla-06 (S2): после нажатия тот же
    ``internal/me/identity/`` для той же MAX-личности обязан вернуть
    ``is_proxy=false`` и настоящий ключ — иначе бот продолжит получать
    прокси и появятся две правды об одном человеке."""
    op = _operator()
    master = _master()
    external_id = "bot:max:1509-identity"
    proxy = _bot_presents(external_id)

    before = _bot_api(external_id).get(IDENTITY_URL).json()["data"]
    assert before == {"ayla_user_id": str(proxy.pk), "is_proxy": True}

    bind_external_identity_by_operator(external_id, master.user_id, actor=op)

    after = _bot_api(external_id).get(IDENTITY_URL).json()["data"]
    assert after == {"ayla_user_id": str(master.user_id), "is_proxy": False}
    assert resolve_external_user(external_id).pk == master.user_id


def test_link_without_actor_is_refused_before_any_write():
    """§143 fail-closed. Положительный контроль — предыдущие тесты: с
    актором тот же вызов связывает."""
    master = _master()
    proxy = _bot_presents("bot:max:1509-noactor")

    for actor in (None, AnonymousUser()):
        with pytest.raises(IdentityBindingActorRequiredError):
            bind_external_identity_by_operator(
                "bot:max:1509-noactor", master.user_id, actor=actor,
            )

    proxy.refresh_from_db()
    assert proxy.linked_user_id is None
    assert _audit("bot:max:1509-noactor") == []


def test_repeat_is_refused_by_name_and_binds_nothing_twice():
    """§148: SETUP_PENDING retryable — повтор безопасен и назван."""
    op = _operator()
    master = _master()
    other = _master("master-1509-b", "+79001509002")
    _bot_presents("bot:max:1509-repeat")
    bind_external_identity_by_operator("bot:max:1509-repeat", master.user_id, actor=op)
    audit_before = len(_audit("bot:max:1509-repeat"))

    # тот же мастер — и другой мастер: оба «уже связан»
    for target in (master, other):
        with pytest.raises(ExternalIdentityAlreadyBoundError):
            bind_external_identity_by_operator(
                "bot:max:1509-repeat", target.user_id, actor=op,
            )

    proxy = User.objects.get(username="bot:max:1509-repeat")
    assert proxy.linked_user_id == master.user_id
    assert User.objects.filter(username="bot:max:1509-repeat").count() == 1
    assert len(_audit("bot:max:1509-repeat")) == audit_before


def test_missing_proxy_row_is_refused_and_not_created():
    """Отличие от ручки: ручка заводит прокси лениво, оператор — нет.
    Нет строки = бот с этой личностью в Ayla не приходил, связывать
    нечего."""
    op = _operator()
    master = _master()
    with pytest.raises(ExternalIdentityNotFoundError):
        bind_external_identity_by_operator(
            "bot:max:1509-never-seen", master.user_id, actor=op,
        )
    assert not User.objects.filter(username="bot:max:1509-never-seen").exists()


def test_target_must_be_an_active_specialist():
    op = _operator()
    _bot_presents("bot:max:1509-target")
    client_user = User.objects.create_user(
        username="client-1509", password="x", role="client", phone="+79001509003",  # pragma: allowlist secret
    )
    inactive = _master("master-1509-off", "+79001509004")
    inactive.user.is_active = False
    inactive.user.save(update_fields=["is_active"])

    for target_pk in (client_user.pk, inactive.user_id, op.pk):
        with pytest.raises(BindTargetNotFoundError):
            bind_external_identity_by_operator(
                "bot:max:1509-target", target_pk, actor=op,
            )
    assert User.objects.get(username="bot:max:1509-target").linked_user_id is None


# ── админка ──────────────────────────────────────────────────────


def test_changelist_shows_only_rows_waiting_for_the_operator(client):
    op = _operator()
    master = _master()
    _bot_presents("bot:max:1509-waiting")
    _bot_presents("bot:max:1509-done")
    bind_external_identity_by_operator("bot:max:1509-done", master.user_id, actor=op)

    assert set(PendingExternalIdentity.objects.values_list("username", flat=True)) == {
        "bot:max:1509-waiting",
    }
    client.force_login(op)
    resp = client.get(reverse(CHANGELIST))
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "bot:max:1509-waiting" in html
    assert "bot:max:1509-done" not in html
    assert master.user.username not in html
    assert 'value="link_to_ayla"' in html


def _apply(client, proxy, target_profile):
    return client.post(
        reverse(CHANGELIST),
        {
            "action": admin_actions.LINK_ACTION_NAME,
            "_selected_action": [str(proxy.pk)],
            "apply": "1",
            "index": "0",
            "target": str(target_profile.pk),
        },
        follow=True,
    )


def _messages(resp):
    return [str(m) for m in get_messages(resp.wsgi_request)]


def test_action_shows_form_then_links_and_names_the_identity_answer(client):
    op = _operator()
    master = _master()
    external_id = "bot:max:1509-button"
    proxy = _bot_presents(external_id)
    client.force_login(op)

    step1 = client.post(
        reverse(CHANGELIST),
        {"action": admin_actions.LINK_ACTION_NAME,
         "_selected_action": [str(proxy.pk)], "index": "0"},
    )
    assert step1.status_code == 200
    html = step1.content.decode()
    assert external_id in html and 'name="target"' in html
    assert master.display_name in html

    step2 = _apply(client, proxy, master)
    assert step2.status_code == 200
    (msg,) = _messages(step2)
    assert msg.startswith("Связано:")
    assert "is_proxy=false" in msg and str(master.user_id) in msg
    assert op.get_username() in msg

    proxy.refresh_from_db()
    assert proxy.linked_user_id == master.user_id
    (event,) = _audit(external_id)
    assert event.payload["initiator_user_id"] == str(op.pk)
    # и бот увидит настоящий ключ
    data = _bot_api(external_id).get(IDENTITY_URL).json()["data"]
    assert data == {"ayla_user_id": str(master.user_id), "is_proxy": False}
    # строка ушла из списка ждущих
    assert not PendingExternalIdentity.objects.filter(pk=proxy.pk).exists()


def test_action_repeat_says_already_bound(client):
    op = _operator()
    master = _master()
    proxy = _bot_presents("bot:max:1509-twice")
    client.force_login(op)
    _apply(client, proxy, master)

    resp = _apply(client, proxy, master)
    (msg,) = _messages(resp)
    assert msg.startswith("Уже связан:")
    assert str(master.user_id) in msg
    proxy.refresh_from_db()
    assert proxy.linked_user_id == master.user_id
    assert [e.payload["result"] for e in _audit("bot:max:1509-twice")] == ["created"]


def test_action_three_refusals_have_three_messages(client):
    op = _operator()
    master = _master()
    client.force_login(op)

    # 1. человек не найден — мастер вне разрешённого набора формы
    proxy = _bot_presents("bot:max:1509-refusals")
    resp = client.post(
        reverse(CHANGELIST),
        {"action": admin_actions.LINK_ACTION_NAME,
         "_selected_action": [str(proxy.pk)], "apply": "1", "index": "0",
         "target": "999999"},
        follow=True,
    )
    assert _messages(resp) == [admin_actions.MSG_TARGET_NOT_FOUND]

    # 2. нет прокси-строки — выбранный pk не прокси
    real = _master("master-1509-real", "+79001509005").user
    resp = _apply(client, real, master)
    (msg,) = _messages(resp)
    assert msg.startswith("Нет прокси-строки:")

    # 3. уже связан
    bind_external_identity_by_operator("bot:max:1509-refusals", master.user_id, actor=op)
    resp = _apply(client, proxy, master)
    (msg,) = _messages(resp)
    assert msg.startswith("Уже связан:")


def test_action_without_actor_binds_nothing():
    """Положительный контроль §143 на уровне действия: сервис требует
    актора, действие его не подставляет. Прямой вызов действия с
    анонимным ``request.user`` — отказ с сообщением, без записи."""
    master = _master()
    proxy = _bot_presents("bot:max:1509-anon")
    modeladmin = admin_actions.PendingExternalIdentityAdmin(
        PendingExternalIdentity, __import__("django.contrib.admin").contrib.admin.site,
    )
    request = RequestFactory().post(
        reverse(CHANGELIST),
        {"action": admin_actions.LINK_ACTION_NAME,
         "_selected_action": [str(proxy.pk)], "apply": "1",
         "target": str(master.pk)},
    )
    request.user = AnonymousUser()
    request.session = {}
    request._messages = FallbackStorage(request)

    result = admin_actions.link_to_ayla(
        modeladmin, request, PendingExternalIdentity.objects.all(),
    )

    assert result is None
    assert [str(m) for m in request._messages] == [admin_actions.MSG_ACTOR_REQUIRED]
    proxy.refresh_from_db()
    assert proxy.linked_user_id is None
    assert _audit("bot:max:1509-anon") == []
