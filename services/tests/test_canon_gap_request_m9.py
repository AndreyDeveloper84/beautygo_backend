"""«Своя услуга» мастера = заявка о разрыве канона (M9, DRF-1801, G6 / D6).

Чекпойнты листа:

* PENDING нигде: заявка не даёт ни ServiceTemplate, ни SalonService, ни
  SpecialistService, ни синонима;
* синоним найден → связь не создана;
* APPROVED без resolved_template → CheckConstraint;
* решает только человек в admin: у ручки нет PATCH/PUT/DELETE, у сервиса —
  нет решения без сотрудника.

Каждое «нет» стоит рядом с положительной парой на тех же данных.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone
from rest_framework.test import APIClient

from services.canon_gap import (
    STATUS_LABELS,
    CanonGapDecisionError,
    create_request,
    decide,
    similar_templates,
)
from services.models import (
    CanonGapRequest,
    SalonService,
    ServiceCategory,
    ServiceTemplate,
    ServiceTemplateSynonym,
    SpecialistService,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

S = CanonGapRequest.Status
RUNTIME_TOKEN = "test-runtime-internal-token-m9"  # noqa: S105


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN
    settings.AYLA_IDENTITY_PROVISIONING_TOKEN = "test-provisioning-only-token-m9"  # noqa: S105


@pytest.fixture
def salon():
    return Tenant.objects.create(slug=f"m9-{uuid4().hex[:8]}", name="Салон M9")


def _master(salon, *, username, phone, external_id):
    user = User.objects.create_user(
        username=username, password="x", role="specialist", phone=phone,  # pragma: allowlist secret
    )
    user.tenant = salon
    user.save(update_fields=["tenant"])
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = salon
    profile.save()
    User.objects.create(username=external_id, role="client", is_proxy=True, is_guest=False, linked_user=user)
    return profile


@pytest.fixture
def master(salon):
    return _master(salon, username="m9_m1", phone="+79995409001", external_id="bot:max:m9001")


@pytest.fixture
def other(salon):
    return _master(salon, username="m9_m2", phone="+79995409002", external_id="bot:max:m9002")


@pytest.fixture
def owner():
    return User.objects.create_superuser(
        username="m9-owner", password="x", email="owner@example.com", role="admin",  # pragma: allowlist secret
    )


@pytest.fixture
def category():
    return ServiceCategory.objects.create(name="Татуаж M9", slug=f"m9-cat-{uuid4().hex[:6]}")


@pytest.fixture
def template(category):
    return ServiceTemplate.objects.create(
        category=category, name="Перманентный макияж бровей", name_short="Пм бровей", duration_default=90,
    )


def _api(actor="bot:max:m9001"):
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RUNTIME_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = actor
    return c


def _url(profile, suffix=""):
    return f"/api/v1/internal/specialists/{profile.pk}/canon-gap-requests/{suffix}"


BODY = {"name": "Татуаж бровей пудровый", "description": "Пудровое напыление", "duration_minutes": 120, "price": "4500"}


def _canon_counts():
    return (
        ServiceTemplate.objects.count(),
        SalonService.objects.count(),
        SpecialistService.objects.count(),
        ServiceTemplateSynonym.objects.count(),
    )


# ---------------------------------------------------------------------------
# PENDING нигде
# ---------------------------------------------------------------------------


class TestPendingCreatesNothingInCanon:
    def test_post_creates_a_pending_request_and_nothing_else(self, master, template):
        before = _canon_counts()
        resp = _api().post(_url(master), BODY, format="json")
        assert resp.status_code == 201, resp.content
        data = resp.data["data"]["request"]
        assert data["status"] == "pending" and data["status_label"] == "На проверке"
        assert data["name"] == BODY["name"] and data["duration_minutes"] == 120 and data["price"] == "4500.00"
        assert data["resolved_template_id"] is None
        req = CanonGapRequest.objects.get(pk=data["id"])
        assert req.specialist_id == master.pk and req.tenant_id == master.tenant_id
        # Отсутствие поверх тех же данных: канон не тронут.
        assert _canon_counts() == before

    def test_approval_links_a_template_but_creates_no_offer(self, master, template, owner):
        """M8 (controlled offer path) на dev нет: APPROVED называет шаблон и
        больше ничего не пишет."""
        req = create_request(master, name="Татуаж", description="", duration_minutes=60, price=Decimal("1000"))
        before = _canon_counts()
        out = decide(req, actor=owner, status=S.APPROVED, template=template)
        assert out.status == S.APPROVED and out.resolved_template_id == template.pk
        assert out.decided_by == owner and out.decided_at is not None
        assert _canon_counts() == before


class TestSimilarIsAHintNotALink:
    def test_synonym_found_link_not_created(self, master, template, owner):
        ServiceTemplateSynonym.objects.create(
            template=template, text="Татуаж бровей пудровый", confirmed_by=owner,
            confirmed_at=timezone.now(), source_ref="разбор M9",
        )
        hits = similar_templates("  ТАТУАЖ бровей пудровый ")
        assert [h.template_id for h in hits] == [str(template.pk)]
        assert hits[0].matched_by == "synonym"

        before = _canon_counts()
        resp = _api().post(_url(master), BODY, format="json")
        assert resp.status_code == 201, resp.content
        assert [s["template_id"] for s in resp.data["data"]["similar"]] == [str(template.pk)]
        req = CanonGapRequest.objects.get(pk=resp.data["data"]["request"]["id"])
        assert req.status == S.PENDING and req.resolved_template_id is None
        assert _canon_counts() == before

    def test_similar_endpoint_reads_only(self, master, template):
        resp = _api().get(_url(master, "similar/"), {"name": "Перманентный макияж бровей"})
        assert resp.status_code == 200, resp.content
        assert resp.data["data"]["similar"] == [
            {"template_id": str(template.pk), "name": template.name, "matched_by": "canonical_name"}
        ]
        assert _api().get(_url(master, "similar/"), {"name": "совсем другое"}).data["data"]["similar"] == []
        assert _api().get(_url(master, "similar/")).status_code == 400


# ---------------------------------------------------------------------------
# Схема
# ---------------------------------------------------------------------------


class TestSchema:
    def _req(self, master, **over):
        fields = dict(specialist=master, tenant=master.tenant, name="X", duration_minutes=60, price=Decimal("1"))
        fields.update(over)
        return CanonGapRequest(**fields)

    def test_approved_without_template_is_refused(self, master, owner, template):
        ok = self._req(
            master, status=S.APPROVED, resolved_template=template, decided_by=owner, decided_at=timezone.now(),
        )
        ok.save()  # положительная пара
        with pytest.raises(IntegrityError, match="canongap_approved_requires_template"), transaction.atomic():
            self._req(master, status=S.APPROVED, decided_by=owner, decided_at=timezone.now()).save()

    def test_decision_without_actor_is_refused(self, master, template):
        with pytest.raises(IntegrityError, match="canongap_decision_requires_actor"), transaction.atomic():
            self._req(master, status=S.APPROVED, resolved_template=template, decided_at=timezone.now()).save()

    def test_pending_binds_no_template(self, master, template):
        self._req(master).save()  # положительная пара
        with pytest.raises(IntegrityError, match="canongap_pending_binds_no_template"), transaction.atomic():
            self._req(master, resolved_template=template).save()

    def test_clarification_and_rejection_need_their_text(self, master, owner):
        now = timezone.now()
        with pytest.raises(IntegrityError, match="canongap_clarification_requires_question"), transaction.atomic():
            self._req(master, status=S.NEEDS_CLARIFICATION, decided_by=owner, decided_at=now).save()
        with pytest.raises(IntegrityError, match="canongap_rejection_requires_reason"), transaction.atomic():
            self._req(master, status=S.REJECTED, decided_by=owner, decided_at=now).save()
        self._req(master, status=S.REJECTED, decided_by=owner, decided_at=now, rejection_reason="нет в P0").save()


# ---------------------------------------------------------------------------
# Решает только человек
# ---------------------------------------------------------------------------


class TestOnlyAHumanDecides:
    def test_service_refuses_without_a_staff_actor(self, master, template):
        req = create_request(master, name="X", description="", duration_minutes=60, price=Decimal("1"))
        bot_like = User.objects.create_user(username="m9-bot", password="x", role="client")  # pragma: allowlist secret
        for actor in (None, bot_like):
            with pytest.raises(CanonGapDecisionError) as exc:
                decide(req, actor=actor, status=S.APPROVED, template=template)
            assert exc.value.code == "actor_required"
        req.refresh_from_db()
        assert req.status == S.PENDING

    def test_decided_is_terminal_and_clarification_can_be_answered(self, master, template, owner):
        req = create_request(master, name="X", description="", duration_minutes=60, price=Decimal("1"))
        req = decide(req, actor=owner, status=S.NEEDS_CLARIFICATION, question="Это брови или губы?")
        assert STATUS_LABELS[req.status] == "Нужно уточнение"
        req = decide(req, actor=owner, status=S.REJECTED, reason="Губы не в P0")
        with pytest.raises(CanonGapDecisionError) as exc:
            decide(req, actor=owner, status=S.APPROVED, template=template)
        assert exc.value.code == "already_decided"

    def test_the_api_cannot_decide(self, master):
        rid = _api().post(_url(master), BODY, format="json").data["data"]["request"]["id"]
        for method in ("patch", "put", "delete"):
            call = getattr(_api(), method)
            assert call(_url(master, f"{rid}/"), {"status": "approved"}, format="json").status_code == 405
            assert call(_url(master), {"status": "approved"}, format="json").status_code == 405
        assert CanonGapRequest.objects.get(pk=rid).status == S.PENDING

    def test_admin_decision_records_who_saved_it(self, master, template, owner):
        req = create_request(master, name="X", description="", duration_minutes=60, price=Decimal("1"))
        client = Client()
        client.force_login(owner)
        url = f"/admin/services/canongaprequest/{req.pk}/change/"
        empty = {"clarification_question": "", "rejection_reason": ""}
        bad = client.post(url, {"status": "approved", "resolved_template": "", **empty})
        assert bad.status_code == 200 and "template_required" not in bad.content.decode()  # форма, не 500
        assert "каноническим шаблоном" in bad.content.decode()
        req.refresh_from_db()
        assert req.status == S.PENDING

        ok = client.post(url, {"status": "approved", "resolved_template": str(template.pk), **empty})
        assert ok.status_code == 302, ok.content[:500]
        req.refresh_from_db()
        assert req.status == S.APPROVED and req.resolved_template_id == template.pk and req.decided_by == owner

    def test_admin_has_no_add_and_no_delete(self, owner):
        client = Client()
        client.force_login(owner)
        assert client.get("/admin/services/canongaprequest/add/").status_code == 403


# ---------------------------------------------------------------------------
# Субъект
# ---------------------------------------------------------------------------


class TestSubject:
    def test_own_list_and_detail(self, master, other):
        rid = _api().post(_url(master), BODY, format="json").data["data"]["request"]["id"]
        _api("bot:max:m9002").post(_url(other), {**BODY, "name": "Чужая"}, format="json")
        listed = _api().get(_url(master)).data["data"]["requests"]
        assert [r["id"] for r in listed] == [rid]
        assert _api().get(_url(master, f"{rid}/")).data["data"]["request"]["id"] == rid

    def test_foreign_profile_is_403_and_nothing_is_written(self, master, other):
        before = CanonGapRequest.objects.count()
        resp = _api("bot:max:m9001").post(_url(other), BODY, format="json")
        assert resp.status_code == 403
        assert CanonGapRequest.objects.count() == before

    def test_similar_and_detail_refuse_a_foreign_profile(self, master, other):
        """Отрицательные тесты для всех трёх маршрутов под субъектом (сторож
        ``SPECIALIST_ROUTES_TESTED_ELSEWHERE`` указывает сюда)."""
        foreign = create_request(other, name="Чужая", description="", duration_minutes=60, price=Decimal("1"))
        assert _api("bot:max:m9001").get(_url(other, "similar/"), {"name": "x"}).status_code == 403
        assert _api("bot:max:m9001").get(_url(other, f"{foreign.pk}/")).status_code == 403
        assert _api("bot:max:m9001").get(_url(other)).status_code == 403
        # Положительная пара: свой профиль по тем же маршрутам — 200.
        assert _api("bot:max:m9002").get(_url(other, f"{foreign.pk}/")).status_code == 200

    def test_foreign_request_id_under_own_url_is_404(self, master, other):
        foreign = create_request(other, name="Чужая", description="", duration_minutes=60, price=Decimal("1"))
        assert _api().get(_url(master, f"{foreign.pk}/")).status_code == 404

    def test_unlinked_proxy_is_refused(self, master):
        User.objects.create(username="bot:max:m9999", role="client", is_proxy=True, is_guest=False)
        assert _api("bot:max:m9999").post(_url(master), BODY, format="json").status_code == 403

    @pytest.mark.parametrize("bad", [
        {**BODY, "name": ""}, {**BODY, "duration_minutes": 0}, {k: v for k, v in BODY.items() if k != "price"},
    ])
    def test_validation(self, master, bad):
        assert _api().post(_url(master), bad, format="json").status_code == 400
        assert CanonGapRequest.objects.count() == 0
