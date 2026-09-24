"""DRF-2442: ``POST /api/v1/internal/specialists/<uuid>/identity/``.

Решение владельца §77 п.38 (24.09): человека в регистрации мастеров нет.
Красное листа: мастер, принявший приглашение, получает 403 на ручках своего
кабинета, потому что `users_user.linked_user_id` у его личности пуст, а
записать его было некому. После вызова этой двери тот же заголовок
``X-External-User-ID`` проходит сторож субъекта.

Узлы, которые держат смысл (а не форму):

* **кабинет открывается на самом деле** — проверка идёт живой ручкой под
  ``IsInternalBearerForSpecialistSubject``, до и после; подмена: стереть связь
  → узел краснеет;
* **два разных отказа не сливаются** — 403 сторожа («нас не пустили») и
  404/409 сервиса («мы просим не про того» / «про того, но связывать нечего»);
* **``client`` не связывается** этой дверью ни при каких условиях, а
  ``specialist`` связывается — обе стороны, иначе запрет неотличим от поломки;
* **повтор ничего не удваивает** — ни связи, ни строки запроса;
* **на отказе не записано ничего** — ни ребра, ни строки.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from tenants.models import Tenant
from users.checks import specialist_identity_link_token_check
from users.models import SpecialistIdentityLinkRequest, SpecialistProfile, User
from users.services import INITIATOR_BOT_SPECIALIST_IDENTITY_LINK
from users.specialist_identity_linking import (
    SpecialistIdentityLinkRefused,
    link_specialist_identity,
)

pytestmark = pytest.mark.django_db

DOOR = "test-specialist-identity-link-2442"  # noqa: S105
GENERAL = "test-general-bot-token-2442"  # noqa: S105
IDENTITY = "test-identity-provisioning-2442"  # noqa: S105
TENANT_PROV = "test-tenant-provisioning-2442"  # noqa: S105
SALON_ADMIN = "test-salon-admin-link-2442"  # noqa: S105

EXTERNAL = "bot:max:2442001"
ACTOR = "bot:onboarding_accept"


@pytest.fixture(autouse=True)
def tokens(settings):
    settings.AYLA_SPECIALIST_IDENTITY_LINK_TOKEN = DOOR
    settings.AYLA_INTERNAL_API_TOKEN = GENERAL
    settings.AYLA_IDENTITY_PROVISIONING_TOKEN = IDENTITY
    settings.AYLA_TENANT_PROVISIONING_TOKEN = TENANT_PROV
    settings.AYLA_SALON_ADMIN_LINK_TOKEN = SALON_ADMIN


@pytest.fixture(autouse=True)
def _clean_throttle_history():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def salon() -> Tenant:
    return Tenant.objects.create(slug="idlink-2442", name="Салон приглашений")


def _specialist(salon: Tenant, *, username: str, status=None) -> SpecialistProfile:
    user = User.objects.create_user(username=username, password="x", role="specialist")
    user.tenant = salon
    user.save(update_fields=["tenant"])
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = salon
    profile.status = status or SpecialistProfile.ProfileStatus.ACTIVE
    profile.save()
    return profile


@pytest.fixture
def master(salon) -> SpecialistProfile:
    return _specialist(salon, username="idlink-2442-m1")


@pytest.fixture
def proxy(db) -> User:
    """Личность, которую бот уже предъявлял каталогу, ещё не связанная."""
    return User.objects.create(
        username=EXTERNAL, role="client", is_proxy=True, is_guest=False,
    )


def _url(specialist_id) -> str:
    return f"/api/v1/internal/specialists/{specialist_id}/identity/"


def _door(token: str = DOOR) -> APIClient:
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return c


def _body(**over) -> dict:
    base = {
        "external_user_id": EXTERNAL,
        "actor": ACTOR,
        "correlation_id": "corr-2442-1",
        "idempotency_key": "idem-2442-0001",
    }
    base.update(over)
    return base


def _workspace(profile: SpecialistProfile, external: str = EXTERNAL):
    """Живая ручка кабинета: тот же путь, которым ходит мастер из Mini App."""
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {GENERAL}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external
    return c.get(f"/api/v1/internal/specialists/{profile.pk}/profile/")


@contextmanager
def _capturing(name: str, caplog):
    """``users`` стоит ``propagate=False`` — перехват на именованный логгер."""
    target = logging.getLogger(name)
    target.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger=name):
            yield
    finally:
        target.removeHandler(caplog.handler)


# ─── красное листа: кабинет закрыт до двери и открыт после ──────────────────


class TestTheWorkspaceActuallyOpens:
    def test_before_the_call_the_workspace_refuses_and_after_it_answers(self, master, proxy) -> None:
        before = _workspace(master)
        assert before.status_code == 403  # красное листа: личность не связана

        response = _door().post(_url(master.pk), _body(), format="json")

        assert response.status_code == 201
        body = response.json()["data"]
        assert body["specialist_id"] == str(master.pk)
        assert body["ayla_user_id"] == str(master.user_id)
        assert body["status"] == "created"
        # …и тот же заголовок теперь проходит сторож субъекта
        assert _workspace(master).status_code == 200
        proxy.refresh_from_db()
        assert proxy.linked_user_id == master.user_id

    def test_readback_is_the_live_gate_not_the_column(self, master, proxy) -> None:
        """Подмена: стереть связь после записи — успех обязан исчезнуть.

        Если readback читал бы ``linked_user_id`` своим запросом, он бы прошёл
        и здесь: значение мы пишем сами. Он обязан спрашивать боевой путь.
        """
        from users import specialist_identity_linking as mod

        real_resolver = mod.resolve_external_user_readonly

        def _severed(external_user_id: str):
            resolved = real_resolver(external_user_id)
            # то, что видит рантайм, если связи нет: изолированная прокси-строка
            return User.objects.filter(username=external_user_id).first() if resolved is None else (
                User.objects.filter(username=external_user_id, is_proxy=True).first()
            )

        mod.resolve_external_user_readonly = _severed
        try:
            response = _door().post(_url(master.pk), _body(), format="json")
        finally:
            mod.resolve_external_user_readonly = real_resolver

        assert response.status_code == 500
        assert response.json()["error"]["details"]["reason"] == "readback_failed"


# ─── сторож двери: «нас не пустили» — отдельный отказ ───────────────────────


class TestTheDoorsOwnCredential:
    @pytest.mark.parametrize(
        "token",
        (GENERAL, IDENTITY, TENANT_PROV, SALON_ADMIN, "", "not-a-token"),
    )
    def test_no_sibling_secret_opens_this_door(self, master, proxy, token: str) -> None:
        client = APIClient()
        if token:
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        response = client.post(_url(master.pk), _body(), format="json")

        assert response.status_code == 403
        proxy.refresh_from_db()
        assert proxy.linked_user_id is None  # на отказе не записано ничего
        assert SpecialistIdentityLinkRequest.objects.count() == 0

    def test_an_empty_setting_closes_the_door(self, settings, master, proxy) -> None:
        settings.AYLA_SPECIALIST_IDENTITY_LINK_TOKEN = ""

        response = _door("").post(_url(master.pk), _body(), format="json")

        assert response.status_code == 403

    def test_the_guards_refusal_is_not_the_services_refusal(self, master, proxy) -> None:
        """403 «не пустили» и 404 «не тот субъект» — разные ответы, не один."""
        refused_by_guard = APIClient().post(_url(master.pk), _body(), format="json")
        refused_by_subject = _door().post(_url(uuid.uuid4()), _body(), format="json")

        assert refused_by_guard.status_code == 403
        assert refused_by_subject.status_code == 404
        assert refused_by_subject.json()["error"]["details"]["reason"] == "specialist_not_found"

    def test_a_secret_equal_to_a_sibling_fails_at_boot(self, settings) -> None:
        # положительно: при разных значениях проверка молчит
        assert specialist_identity_link_token_check(None) == []

        settings.AYLA_SPECIALIST_IDENTITY_LINK_TOKEN = SALON_ADMIN
        errors = specialist_identity_link_token_check(None)

        assert [e.id for e in errors] == ["users.E005"]


# ─── роль цели: specialist да, client никогда ───────────────────────────────


class TestOnlyASpecialistIsLinked:
    def test_a_client_account_is_refused_and_nothing_is_written(self, salon, proxy) -> None:
        # Профиль заводит сигнал на ``role=specialist``; роль потом сменили —
        # ровно тот случай, когда профиль в URL есть, а цель уже не мастер.
        profile = _specialist(salon, username="idlink-2442-client")
        User.objects.filter(pk=profile.user_id).update(role="client")
        assert SpecialistProfile.objects.filter(pk=profile.pk).exists()  # строка на месте

        response = _door().post(_url(profile.pk), _body(), format="json")

        assert response.status_code == 409
        assert response.json()["error"]["details"]["reason"] == "specialist_not_linkable"
        proxy.refresh_from_db()
        assert proxy.linked_user_id is None
        assert SpecialistIdentityLinkRequest.objects.count() == 0

    def test_a_specialist_in_draft_is_linked_too(self, salon, proxy) -> None:
        """Связь не ждёт публикации: DRAFT-профиль — тот же субъект."""
        draft = _specialist(
            salon, username="idlink-2442-draft", status=SpecialistProfile.ProfileStatus.DRAFT,
        )

        response = _door().post(_url(draft.pk), _body(), format="json")

        assert response.status_code == 201
        proxy.refresh_from_db()
        assert proxy.linked_user_id == draft.user_id

    def test_an_inactive_specialist_is_refused(self, master, proxy) -> None:
        User.objects.filter(pk=master.user_id).update(is_active=False)

        response = _door().post(_url(master.pk), _body(), format="json")

        assert response.status_code == 409
        assert response.json()["error"]["details"]["reason"] == "specialist_not_linkable"


# ─── состояние личности ────────────────────────────────────────────────────


class TestIdentityState:
    def test_an_identity_the_bot_never_showed_is_refused(self, master) -> None:
        """Прокси-строки нет — связывать нечего, и дверь её не создаёт (§148)."""
        response = _door().post(_url(master.pk), _body(), format="json")

        assert response.status_code == 404
        assert response.json()["error"]["details"]["reason"] == "identity_unknown"
        assert not User.objects.filter(username=EXTERNAL).exists()

    def test_an_identity_bound_to_another_account_is_refused(self, salon, master, proxy) -> None:
        other = _specialist(salon, username="idlink-2442-m2")
        proxy.linked_user = other.user
        proxy.save(update_fields=["linked_user"])

        response = _door().post(_url(master.pk), _body(), format="json")

        assert response.status_code == 409
        assert response.json()["error"]["details"]["reason"] == "identity_already_bound"
        proxy.refresh_from_db()
        assert proxy.linked_user_id == other.user_id  # чужая связь не тронута

    def test_a_real_account_under_that_username_is_refused(self, master) -> None:
        User.objects.create_user(username=EXTERNAL, password="x", role="client")

        response = _door().post(_url(master.pk), _body(), format="json")

        assert response.status_code == 409
        assert response.json()["error"]["details"]["reason"] == "identity_not_proxy"


# ─── идемпотентность ───────────────────────────────────────────────────────


class TestRepeatDoesNotDouble:
    def test_the_same_key_and_body_replays(self, master, proxy) -> None:
        first = _door().post(_url(master.pk), _body(), format="json")
        second = _door().post(_url(master.pk), _body(), format="json")

        assert first.status_code == 201
        assert second.status_code == 200
        assert second.json()["data"]["status"] == "replayed"
        assert SpecialistIdentityLinkRequest.objects.count() == 1
        proxy.refresh_from_db()
        assert proxy.linked_user_id == master.user_id

    def test_a_new_key_for_an_already_linked_pair_still_links_once(self, master, proxy) -> None:
        _door().post(_url(master.pk), _body(), format="json")

        again = _door().post(
            _url(master.pk), _body(idempotency_key="idem-2442-0002"), format="json",
        )

        assert again.status_code == 201  # идемпотентно по состоянию, не по ключу
        assert User.objects.filter(username=EXTERNAL, linked_user=master.user).count() == 1

    def test_the_same_key_with_another_body_is_refused(self, salon, master, proxy) -> None:
        other = _specialist(salon, username="idlink-2442-m3")
        _door().post(_url(master.pk), _body(), format="json")

        reused = _door().post(_url(other.pk), _body(), format="json")

        assert reused.status_code == 409
        assert reused.json()["error"]["details"]["reason"] == "idempotency_key_reused"
        assert SpecialistIdentityLinkRequest.objects.count() == 1


# ─── аудит и след ──────────────────────────────────────────────────────────


class TestAuditTrail:
    def test_the_request_row_names_who_asked_and_for_whom(self, master, proxy) -> None:
        _door().post(_url(master.pk), _body(), format="json")

        row = SpecialistIdentityLinkRequest.objects.get()
        assert row.profile_id == master.pk
        assert row.user_id == master.user_id
        assert row.external_user_id == EXTERNAL
        assert row.actor == ACTOR
        assert row.correlation_id == "corr-2442-1"
        assert row.result == "created"

    def test_the_binding_audit_names_this_initiator(self, master, proxy) -> None:
        from analytics.models import AnalyticsEvent

        _door().post(_url(master.pk), _body(), format="json")

        events = list(AnalyticsEvent.objects.all())
        assert events, "binding audit row must exist"  # положительно: аудит пишется
        assert any(
            (e.payload or {}).get("initiator") == INITIATOR_BOT_SPECIALIST_IDENTITY_LINK
            for e in events
        )

    def test_a_refusal_never_logs_the_external_id(self, master, caplog) -> None:
        with _capturing("users.specialist_identity_linking", caplog):
            with pytest.raises(SpecialistIdentityLinkRefused):
                link_specialist_identity(
                    master.pk, EXTERNAL, actor=ACTOR, idempotency_key="idem-2442-0009",
                )

        assert "identity_unknown" in caplog.text  # положительно: отказ записан
        assert EXTERNAL not in caplog.text
