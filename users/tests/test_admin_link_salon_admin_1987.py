"""«Связать с Ayla» для администратора салона (F10, ручной bootstrap, раздел Q).

Решение владельца 15.09: для пилота одна owner/admin-учётка салона заводится
``provision_salon_admin`` и связывается с MAX-личностью. Команда заводит
``role=admin``, а операторская привязка принимала только ``specialist``
(соло-мастер), S2S — только ``client``. Без привязки салонный API отвечает
боту 403: ``IsTenantAdmin`` ищет роль у пользователя, в которого разрешилась
``X-External-User-ID``.

Здесь закрепляется:

* операторская привязка принимает ``role=admin`` **только** при активной
  ``TenantUserRelationship(role=admin)`` в активном салоне, и не принимает
  персонал платформы (``is_superuser`` / ``is_staff``) — MAX-личность не
  ведёт в учётку платформы;
* отказы названы и попадают в аудит своей причиной;
* S2S не расширен; specialist и client на операторском пути — как раньше;
* после привязки салонный API отвечает 200, до неё — 403; после отзыва роли
  у связанного администратора — снова 403 (связь остаётся, права уходят).
"""
from __future__ import annotations

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse
from rest_framework.test import APIClient

from analytics import event_catalogue
from analytics.models import AnalyticsEvent
from tenants.models import Tenant
from users import admin_actions, services
from users.models import SpecialistProfile, TenantUserRelationship, User
from users.services import (
    BIND_TARGET_ROLES_OPERATOR,
    BindTargetNotFoundError,
    bind_external_identity,
    bind_external_identity_by_operator,
)

pytestmark = pytest.mark.django_db

INTERNAL_TOKEN = "test-ayla-internal-token-1987"  # pragma: allowlist secret
IDENTITY_URL = "/api/v1/internal/me/identity/"
DAY_URL = "/api/v1/tenants/me/day/"
CHANGELIST = "admin:users_pendingexternalidentity_changelist"

REASON_WITHOUT_ROLE = "admin_without_active_salon_role"
REASON_PLATFORM_STAFF = "target_is_platform_staff"
INITIATOR_SALON_ADMIN = "admin_link_salon_admin"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = INTERNAL_TOKEN


@pytest.fixture
def salon():
    return Tenant.objects.create(slug="link-1987", name="Салон привязки", city="Пенза")


def _operator():
    return User.objects.create_superuser(
        username="op-1987", password="pw", email="op-1987@x.y",  # pragma: allowlist secret
        role="admin",
    )


_phones = iter(f"+7900198700{i}" if i < 10 else f"+790019870{i}" for i in range(1, 99))


def _admin(first_name, *, tenant=None, tur_role="admin", tur_active=True):
    """Учётка, как её заводит ``provision_salon_admin``: ``role=admin`` + роль в салоне."""
    user = User.objects.create_user(
        username=f"adm-{first_name}", password="x", role="admin",  # pragma: allowlist secret
        phone=next(_phones), first_name=first_name,
    )
    if tenant is not None:
        TenantUserRelationship.objects.create(
            user=user, tenant=tenant, role=tur_role, is_active=tur_active,
        )
    return user


def _master(username="master-1987"):
    user = User.objects.create_user(
        username=username, password="x", role="specialist", phone=next(_phones),  # pragma: allowlist secret
    )
    return SpecialistProfile.objects.get(user=user)


def _bot_api(external_id, *, tenant_slug=None) -> APIClient:
    client = APIClient()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {INTERNAL_TOKEN}"
    client.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_id
    client.defaults["HTTP_X_APP_TYPE"] = "pro"
    if tenant_slug:
        client.defaults["HTTP_X_TENANT"] = tenant_slug
    return client


def _bot_presents(external_id):
    resp = _bot_api(external_id).get(IDENTITY_URL)
    assert resp.status_code == 200, resp.content
    return User.objects.get(username=external_id)


def _audit(external_id):
    return list(
        AnalyticsEvent.objects.filter(
            event_name=event_catalogue.EXTERNAL_IDENTITY_BOUND,
            payload__external_user_id=external_id,
        ).order_by("created_at")
    )


def _refused_by_name(external_id, target, reason):
    op = _operator()
    proxy = _bot_presents(external_id)
    with pytest.raises(BindTargetNotFoundError) as exc:
        bind_external_identity_by_operator(external_id, target.pk, actor=op)
    proxy.refresh_from_db()
    assert proxy.linked_user_id is None
    assert [e.payload["reason"] for e in _audit(external_id)] == [reason]
    named = getattr(services, "BindTargetNotSalonAdminError", None)
    assert named is not None, "нет именованного отказа BindTargetNotSalonAdminError"
    assert isinstance(exc.value, named)


