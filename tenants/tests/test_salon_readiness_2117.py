"""DRF-2117 — салонная готовность поимённо: GET /api/v1/internal/salons/<slug>/readiness/.

Красное листа: на dev маршрута нет — владелец салона не может узнать, кто из
мастеров мешает записи; готовность есть только у соло-мастера
(``users.publication``). Здесь — половина ответа, которую знает каталог
(докстринг ``tenants.salon_readiness``).

Отрицательные тесты субъектного сторожа (``TestSubject``) названы в переписи
``users/tests/test_internal_subject_authorization.py::SALON_ROUTES_TESTED_ELSEWHERE``:
чужой салон → 404, provisioning-credential / без заголовка / неверный токен
→ отказ, неизвестный актор — строк не создано, неактивный актор → отказ.
Каждый отказ — рядом с положительной половиной.
"""

from __future__ import annotations

import logging
from datetime import time, timedelta
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from appointments.models import SpecialistWorkingHours
from services.models import SalonService, ServiceCategory, SpecialistService
from tenants import salon_readiness as sr
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User

pytestmark = pytest.mark.django_db

RUNTIME = "test-runtime-token-2117"  # noqa: S105
PROVISIONING = "test-provisioning-token-2117"  # noqa: S105

OWNER = "bot:max:2117001"


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME
    settings.AYLA_IDENTITY_PROVISIONING_TOKEN = PROVISIONING


def _url(slug: str) -> str:
    return f"/api/v1/internal/salons/{slug}/readiness/"


def _client(*, bearer: str | None = RUNTIME, actor: str | None = OWNER) -> APIClient:
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if actor is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = actor
    return c


def _admin_of(tenant: Tenant, external_id: str, *, nick: str) -> User:
    """Живой аккаунт администратора + прокси MAX, привязанный к нему, + TUR admin.

    Собрано руками, как в ``test_internal_subject_authorization``: смена
    политики привязки не должна молча менять то, что этот тест считает
    администратором.
    """
    real = User.objects.create_user(
        username=nick, password="x", role="client", phone=f"+7999211{nick[-4:]}",  # noqa: S106
    )
    User.objects.create(
        username=external_id, role="client", is_proxy=True, is_guest=False, linked_user=real,
    )
    TenantUserRelationship.objects.create(
        user=real, tenant=tenant, role=TenantUserRelationship.Role.ADMIN, is_active=True,
    )
    return real


@pytest.fixture
def salon() -> Tenant:
    return Tenant.objects.create(slug="ready-2117-a", name="Формула тела")


@pytest.fixture
def other_salon() -> Tenant:
    return Tenant.objects.create(slug="ready-2117-b", name="Салон Б")


@pytest.fixture
def owner(salon) -> User:
    return _admin_of(salon, OWNER, nick="owner2117")


_seq = iter(range(1000, 9999))


def _master(tenant: Tenant, name: str) -> SpecialistProfile:
    n = next(_seq)
    u = User.objects.create_user(
        username=f"m2117-{n}", password="x", role="specialist", phone=f"+7999217{n}",  # noqa: S106
    )
    sp = SpecialistProfile.objects.get(user=u)
    sp.tenant = tenant
    sp.display_name = name
    sp.status = SpecialistProfile.ProfileStatus.ACTIVE
    sp.is_available = True
    sp.is_booking_enabled = True
    sp.timezone = "Europe/Moscow"
    sp.save()
    return sp


def _hours(sp: SpecialistProfile, *, days=range(7)) -> None:
    for d in days:
        SpecialistWorkingHours.objects.create(
            specialist=sp, day_of_week=d, is_working_day=True,
            start_time=time(10, 0), end_time=time(19, 0),
        )


