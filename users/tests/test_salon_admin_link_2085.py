"""DRF-2085 (OWNER RULING 18.09, вариант А): POST /api/v1/internal/tenants/<slug>/salon-admins/.

Свежая учётка администратора салона + TUR ``admin`` + связь с MAX-личностью
одной операцией под собственным credential. Узлы — по пунктам контракта
ruling'а; узлы изоляции (``TestTenantIsolation``) названы поимённо в теле
PR — MERGE по ним.

Красное листа: администратор, получивший роль в боте, на салонной
поверхности каталога получает 403 (``IsTenantAdmin``: прокси без TUR) —
после операции тот же заголовок ``X-External-User-ID`` даёт 200.
"""

from __future__ import annotations

import ast
import logging
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from analytics.models import AnalyticsEvent
from tenants.models import Tenant
from users.checks import salon_admin_link_token_check
from users.models import SalonAdminLinkRequest, SpecialistProfile, TenantUserRelationship, User
from users.permissions import IsTenantAdmin
from users.salon_admin_linking import USERNAME_PREFIX, link_salon_admin
from users.services import (
    INITIATOR_BOT_SALON_ADMIN_LINK,
    bind_external_identity_by_operator,
    resolve_external_user,
)

pytestmark = pytest.mark.django_db

LINK = "test-salon-admin-link-2085"
GENERAL = "test-general-bot-token-2085"
IDENTITY = "test-identity-provisioning-2085"
PROVISIONING = "test-tenant-provisioning-2085"

EXTERNAL = "bot:max:2085001"
ACTOR = "django_admin:user=7"
CUSTOMERS = "/api/v1/tenants/me/customers/?q=Анн"


def _url(slug: str) -> str:
    return f"/api/v1/internal/tenants/{slug}/salon-admins/"


@pytest.fixture
def tokens(settings):
    settings.AYLA_SALON_ADMIN_LINK_TOKEN = LINK
    settings.AYLA_INTERNAL_API_TOKEN = GENERAL
    settings.AYLA_IDENTITY_PROVISIONING_TOKEN = IDENTITY
    settings.AYLA_TENANT_PROVISIONING_TOKEN = PROVISIONING


@pytest.fixture(autouse=True)
def _clean_throttle_history():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def salon() -> Tenant:
    return Tenant.objects.create(slug="link-2085-a", name="Салон А")


@pytest.fixture
def other_salon() -> Tenant:
    return Tenant.objects.create(slug="link-2085-b", name="Салон Б")


def _bearer(token: str) -> APIClient:
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return c


@pytest.fixture
def client(tokens) -> APIClient:
    return _bearer(LINK)


def _body(**over) -> dict:
    base = {
        "external_user_id": EXTERNAL,
        "actor": ACTOR,
        "correlation_id": "corr-2085-1",
        "idempotency_key": "idem-2085-0001",
    }
    base.update(over)
    return base


def _salon_surface(tenant: Tenant, external: str = EXTERNAL) -> APIClient:
    """Как ходит бот на салонную поверхность: общий Bearer + X-External-User-ID + X-Tenant."""
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {GENERAL}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external
    c.defaults["HTTP_X_TENANT"] = tenant.slug
    return c


def _fresh_users() -> int:
    return User.objects.filter(username__startswith=USERNAME_PREFIX).count()


def _turs(tenant: Tenant) -> int:
    return TenantUserRelationship.objects.filter(
        tenant=tenant, role=TenantUserRelationship.Role.ADMIN, is_active=True,
    ).count()


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


# ─── красное листа ──────────────────────────────────────────────────────────


class TestTheSheet:
    def test_c1_403_before_the_link_and_200_after_with_the_same_header(self, client, salon):
        before = _salon_surface(salon).get(CUSTOMERS)
        assert before.status_code == 403, before.content  # прокси без TUR — IsTenantAdmin

        r = client.post(_url(salon.slug), _body(), format="json")
        assert r.status_code == 201, r.content

        after = _salon_surface(salon).get(CUSTOMERS)
        assert after.status_code == 200, after.content

    def test_c2_the_response_names_the_fresh_account_and_its_relationship(self, client, salon):
        r = client.post(_url(salon.slug), _body(), format="json")
        assert r.status_code == 201, r.content
        data = r.json()["data"]

        user = User.objects.get(id=data["ayla_user_id"])
        assert user.username.startswith(USERNAME_PREFIX) and not user.username.startswith("bot:")
        assert user.role == "admin" and user.is_proxy is False and user.is_guest is False
        assert user.phone is None and user.first_name == "" and not user.has_usable_password()
        rel = TenantUserRelationship.objects.get(id=data["relationship_id"])
        assert rel.user_id == user.id and rel.tenant_id == salon.id
        assert rel.role == "admin" and rel.is_active and rel.granted_by == "admin"
        assert data == {
            "tenant_id": str(salon.id),
            "slug": salon.slug,
            "ayla_user_id": str(user.id),
            "relationship_id": str(rel.id),
            "role": "admin",
            "status": "created",
        }
        proxy = User.objects.get(username=EXTERNAL)
        assert proxy.is_proxy and proxy.linked_user_id == user.id