# ── сервис ────────────────────────────────────────────────────────


def test_admin_with_active_salon_role_is_bound_and_audited(salon):  # 1
    op = _operator()
    admin = _admin("Сроль", tenant=salon)
    proxy = _bot_presents("bot:max:1987-ok")

    try:
        bind_external_identity_by_operator("bot:max:1987-ok", admin.pk, actor=op, request_id="req-1987")
    except BindTargetNotFoundError:
        refused = True
    else:
        refused = False
    assert not refused, "admin с активной ролью в салоне не привязан"

    proxy.refresh_from_db()
    assert proxy.linked_user_id == admin.pk
    (event,) = _audit("bot:max:1987-ok")
    assert event.payload["result"] == "created"
    assert event.payload["initiator"] == INITIATOR_SALON_ADMIN
    assert event.payload["initiator_user_id"] == str(op.pk)
    assert event.payload["target_user_id"] == str(admin.pk)


def test_admin_without_any_salon_role_is_refused_by_name(salon):  # 2
    _refused_by_name("bot:max:1987-norole", _admin("Безроли"), REASON_WITHOUT_ROLE)


def test_admin_with_revoked_role_is_refused_by_name(salon):  # 3
    admin = _admin("Отозван", tenant=salon, tur_active=False)
    _refused_by_name("bot:max:1987-revoked", admin, REASON_WITHOUT_ROLE)


def test_admin_account_with_staff_role_only_is_refused_by_name(salon):  # 4
    admin = _admin("Персонал", tenant=salon, tur_role=TenantUserRelationship.Role.STAFF)
    _refused_by_name("bot:max:1987-staffrole", admin, REASON_WITHOUT_ROLE)


def test_admin_of_inactive_salon_only_is_refused_by_name(salon):  # 5
    admin = _admin("Закрытый", tenant=salon)
    Tenant.all_objects.filter(pk=salon.pk).update(is_active=False)
    _refused_by_name("bot:max:1987-closed", admin, REASON_WITHOUT_ROLE)


def test_platform_superuser_with_salon_role_is_refused_by_name(salon):  # 6
    su = User.objects.create_superuser(
        username="su-1987", password="pw", email="su-1987@x.y", role="admin",  # pragma: allowlist secret
    )
    TenantUserRelationship.objects.create(
        user=su, tenant=salon, role=TenantUserRelationship.Role.ADMIN, is_active=True,
    )
    _refused_by_name("bot:max:1987-su", su, REASON_PLATFORM_STAFF)


def test_specialist_is_still_bound_on_operator_path():  # 7
    op = _operator()
    master = _master()
    proxy = _bot_presents("bot:max:1987-master")
    bind_external_identity_by_operator("bot:max:1987-master", master.user_id, actor=op)
    proxy.refresh_from_db()
    assert proxy.linked_user_id == master.user_id


def test_client_is_still_refused_on_operator_path():  # 8
    op = _operator()
    client_user = User.objects.create_user(
        username="client-1987", password="x", role="client", phone=next(_phones),  # pragma: allowlist secret
    )
    proxy = _bot_presents("bot:max:1987-client")
    with pytest.raises(BindTargetNotFoundError):
        bind_external_identity_by_operator("bot:max:1987-client", client_user.pk, actor=op)
    proxy.refresh_from_db()
    assert proxy.linked_user_id is None


def test_s2s_default_still_refuses_admin_with_salon_role(salon):  # 9
    admin = _admin("Сдвеслужбы", tenant=salon)
    with pytest.raises(BindTargetNotFoundError):
        bind_external_identity("bot:max:1987-s2s", admin.pk)
    assert not User.objects.filter(username="bot:max:1987-s2s", linked_user__isnull=False).exists()


def test_core_with_operator_roles_refuses_admin_without_role():  # 10
    """Условие роли живёт в самом ``bind_external_identity`` (и под блокировкой), а не только в обёртке."""
    admin = _admin("Ядро")
    with pytest.raises(BindTargetNotFoundError):
        bind_external_identity("bot:max:1987-core", admin.pk, target_roles=BIND_TARGET_ROLES_OPERATOR)
    assert not User.objects.filter(username="bot:max:1987-core", linked_user__isnull=False).exists()


# ── права на салонном API, настоящий стек ────────────────────────


def test_unbound_admin_identity_gets_403(salon):  # 11
    _admin("Доприв", tenant=salon)
    _bot_presents("bot:max:1987-before")
    resp = _bot_api("bot:max:1987-before", tenant_slug=salon.slug).get(DAY_URL)
    assert resp.status_code == 403, resp.content