def _service(sp: SpecialistProfile, *, price: str = "1500", active: bool = True) -> SpecialistService:
    n = next(_seq)
    category = ServiceCategory.objects.create(name=f"Cat {n}", slug=f"cat-2117-{n}")
    salon = SalonService.objects.create(
        tenant=sp.tenant, category=category, name=f"Услуга {n}", duration_minutes=60,
        base_price=None, is_active=True, requires_health_check=False,
    )
    return SpecialistService.objects.create(
        salon_service=salon, specialist=sp, duration_minutes=60,
        price=Decimal(price), buffer_after_minutes=0, is_active=active,
    )


def _link(sp: SpecialistProfile, external_id: str) -> None:
    User.objects.create(
        username=external_id, role="client", is_proxy=True, is_guest=False, linked_user=sp.user,
    )


def _ready_master(tenant: Tenant, name: str, external_id: str) -> SpecialistProfile:
    sp = _master(tenant, name)
    _hours(sp)
    _service(sp)
    _link(sp, external_id)
    return sp


def _codes(body: dict) -> list[tuple[str, str]]:
    return [(p["master"]["name"], p["code"]) for p in body["data"]["problems"]]


# ─── красное листа: список поимённо ─────────────────────────────────────────


class TestTheSheet:
    def test_named_problems_per_master(self, salon, owner):
        anna = _master(salon, "Анна Иванова")          # без графика
        _service(anna)
        _link(anna, "bot:max:2117101")
        ivan = _master(salon, "Иван")                  # без услуг
        _hours(ivan)
        _link(ivan, "bot:max:2117102")
        maria = _master(salon, "Мария")                # без связи
        _hours(maria)
        _service(maria)

        resp = _client().get(_url(salon.slug))
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["data"]["salon"] == {"slug": salon.slug, "name": "Формула тела"}
        assert body["data"]["ready"] is False
        assert _codes(body) == [
            ("Анна", sr.SCHEDULE_MISSING),
            ("Иван", sr.SERVICES_MISSING),
            ("Мария", sr.IDENTITY_NOT_LINKED),
        ]
        texts = [p["text"] for p in body["data"]["problems"]]
        assert texts == [
            "Анна — не настроен график",
            "Иван — не назначены услуги",
            "Мария — не привязана личность MAX",
        ]
        # Первое имя — без склонений: предел назван в докстринге модуля.
        by_name = {m["name"]: m for m in body["data"]["masters"]}
        assert by_name["Анна"]["checks"] == {
            "publication": "ok", "schedule": "problem", "services": "ok",
            "catalog_link": "ok", "slots": "skipped",
        }
        assert body["data"]["horizon_days"] == 7
        assert body["data"]["limits"] == list(sr.LIMITS)

    def test_ready_only_with_an_empty_list(self, salon, owner):
        _ready_master(salon, "Анна", "bot:max:2117111")
        resp = _client().get(_url(salon.slug))
        assert resp.status_code == 200, resp.content
        body = resp.json()["data"]
        assert body["problems"] == []
        assert body["ready"] is True
        assert body["masters"][0]["checks"] == {
            "publication": "ok", "schedule": "ok", "services": "ok",
            "catalog_link": "ok", "slots": "ok",
        }

    def test_fixing_one_master_shortens_the_list(self, salon, owner):
        """«Как проверим» листа: настроить график одному → список стал короче."""
        anna = _master(salon, "Анна")
        _service(anna)
        _link(anna, "bot:max:2117121")
        ivan = _master(salon, "Иван")
        _service(ivan)
        _link(ivan, "bot:max:2117122")
        assert len(_codes(_client().get(_url(salon.slug)).json())) == 2
        _hours(anna)
        after = _client().get(_url(salon.slug)).json()
        assert _codes(after) == [("Иван", sr.SCHEDULE_MISSING)]

    def test_salon_without_masters_is_not_ready_and_says_why(self, salon, owner):
        """Три мастера с выключенными аккаунтами = ни одного: «готов» здесь — ложь."""
        body = _client().get(_url(salon.slug)).json()["data"]
        assert body["masters"] == []
        assert body["ready"] is False
        assert body["problems"] == [
            {"master": None, "code": sr.NO_MASTERS, "text": "В салоне нет ни одного мастера"},
        ]