# ─── контракт ruling'а ─────────────────────────────────────────────────────


class TestTheContract:
    def test_c3_repeat_with_the_same_key_replays_and_creates_nothing(self, client, salon):
        first = client.post(_url(salon.slug), _body(), format="json")
        assert first.status_code == 201, first.content
        counts = (_fresh_users(), _turs(salon), SalonAdminLinkRequest.objects.count())

        second = client.post(_url(salon.slug), _body(correlation_id="corr-2085-2"), format="json")

        assert second.status_code == 200, second.content
        assert second.json()["data"]["status"] == "replayed"
        assert {k: v for k, v in second.json()["data"].items() if k != "status"} == {
            k: v for k, v in first.json()["data"].items() if k != "status"
        }
        assert (_fresh_users(), _turs(salon), SalonAdminLinkRequest.objects.count()) == counts == (1, 1, 1)

    def test_c4_the_same_key_with_another_body_is_refused_by_name(self, client, salon, other_salon):
        assert client.post(_url(salon.slug), _body(), format="json").status_code == 201

        r = client.post(_url(other_salon.slug), _body(), format="json")

        assert r.status_code == 409, r.content
        assert r.json()["error"]["code"] == "SALON_ADMIN_LINK_REFUSED"
        assert r.json()["error"]["details"]["reason"] == "idempotency_key_reused"
        assert _turs(other_salon) == 0 and _fresh_users() == 1

    def test_c5_unknown_slug_is_404_and_inactive_salon_is_409_nothing_written(self, client, salon):
        missing = client.post(_url("no-such-salon-2085"), _body(), format="json")
        assert missing.status_code == 404
        assert missing.json()["error"]["details"]["reason"] == "tenant_not_found"

        salon.is_active = False
        salon.save(update_fields=["is_active"])
        inactive = client.post(_url(salon.slug), _body(idempotency_key="idem-2085-0002"), format="json")
        assert inactive.status_code == 409
        assert inactive.json()["error"]["details"]["reason"] == "tenant_inactive"

        assert _fresh_users() == 0 and SalonAdminLinkRequest.objects.count() == 0
        assert not User.objects.filter(username=EXTERNAL).exists()  # прокси тоже не заведён

    def test_c6_a_real_account_under_the_external_id_fails_closed(self, client, salon):
        """Пункт 5: совпадение с существующим человеком — не разрешаем, отказ по имени."""
        User.objects.create_user(username=EXTERNAL, password="x", role="client", is_proxy=False)

        r = client.post(_url(salon.slug), _body(), format="json")

        assert r.status_code == 409, r.content
        assert r.json()["error"]["details"]["reason"] == "identity_not_proxy"
        assert _fresh_users() == 0 and _turs(salon) == 0

    def test_c7_an_identity_already_bound_to_a_specialist_fails_closed(self, client, salon):
        """Пункт 4: MAX-личность уже чья-то — здесь не перепривязывают."""
        master = User.objects.create_user(username="m-2085", password="x", role="specialist")
        SpecialistProfile.objects.filter(user=master).update(
            status=SpecialistProfile.ProfileStatus.ACTIVE,
        )
        operator = User.objects.create_user(username="op-2085", password="x", is_staff=True)
        resolve_external_user(EXTERNAL)  # бот уже показывал эту личность каталогу — прокси есть
        bind_external_identity_by_operator(EXTERNAL, master.pk, actor=operator)

        r = client.post(_url(salon.slug), _body(), format="json")

        assert r.status_code == 409, r.content
        assert r.json()["error"]["details"]["reason"] == "identity_already_bound"
        assert _fresh_users() == 0 and _turs(salon) == 0
        assert User.objects.get(username=EXTERNAL).linked_user_id == master.pk

    def test_c8_invalid_external_id_is_refused_by_name(self, client, salon):
        r = client.post(_url(salon.slug), _body(external_user_id="not an id"), format="json")
        assert r.status_code == 400
        assert r.json()["error"]["details"]["reason"] == "invalid_external_user_id"
        assert _fresh_users() == 0

    def test_c9_audit_in_the_catalog_names_actor_tenant_result_and_correlation_id(self, client, salon):
        r = client.post(_url(salon.slug), _body(), format="json")
        assert r.status_code == 201, r.content

        row = SalonAdminLinkRequest.objects.get()
        assert (row.tenant_id, row.external_user_id, row.actor, row.correlation_id, row.result) == (
            salon.id, EXTERNAL, ACTOR, "corr-2085-1", "created",
        )
        assert str(row.user_id) == r.json()["data"]["ayla_user_id"]
        assert row.idempotency_key == "idem-2085-0001" and row.created_at is not None

        events = list(AnalyticsEvent.objects.filter(payload__initiator=INITIATOR_BOT_SALON_ADMIN_LINK))
        assert len(events) == 1
        assert events[0].payload["result"] == "created" and events[0].payload["request_id"] == "corr-2085-1"
        assert events[0].payload["target_user_id"] == r.json()["data"]["ayla_user_id"]
        assert "phone" not in events[0].payload and "initiator_user_id" not in events[0].payload

    def test_c10_the_external_id_is_not_logged_on_success_or_refusal(self, client, salon, caplog):
        with _capturing("users.salon_admin_linking", caplog):
            assert client.post(_url(salon.slug), _body(), format="json").status_code == 201
            assert client.post(
                _url(salon.slug), _body(idempotency_key="idem-2085-0003"), format="json",
            ).status_code == 409  # already bound → refused, logged
        text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "users.salon_admin_link.created" in text and "users.salon_admin_link.refused" in text
        assert EXTERNAL not in text and "2085001" not in text

    def test_c11_readback_failure_is_named_not_silent(self, client, salon, monkeypatch):
        """Пункт 9 — положительный сторож: SUCCESS только после readback."""
        import users.salon_admin_linking as mod

        monkeypatch.setattr(mod, "_readback", lambda *a, **k: False)
        r = client.post(_url(salon.slug), _body(), format="json")
        assert r.status_code == 500, r.content
        assert r.json()["error"]["details"]["reason"] == "readback_failed"

    def test_c12_rate_limit_is_enforced(self, client, salon, monkeypatch):
        monkeypatch.setitem(SimpleRateThrottle.THROTTLE_RATES, "salon_admin_link", "2/min")
        codes = [
            client.post(
                _url("no-such-salon-2085"), _body(idempotency_key=f"idem-2085-rl-{i}"), format="json",
            ).status_code
            for i in range(3)
        ]
        assert codes == [404, 404, 429]

    def test_c13_the_scope_is_wired_to_the_bucket_settings_declare(self, settings):
        from users.internal_salon_admin_api import InternalSalonAdminLinkView

        assert InternalSalonAdminLinkView.throttle_scope == "salon_admin_link"
        assert "salon_admin_link" in settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]