def test_bound_admin_identity_gets_200(salon):  # 12
    op = _operator()
    admin = _admin("Послеприв", tenant=salon)
    _bot_presents("bot:max:1987-after")
    try:
        bind_external_identity_by_operator("bot:max:1987-after", admin.pk, actor=op)
    except BindTargetNotFoundError:
        refused = True
    else:
        refused = False
    assert not refused, "admin с активной ролью в салоне не привязан"
    resp = _bot_api("bot:max:1987-after", tenant_slug=salon.slug).get(DAY_URL)
    assert resp.status_code == 200, resp.content


def test_revoking_role_of_bound_admin_takes_rights_not_binding(salon):  # 18
    """Положительная стража: права — от роли, не от привязки. Связь ставится
    прямой записью, чтобы тест не зависел от нового пути."""
    admin = _admin("Отзыв", tenant=salon)
    proxy = _bot_presents("bot:max:1987-revoke")
    User.objects.filter(pk=proxy.pk).update(linked_user=admin)
    api = _bot_api("bot:max:1987-revoke", tenant_slug=salon.slug)
    assert api.get(DAY_URL).status_code == 200

    TenantUserRelationship.objects.filter(user=admin, tenant=salon).update(is_active=False)

    assert api.get(DAY_URL).status_code == 403
    proxy.refresh_from_db()
    assert proxy.linked_user_id == admin.pk


# ── админка ──────────────────────────────────────────────────────


def _messages(resp):
    return [str(m) for m in get_messages(resp.wsgi_request)]


def _post(client, proxy, **fields):
    data = {
        "action": admin_actions.LINK_ACTION_NAME,
        "_selected_action": [str(proxy.pk)],
        "apply": "1",
        "index": "0",
        **fields,
    }
    return client.post(reverse(CHANGELIST), data, follow=True)


def test_form_lists_only_admins_with_active_salon_role(client, salon):  # 13
    op = _operator()
    _admin("Виденсроль", tenant=salon)
    _admin("Невиденбезроли")
    _admin("Невиденотозван", tenant=salon, tur_active=False)
    su = User.objects.create_superuser(
        username="su-1987-form", password="pw", email="su-f@x.y", role="admin",  # pragma: allowlist secret
        first_name="Невиденсупер",
    )
    TenantUserRelationship.objects.create(
        user=su, tenant=salon, role=TenantUserRelationship.Role.ADMIN, is_active=True,
    )
    proxy = _bot_presents("bot:max:1987-form")
    client.force_login(op)

    resp = client.post(
        reverse(CHANGELIST),
        {"action": admin_actions.LINK_ACTION_NAME, "_selected_action": [str(proxy.pk)], "index": "0"},
    )

    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'name="admin_target"' in html
    assert "Виденсроль" in html
    for hidden in ("Невиденбезроли", "Невиденотозван", "Невиденсупер"):
        assert hidden not in html, hidden


def test_action_links_admin_and_says_administrator(client, salon):  # 14
    op = _operator()
    admin = _admin("Кнопка", tenant=salon)
    proxy = _bot_presents("bot:max:1987-button")
    client.force_login(op)

    resp = _post(client, proxy, admin_target=str(admin.pk))

    (msg,) = _messages(resp)
    assert msg.startswith("Связано:"), msg
    assert "администратор" in msg
    proxy.refresh_from_db()
    assert proxy.linked_user_id == admin.pk


def test_action_requires_exactly_one_target(client, salon):  # 15
    op = _operator()
    admin = _admin("Двое", tenant=salon)
    master = _master()
    proxy = _bot_presents("bot:max:1987-both")
    client.force_login(op)
    expected = getattr(admin_actions, "MSG_CHOOSE_ONE_TARGET", None)
    assert expected is not None, "нет сообщения MSG_CHOOSE_ONE_TARGET"

    both = _post(client, proxy, target=str(master.pk), admin_target=str(admin.pk))
    neither = _post(client, proxy)

    assert _messages(both) == [expected]
    assert _messages(neither) == [expected]
    proxy.refresh_from_db()
    assert proxy.linked_user_id is None


def test_forged_admin_without_role_gets_named_message(client, salon):  # 16
    op = _operator()
    admin = _admin("Подделка")
    proxy = _bot_presents("bot:max:1987-forged")
    client.force_login(op)
    expected = getattr(admin_actions, "MSG_ADMIN_WITHOUT_ROLE", None)
    assert expected is not None, "нет сообщения MSG_ADMIN_WITHOUT_ROLE"

    resp = _post(client, proxy, admin_target=str(admin.pk))

    assert _messages(resp) == [expected]
    proxy.refresh_from_db()
    assert proxy.linked_user_id is None