# ─── публикация, сокрытие, пауза ────────────────────────────────────────────


class TestPublication:
    def test_draft_profile_is_not_published(self, salon, owner):
        sp = _ready_master(salon, "Анна", "bot:max:2117201")
        sp.status = SpecialistProfile.ProfileStatus.DRAFT
        sp.save(update_fields=["status"])
        assert _codes(_client().get(_url(salon.slug)).json()) == [("Анна", sr.NOT_PUBLISHED)]

    def test_hidden_profile(self, salon, owner):
        sp = _ready_master(salon, "Анна", "bot:max:2117202")
        sp.is_available = False
        sp.save(update_fields=["is_available"])
        assert _codes(_client().get(_url(salon.slug)).json()) == [("Анна", sr.HIDDEN_FROM_CATALOG)]

    def test_booking_paused(self, salon, owner):
        sp = _ready_master(salon, "Анна", "bot:max:2117203")
        sp.is_booking_enabled = False
        sp.save(update_fields=["is_booking_enabled"])
        assert _codes(_client().get(_url(salon.slug)).json()) == [("Анна", sr.BOOKING_PAUSED)]


# ─── услуги — единственная форма «продаётся» (DRF-1962) ─────────────────────


class TestServices:
    def test_zero_price_edge_does_not_count(self, salon, owner):
        sp = _master(salon, "Иван")
        _hours(sp)
        _link(sp, "bot:max:2117301")
        _service(sp, price="0")
        assert _codes(_client().get(_url(salon.slug)).json()) == [("Иван", sr.SERVICES_MISSING)]

    def test_inactive_edge_does_not_count(self, salon, owner):
        sp = _master(salon, "Иван")
        _hours(sp)
        _link(sp, "bot:max:2117302")
        _service(sp, active=False)
        assert _codes(_client().get(_url(salon.slug)).json()) == [("Иван", sr.SERVICES_MISSING)]

    def test_edge_from_another_tenant_does_not_count(self, salon, other_salon, owner):
        """Тот же предикат, что у пути записи: услуга должна быть этого тенанта."""
        sp = _master(salon, "Иван")
        _hours(sp)
        _link(sp, "bot:max:2117304")
        edge = _service(sp)
        SalonService.objects.filter(pk=edge.salon_service_id).update(tenant=other_salon)
        assert _codes(_client().get(_url(salon.slug)).json()) == [("Иван", sr.SERVICES_MISSING)]

    def test_edge_without_any_duration_is_named_not_unknown(self, salon, owner):
        """Без длительности вычислитель упал бы — это починяемый факт, не «не удалось»."""
        sp = _master(salon, "Иван")
        _hours(sp)
        _link(sp, "bot:max:2117305")
        edge = _service(sp)
        SalonService.objects.filter(pk=edge.salon_service_id).update(duration_minutes=None)
        SpecialistService.objects.filter(pk=edge.pk).update(duration_minutes=None)
        body = _client().get(_url(salon.slug)).json()["data"]
        assert [(p["master"]["name"], p["code"]) for p in body["problems"]] == [
            ("Иван", sr.SERVICE_DURATION_MISSING),
        ]
        assert body["masters"][0]["checks"]["services"] == "problem"
        assert body["masters"][0]["checks"]["slots"] == "skipped"

    def test_working_day_without_times_is_not_a_schedule(self, salon, owner):
        sp = _master(salon, "Анна")
        _service(sp)
        _link(sp, "bot:max:2117303")
        SpecialistWorkingHours.objects.create(
            specialist=sp, day_of_week=0, is_working_day=True, start_time=None, end_time=None,
        )
        assert _codes(_client().get(_url(salon.slug)).json()) == [("Анна", sr.SCHEDULE_MISSING)]