# ─── изоляция — MERGE по этим узлам ─────────────────────────────────────────


class TestTenantIsolation:
    def test_iso1_the_linked_admin_acts_only_in_the_named_salon(self, client, salon, other_salon):
        assert client.post(_url(salon.slug), _body(), format="json").status_code == 201

        assert _salon_surface(salon).get(CUSTOMERS).status_code == 200
        assert _salon_surface(other_salon).get(CUSTOMERS).status_code == 403

    def test_iso2_a_second_salon_cannot_claim_a_bound_identity(self, client, salon, other_salon):
        assert client.post(_url(salon.slug), _body(), format="json").status_code == 201

        r = client.post(_url(other_salon.slug), _body(idempotency_key="idem-2085-b-0001"), format="json")

        assert r.status_code == 409, r.content
        assert r.json()["error"]["details"]["reason"] == "identity_already_bound"
        assert _turs(other_salon) == 0 and _turs(salon) == 1 and _fresh_users() == 1

    def test_iso3_the_link_credential_opens_no_other_route(self, client, salon):
        ensure = client.post(
            "/api/v1/internal/tenants/", {"slug": "x-2085", "name": "X"}, format="json",
        )
        bind = client.post(
            "/api/v1/internal/users/bind-external/",
            {"external_user_id": EXTERNAL, "ayla_user_id": str(uuid.uuid4())},
            format="json",
        )
        identity = client.get("/api/v1/internal/me/identity/", HTTP_X_EXTERNAL_USER_ID=EXTERNAL)

        assert (ensure.status_code, bind.status_code, identity.status_code) == (403, 403, 403)
        assert not Tenant.all_objects.filter(slug="x-2085").exists()
        assert not User.objects.filter(username=EXTERNAL).exists()

    def test_iso4_the_three_sibling_bearers_do_not_open_the_link(self, tokens, salon):
        for token in (GENERAL, IDENTITY, PROVISIONING):
            r = _bearer(token).post(_url(salon.slug), _body(), format="json")
            assert r.status_code == 403, (token, r.content)
        assert _fresh_users() == 0 and _turs(salon) == 0
        # положительная пара: тот же адрес, тот же body, свой credential — открыт
        assert _bearer(LINK).post(_url(salon.slug), _body(), format="json").status_code == 201

    def test_iso5_equal_secrets_close_the_route_and_fail_the_boot_check(self, settings, salon):
        settings.AYLA_SALON_ADMIN_LINK_TOKEN = GENERAL
        settings.AYLA_INTERNAL_API_TOKEN = GENERAL
        settings.AYLA_IDENTITY_PROVISIONING_TOKEN = IDENTITY
        settings.AYLA_TENANT_PROVISIONING_TOKEN = PROVISIONING

        assert _bearer(GENERAL).post(_url(salon.slug), _body(), format="json").status_code == 403
        assert [e.id for e in salon_admin_link_token_check(None)] == ["users.E004"]

        settings.AYLA_SALON_ADMIN_LINK_TOKEN = LINK
        assert salon_admin_link_token_check(None) == []

    def test_iso6_empty_link_secret_closes_the_route(self, settings, salon):
        settings.AYLA_SALON_ADMIN_LINK_TOKEN = ""
        settings.AYLA_INTERNAL_API_TOKEN = GENERAL
        assert _bearer("").post(_url(salon.slug), _body(), format="json").status_code == 403
        assert _bearer(GENERAL).post(_url(salon.slug), _body(), format="json").status_code == 403

    def test_iso7_the_fresh_account_is_not_platform_staff_and_holds_one_relationship(self, client, salon):
        assert client.post(_url(salon.slug), _body(), format="json").status_code == 201

        user = User.objects.get(username__startswith=USERNAME_PREFIX)
        assert (user.is_staff, user.is_superuser, user.is_platform_admin) == (False, False, False)
        rels = list(TenantUserRelationship.objects.filter(user=user))
        assert len(rels) == 1 and rels[0].tenant_id == salon.id and rels[0].role == "admin"

    def test_iso8_readback_is_the_salon_surface_permission(self, client, salon, other_salon):
        assert client.post(_url(salon.slug), _body(), format="json").status_code == 201

        resolved = resolve_external_user(EXTERNAL)
        assert resolved.is_proxy is False and resolved.username.startswith(USERNAME_PREFIX)
        assert IsTenantAdmin().has_permission(SimpleNamespace(user=resolved, tenant=salon), None) is True
        assert IsTenantAdmin().has_permission(SimpleNamespace(user=resolved, tenant=other_salon), None) is False

    def test_iso9_two_operators_on_one_identity_yield_one_admin(self, tokens, salon, other_salon):
        """Гонка двух ключей на одну личность — второй видит linked_user под блокировкой.
        Последовательно (одна БД-сессия в тесте), но тем же путём."""
        first = link_salon_admin(
            tenant_slug=salon.slug, external_user_id=EXTERNAL, actor=ACTOR, idempotency_key="k-2085-1",
        )
        with pytest.raises(Exception) as exc:
            link_salon_admin(
                tenant_slug=other_salon.slug, external_user_id=EXTERNAL, actor=ACTOR, idempotency_key="k-2085-2",
            )
        assert getattr(exc.value, "reason", None) == "identity_already_bound"
        assert first.created and _fresh_users() == 1 and _turs(other_salon) == 0


# ─── удаление аккаунта ──────────────────────────────────────────────────────


class _BotOk:
    def confirm(self, **kw):
        from users.deletion_executor import BotConfirmation

        return BotConfirmation(True, {"all_ok": True, "steps": ["memory_delete"], "flag_cleared": True})


class TestAccountDeletion:
    def test_d1_deleting_the_admin_account_keeps_the_audit_row_without_the_person(self, client, salon):
        """Реестр удаления: users.SalonAdminLinkRequest.user — ANONYMISE. Строка остаётся
        (след операции оператора), user → NULL, MAX-идентификатор переименован вместе с прокси."""
        from users.deletion_executor import execute, undecided_pointers
        from users.deletion_requests import ensure_deletion_request

        assert undecided_pointers() == {}  # новая таблица решена, пайплайн не закрыт
        r = client.post(_url(salon.slug), _body(), format="json")
        assert r.status_code == 201, r.content
        admin_user = User.objects.get(id=r.json()["data"]["ayla_user_id"])
        proxy = User.objects.get(username=EXTERNAL)
        row = SalonAdminLinkRequest.objects.get()
        assert row.user_id == admin_user.id and row.external_user_id == EXTERNAL

        out = execute(ensure_deletion_request(admin_user, initiator="bot").request, bot_client=_BotOk())

        assert out.completed, out.failure_reason
        assert out.steps["anonymised"]["users.SalonAdminLinkRequest.user"] == 1
        assert out.steps["anonymised"]["users.SalonAdminLinkRequest.external_user_id"] == 1
        row.refresh_from_db()
        assert row.user_id is None
        assert row.external_user_id == f"deleted:{proxy.pk}" and "2085001" not in row.external_user_id
        assert (row.tenant_id, row.actor, row.correlation_id, row.result) == (
            salon.id, ACTOR, "corr-2085-1", "created",
        )
        proxy.refresh_from_db()
        assert proxy.username == f"deleted:{proxy.pk}" and proxy.linked_user_id is None
        assert not TenantUserRelationship.objects.filter(user=admin_user, is_active=True).exists()

    def test_d2_a_row_left_pointing_at_the_person_is_incomplete_and_rolls_back(self, client, salon, monkeypatch):
        """Положительный сторож полноты: шаг «забыл» снять указатель — FAILED, не COMPLETED."""
        from django.db.models import QuerySet

        from users.deletion_executor import execute
        from users.deletion_requests import ensure_deletion_request
        from users.models import DeletionRequest

        assert client.post(_url(salon.slug), _body(), format="json").status_code == 201
        admin_user = User.objects.get(username__startswith=USERNAME_PREFIX)
        real_update = QuerySet.update

        def _skip_for_link_rows(self, **kwargs):
            if self.model is SalonAdminLinkRequest:
                return 0
            return real_update(self, **kwargs)

        monkeypatch.setattr(QuerySet, "update", _skip_for_link_rows)
        req = ensure_deletion_request(admin_user, initiator="bot").request
        out = execute(req, bot_client=_BotOk())

        req.refresh_from_db()
        assert out.status == req.status == DeletionRequest.Status.FAILED
        assert "SalonAdminLinkRequest" in req.failure_reason
        assert SalonAdminLinkRequest.objects.get().user_id == admin_user.id  # откат: ничего не стёрто
        admin_user.refresh_from_db()
        assert admin_user.username.startswith(USERNAME_PREFIX)


# ─── стражи ─────────────────────────────────────────────────────────────────


class TestTheGuards:
    def test_g1_the_secret_is_read_in_exactly_three_places(self):
        """Пункт 11: credential не в логах и не в клиентском коде — по AST, не по grep."""
        root = Path(__file__).resolve().parents[2]
        readers: set[str] = set()
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if "/tests/" in f"/{rel}" or "/migrations/" in rel or rel.startswith((".venv", "venv")):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == "AYLA_SALON_ADMIN_LINK_TOKEN":
                    readers.add(rel)
                elif isinstance(node, ast.Attribute) and node.attr == "AYLA_SALON_ADMIN_LINK_TOKEN":
                    readers.add(rel)
        assert readers == {
            "djangoProject/settings/base.py",
            "users/permissions.py",
            "users/checks.py",
        }, readers

    def test_g2_the_view_carries_only_the_link_permission(self):
        from users.internal_salon_admin_api import InternalSalonAdminLinkView
        from users.permissions import IsSalonAdminLinkBearer

        assert InternalSalonAdminLinkView.permission_classes == [IsSalonAdminLinkBearer]
        assert InternalSalonAdminLinkView.authentication_classes == []