# ─── слоты: есть/нет, skipped, UNKNOWN = проблема ───────────────────────────


class TestSlots:
    def test_no_free_window_in_seven_days(self, salon, owner):
        """График и услуги есть, но отгул закрывает всю ближайшую неделю:
        это уже не «не настроен график», это «нет свободных окон»."""
        from datetime import datetime, timezone

        from appointments.models import SpecialistTimeOff

        sp = _ready_master(salon, "Анна", "bot:max:2117401")
        now = datetime.now(tz=timezone.utc)
        SpecialistTimeOff.objects.create(
            specialist=sp, start_at=now - timedelta(days=1), end_at=now + timedelta(days=9),
        )
        body = _client().get(_url(salon.slug)).json()
        assert _codes(body) == [("Анна", sr.NO_FREE_SLOTS)]
        assert body["data"]["masters"][0]["checks"]["slots"] == "problem"
        assert body["data"]["problems"][0]["text"] == (
            "Анна — нет свободных окон на ближайшие 7 дней"
        )

    def test_slots_are_skipped_until_schedule_and_services_exist(self, salon, owner):
        sp = _master(salon, "Анна")
        _link(sp, "bot:max:2117402")
        body = _client().get(_url(salon.slug)).json()["data"]
        assert body["masters"][0]["checks"]["slots"] == "skipped"
        assert {c for _, c in _codes({"data": body})} == {sr.SCHEDULE_MISSING, sr.SERVICES_MISSING}

    def test_calculator_failure_is_a_problem_not_an_ok(self, salon, owner, monkeypatch, caplog):
        _ready_master(salon, "Анна", "bot:max:2117403")

        def boom(*_a, **_k):
            raise RuntimeError("availability exploded")

        monkeypatch.setattr(sr, "_has_free_slot", boom)
        with caplog.at_level(logging.WARNING, logger="tenants.salon_readiness"):
            body = _client().get(_url(salon.slug)).json()["data"]
        assert body["ready"] is False
        assert body["masters"][0]["checks"]["slots"] == "unknown"
        assert body["problems"] == [{
            "master": {"id": body["masters"][0]["id"], "name": "Анна"},
            "code": sr.SLOTS_UNKNOWN,
            "text": "Анна — не удалось проверить свободные окна",
        }]
        # Класс исключения — в лог (поправка главного окна), не только «не удалось».
        assert any(
            "salon_readiness.slots_unknown" in r.getMessage() and "RuntimeError" in r.getMessage()
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]


# ─── кто считается мастером салона ──────────────────────────────────────────


class TestRoster:
    def test_foreign_salon_masters_are_not_listed(self, salon, other_salon, owner):
        _ready_master(salon, "Анна", "bot:max:2117501")
        _master(other_salon, "Чужая")
        names = [m["name"] for m in _client().get(_url(salon.slug)).json()["data"]["masters"]]
        assert names == ["Анна"]

    def test_deactivated_account_is_not_listed(self, salon, owner):
        sp = _master(salon, "Ушла")
        sp.user.is_active = False
        sp.user.save(update_fields=["is_active"])
        _ready_master(salon, "Анна", "bot:max:2117502")
        names = [m["name"] for m in _client().get(_url(salon.slug)).json()["data"]["masters"]]
        assert names == ["Анна"]


# ─── субъектный сторож: чужой салон → 404, остальное — отказ ────────────────


class TestSubject:
    def test_own_salon_is_reachable(self, salon, owner):
        assert _client().get(_url(salon.slug)).status_code == 200

    def test_foreign_salon_is_404_not_403(self, salon, other_salon, owner):
        """Slug чужого тенанта не подтверждается — как у GUARDED_OTHERWISE_SPECIALIST."""
        _ready_master(other_salon, "Чужая", "bot:max:2117601")
        resp = _client().get(_url(other_salon.slug))
        assert resp.status_code == 404, resp.content
        assert "Чужая" not in resp.content.decode()

    def test_unknown_slug_is_404(self, salon, owner):
        assert _client().get(_url("no-such-salon")).status_code == 404

    def test_inactive_salon_is_404(self, salon, owner):
        salon.is_active = False
        salon.save(update_fields=["is_active"])
        assert _client().get(_url(salon.slug)).status_code == 404

    def test_revoked_tur_is_404(self, salon, owner):
        TenantUserRelationship.objects.filter(user=owner, tenant=salon).update(is_active=False)
        assert _client().get(_url(salon.slug)).status_code == 404

    def test_customer_tur_is_not_an_admin(self, salon, owner):
        TenantUserRelationship.objects.filter(user=owner, tenant=salon).update(
            role=TenantUserRelationship.Role.CUSTOMER,
        )
        assert _client().get(_url(salon.slug)).status_code == 404

    def test_provisioning_credential_is_refused(self, salon, owner):
        assert _client(bearer=PROVISIONING).get(_url(salon.slug)).status_code in (401, 403)

    def test_invalid_token_is_refused(self, salon, owner):
        assert _client(bearer="nope").get(_url(salon.slug)).status_code in (401, 403)

    def test_unset_runtime_token_fails_closed(self, settings, salon, owner):
        settings.AYLA_INTERNAL_API_TOKEN = ""
        assert _client(bearer="").get(_url(salon.slug)).status_code in (401, 403)

    def test_no_header_means_no_access(self, salon, owner):
        resp = _client(actor=None).get(_url(salon.slug))
        assert resp.status_code == 403
        assert "X-External-User-ID" in resp.json()["error"]["message"]

    def test_unknown_actor_is_refused_without_provisioning_a_row(self, salon, owner):
        before = User.objects.count()
        resp = _client(actor="bot:max:404404").get(_url(salon.slug))
        assert resp.status_code == 403
        assert User.objects.count() == before
        assert not User.objects.filter(username="bot:max:404404").exists()

    def test_inactive_actor_is_refused(self, salon, owner):
        owner.is_active = False
        owner.save(update_fields=["is_active"])
        assert _client().get(_url(salon.slug)).status_code == 403

    def test_unlinked_proxy_administers_nothing(self, salon, owner):
        User.objects.create(
            username="bot:max:2117777", role="client", is_proxy=True, is_guest=False,
        )
        assert _client(actor="bot:max:2117777").get(_url(salon.slug)).status_code == 404


# ─── контракт: ключи ответа зафиксированы для потребителей (бот, ayla-85) ───


class TestContract:
    def test_response_shape(self, salon, owner):
        _ready_master(salon, "Анна", "bot:max:2117801")
        body = _client().get(_url(salon.slug)).json()["data"]
        assert set(body) == {
            "salon", "ready", "checked_at", "horizon_days", "masters", "problems", "limits",
        }
        master = body["masters"][0]
        assert set(master) == {"id", "user_id", "name", "checks", "problems"}
        assert set(master["checks"]) == set(sr.CHECKS)

    def test_every_code_has_a_text_and_a_check(self):
        master_codes = {
            sr.NOT_PUBLISHED, sr.HIDDEN_FROM_CATALOG, sr.BOOKING_PAUSED, sr.SCHEDULE_MISSING,
            sr.SERVICES_MISSING, sr.SERVICE_DURATION_MISSING, sr.IDENTITY_NOT_LINKED,
            sr.NO_FREE_SLOTS, sr.SLOTS_UNKNOWN,
        }
        assert set(sr.TEXTS) == master_codes | {sr.NO_MASTERS}
        assert set(sr.CODE_CHECK) == master_codes
        assert set(sr.CODE_CHECK.values()) == set(sr.CHECKS)
        for code in master_codes:
            assert "{name}" in sr.TEXTS[code], code
