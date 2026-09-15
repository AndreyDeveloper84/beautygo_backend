"""DRF-1796 (M4): готовность к публикации, «Опубликовать», «Проверить статус», гейт модератора.

Что стережётся:

* готовность считает сервер одним местом и называет каждый недостающий
  пункт поимённо: матрица «полный workspace — READY; снят ровно один пункт —
  NOT_READY ровно с его кодом»;
* место — ``works_at`` мастера по критерию движка расстояний, а не «какое-то
  подтверждённое место тенанта»; выезд/весь город назван как недоступный;
* «Опубликовать» переводит DRAFT → PENDING один раз на ключ команды: повтор —
  та же строка аудита; неготовность — 409 и ничего не записано; pre-LINKED
  владелец не публикуется до связи; чужой ключ — отказ;
* ACTIVE ставит только модератор, и его действие проверяет ту же готовность
  у соло-мастера; салонный мастер активируется как прежде — тест назван.

Каждый отказ рядом с положительной половиной на тех же данных.
Красный до правки: HTTP-тесты (маршрутов нет) и два теста гейта модератора
(действие активировало без проверки). Зелёные в обе стороны — «готовый соло
одобряется» и «салонный мастер по-прежнему» — они держат, что гейт не
сломал разрешённое.
"""

from __future__ import annotations

import uuid
from datetime import time
from decimal import Decimal

import pytest
from django.contrib import admin as django_admin
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory
from django.utils import timezone
from rest_framework.test import APIClient

from appointments.models import SpecialistWorkingHours
from services.models import ServiceCategory, ServiceTemplate, SpecialistService
from services.offer_selection import select_templates, set_offer
from tenants.models import GeocodeStatus, LocationStatus, ServiceLocation, Tenant
from tenants.solo_provisioning import provision_solo_workspace
from users.admin import SpecialistProfileAdmin, approve_specialists
from users.models import SpecialistProfile, SpecialistPublicationRequest, User
from users.publication import publish
from users.services import bind_external_identity_by_operator

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token-1796"  # noqa: S105
OLGA = "bot:max:1796001"
IRINA = "bot:max:1796002"


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN


@pytest.fixture
def operator(db) -> User:
    return User.objects.create_superuser(
        username="op-1796", password="pw", email="op-1796@x.y", role="admin",  # pragma: allowlist secret
    )


def _provision(external_user_id: str, slug: str) -> SpecialistProfile:
    return provision_solo_workspace(
        tenant_id=uuid.uuid4(),
        slug=slug,
        name=f"Студия {slug}",
        city="Пенза",
        external_user_id=external_user_id,
        display_name="Мастер",
    ).profile


@pytest.fixture
def olga() -> SpecialistProfile:
    return _provision(OLGA, "solo-max-1796olga")


@pytest.fixture
def irina() -> SpecialistProfile:
    return _provision(IRINA, "solo-max-1796irin")


def _link(profile: SpecialistProfile, external_user_id: str, operator: User) -> None:
    User.objects.get_or_create(
        username=external_user_id,
        defaults={"role": "client", "is_proxy": True, "is_guest": False},
    )
    bind_external_identity_by_operator(external_user_id, profile.user_id, actor=operator)


def _make_ready(profile: SpecialistProfile, external_user_id: str, operator: User) -> SpecialistProfile:
    """Полный workspace: фото, настроенная услуга, место, рабочий день, связь."""

    suffix = uuid.uuid4().hex[:8]
    SpecialistProfile.objects.filter(pk=profile.pk).update(avatar=f"specialists/avatars/{suffix}.jpg")

    category = ServiceCategory.objects.create(name=f"Ногти 1796 {suffix}", slug=f"nails-1796-{suffix}")
    template = ServiceTemplate.objects.create(
        category=category, name=f"Маникюр 1796 {suffix}", name_short="Маникюр", duration_default=60,
    )
    select_templates(profile, [template.pk])
    row = profile.tenant.salon_services.get(template=template)
    set_offer(profile, row.pk, price=Decimal("1500"), duration_minutes=60)

    place = ServiceLocation.objects.create(
        tenant=None,
        address=f"Пенза, Московская, {suffix}",
        city="Пенза",
        latitude=Decimal("53.195000"),
        longitude=Decimal("45.018000"),
        geocode_status=GeocodeStatus.OK,
        status=LocationStatus.CONFIRMED,
        confirmed_by=operator,
        confirmed_at=timezone.now(),
        confirmed_source_ref="test-1796",
    )
    SpecialistProfile.objects.filter(pk=profile.pk).update(works_at=place)

    SpecialistWorkingHours.objects.create(
        specialist=profile, day_of_week=0, is_working_day=True, start_time=time(10), end_time=time(19),
    )
    _link(profile, external_user_id, operator)
    profile.refresh_from_db()
    return profile


def _client(actor: str = OLGA) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RUNTIME_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = actor
    return c


def _readiness(profile, actor: str = OLGA):
    return _client(actor).get(f"/api/v1/internal/specialists/{profile.pk}/publication/readiness/")


def _publish(profile, command_id, actor: str = OLGA):
    return _client(actor).post(
        f"/api/v1/internal/specialists/{profile.pk}/publication/",
        {"command_id": str(command_id)},
        format="json",
    )


def _status(profile, actor: str = OLGA):
    return _client(actor).get(f"/api/v1/internal/specialists/{profile.pk}/publication/status/")


def _salon_master(*, status: str, external_user_id: str) -> SpecialistProfile:
    salon = Tenant.objects.create(slug=f"salon-1796-{uuid.uuid4().hex[:6]}", name="Салон 1796")
    user = User.objects.create_user(
        username=f"salon_1796_{uuid.uuid4().hex[:6]}",
        password="x",  # pragma: allowlist secret
        role="specialist",
        phone=f"+7999517{uuid.uuid4().int % 10000:04d}",
    )
    user.tenant = salon
    user.save(update_fields=["tenant"])
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = salon
    profile.status = status
    profile.save()
    User.objects.create(
        username=external_user_id, role="client", is_proxy=True, is_guest=False, linked_user=user,
    )
    return profile


class TestReadinessIsOneServerAnswer:
    def test_a_complete_workspace_is_ready(self, olga, operator):
        _make_ready(olga, OLGA, operator)

        r = _readiness(olga)

        assert r.status_code == 200, r.content
        assert r.json()["data"] == {"specialist_id": str(olga.pk), "status": "READY", "missing": []}

    @pytest.mark.parametrize(
        "take_away, code, section",
        [
            ("photo", "photo_missing", "profile"),
            ("services", "no_configured_service", "services"),
            ("location_unassigned", "location_not_assigned", "location"),
            ("location_unconfirmed", "location_not_participating", "location"),
            ("hours", "no_working_day", "hours"),
            ("identity", "identity_not_linked", "identity"),
        ],
    )
    def test_taking_one_item_away_names_exactly_that_item(self, olga, operator, take_away, code, section):
        _make_ready(olga, OLGA, operator)
        if take_away == "photo":
            SpecialistProfile.objects.filter(pk=olga.pk).update(avatar=None)
        elif take_away == "services":
            SpecialistService.objects.filter(specialist=olga).delete()
        elif take_away == "location_unassigned":
            SpecialistProfile.objects.filter(pk=olga.pk).update(works_at=None)
        elif take_away == "location_unconfirmed":
            ServiceLocation.objects.filter(pk=olga.works_at_id).update(
                status=LocationStatus.REVIEW_REQUIRED, confirmed_by=None, confirmed_at=None, confirmed_source_ref="",
            )
        elif take_away == "hours":
            SpecialistWorkingHours.objects.filter(specialist=olga).delete()
        else:
            User.objects.filter(username=OLGA).update(linked_user=None)

        r = _readiness(olga)

        assert r.status_code == 200, r.content
        data = r.json()["data"]
        assert data["status"] == "NOT_READY"
        assert [(m["code"], m["section"]) for m in data["missing"]] == [(code, section)]
        if section == "location":
            assert data["missing"][0]["detail"] == {"area_option": "location_area_unavailable"}

    def test_a_fresh_pre_linked_workspace_lists_everything_it_lacks(self, olga):
        r = _readiness(olga)

        assert r.status_code == 200, r.content
        codes = {m["code"] for m in r.json()["data"]["missing"]}
        assert codes == {
            "photo_missing", "no_configured_service", "location_not_assigned",
            "no_working_day", "identity_not_linked",
        }


class TestPublish:
    def test_one_command_is_one_transition_and_one_audit_row(self, olga, operator):
        _make_ready(olga, OLGA, operator)
        key = uuid.uuid4()

        first = _publish(olga, key)
        second = _publish(olga, key)

        assert first.status_code == 201, first.content
        assert first.json()["data"]["request"]["outcome"] == "submitted"
        assert first.json()["data"]["profile_status"] == "pending"
        assert second.status_code == 200, second.content
        assert second.json()["data"]["replayed"] is True
        assert second.json()["data"]["request"]["id"] == first.json()["data"]["request"]["id"]
        assert SpecialistPublicationRequest.objects.filter(specialist=olga).count() == 1
        olga.refresh_from_db()
        assert olga.status == SpecialistProfile.ProfileStatus.PENDING

    def test_a_new_command_while_pending_is_recorded_without_a_transition(self, olga, operator):
        _make_ready(olga, OLGA, operator)
        assert _publish(olga, uuid.uuid4()).status_code == 201

        r = _publish(olga, uuid.uuid4())

        assert r.status_code == 200, r.content
        request = r.json()["data"]["request"]
        assert request["outcome"] == "already_pending"
        assert request["from_status"] == request["to_status"] == "pending"
        assert SpecialistPublicationRequest.objects.filter(specialist=olga).count() == 2

    def test_not_ready_is_409_and_nothing_is_written(self, olga, operator):
        key = uuid.uuid4()

        r = _publish(olga, key)

        assert r.status_code == 409, r.content
        assert r.json()["error"]["code"] == "PUBLICATION_NOT_READY"
        assert r.json()["error"]["details"]["status"] == "NOT_READY"
        assert "identity_not_linked" in {m["code"] for m in r.json()["error"]["details"]["missing"]}
        olga.refresh_from_db()
        assert olga.status == SpecialistProfile.ProfileStatus.DRAFT
        assert SpecialistPublicationRequest.objects.count() == 0
        # Положительная половина: отказ не записан — тот же ключ после настройки проходит.
        _make_ready(olga, OLGA, operator)
        assert _publish(olga, key).status_code == 201

    def test_a_pre_linked_owner_publishes_only_after_the_link(self, olga, operator):
        _make_ready(olga, OLGA, operator)
        User.objects.filter(username=OLGA).update(linked_user=None)

        refused = _publish(olga, uuid.uuid4())

        assert refused.status_code == 409, refused.content
        assert [m["code"] for m in refused.json()["error"]["details"]["missing"]] == ["identity_not_linked"]
        _link(olga, OLGA, operator)
        assert _publish(olga, uuid.uuid4()).status_code == 201

    def test_a_command_id_of_another_master_is_refused(self, olga, irina, operator):
        _make_ready(olga, OLGA, operator)
        _make_ready(irina, IRINA, operator)
        key = uuid.uuid4()
        assert _publish(olga, key).status_code == 201

        r = _publish(irina, key, actor=IRINA)

        assert r.status_code == 409, r.content
        assert r.json()["error"]["details"] == {"reason": "command_id_reused"}
        irina.refresh_from_db()
        assert irina.status == SpecialistProfile.ProfileStatus.DRAFT
        assert _publish(irina, uuid.uuid4(), actor=IRINA).status_code == 201

    def test_a_salon_master_is_refused(self, olga, operator):
        master = _salon_master(status=SpecialistProfile.ProfileStatus.DRAFT, external_user_id="bot:max:1796900")

        read = _readiness(master, actor="bot:max:1796900")
        post = _publish(master, uuid.uuid4(), actor="bot:max:1796900")

        for r in (read, post):
            assert r.status_code == 409, r.content
            assert r.json()["error"]["details"] == {"reason": "salon_publication_owner_managed"}
        master.refresh_from_db()
        assert master.status == SpecialistProfile.ProfileStatus.DRAFT
        # Положительная половина: соло на тех же ручках отвечает.
        assert _readiness(olga).status_code == 200

    def test_another_masters_workspace_is_403(self, olga, irina):
        assert _readiness(irina, actor=OLGA).status_code == 403
        assert _publish(irina, uuid.uuid4(), actor=OLGA).status_code == 403
        assert _readiness(olga, actor=OLGA).status_code == 200

    def test_status_reports_the_profile_the_readiness_and_the_last_command(self, olga, operator):
        _make_ready(olga, OLGA, operator)
        before = _status(olga)
        assert before.status_code == 200, before.content
        assert before.json()["data"]["last_request"] is None

        assert _publish(olga, uuid.uuid4()).status_code == 201
        after = _status(olga).json()["data"]

        assert after["profile_status"] == "pending"
        assert after["readiness"]["status"] == "READY"
        assert after["last_request"]["outcome"] == "submitted"


def _approve(queryset, operator: User) -> list[str]:
    request = RequestFactory().post("/admin/users/specialistprofile/")
    request.user = operator
    request.session = {}
    request._messages = FallbackStorage(request)
    approve_specialists(SpecialistProfileAdmin(SpecialistProfile, django_admin.site), request, queryset)
    return [str(m) for m in request._messages]


class TestTheModeratorActivatesOnlyAReadyLinkedSoloMaster:
    def test_a_ready_linked_solo_master_is_approved(self, olga, operator):
        _make_ready(olga, OLGA, operator)
        publish(olga, uuid.uuid4())

        _approve(SpecialistProfile.objects.filter(pk=olga.pk), operator)

        olga.refresh_from_db()
        assert olga.status == SpecialistProfile.ProfileStatus.ACTIVE

    def test_an_incomplete_solo_master_is_not_approved_and_the_moderator_sees_why(self, olga, operator):
        _make_ready(olga, OLGA, operator)
        publish(olga, uuid.uuid4())
        SpecialistWorkingHours.objects.filter(specialist=olga).delete()

        messages = _approve(SpecialistProfile.objects.filter(pk=olga.pk), operator)

        olga.refresh_from_db()
        assert olga.status == SpecialistProfile.ProfileStatus.PENDING
        assert any("no_working_day" in m for m in messages), messages
        # Положительная половина: рабочий день вернулся — модератор одобряет.
        SpecialistWorkingHours.objects.create(
            specialist=olga, day_of_week=1, is_working_day=True, start_time=time(10), end_time=time(19),
        )
        _approve(SpecialistProfile.objects.filter(pk=olga.pk), operator)
        olga.refresh_from_db()
        assert olga.status == SpecialistProfile.ProfileStatus.ACTIVE

    def test_an_unlinked_solo_draft_is_not_approved(self, olga, operator):
        messages = _approve(SpecialistProfile.objects.filter(pk=olga.pk), operator)

        olga.refresh_from_db()
        assert olga.status == SpecialistProfile.ProfileStatus.DRAFT
        assert any("identity_not_linked" in m for m in messages), messages

    def test_a_salon_master_keeps_the_old_approval(self, operator):
        """Названная разница: каталог салона ведёт владелец салона, соло-готовности
        у салонного мастера нет — модератор активирует его как прежде."""

        master = _salon_master(status=SpecialistProfile.ProfileStatus.PENDING, external_user_id="bot:max:1796901")

        _approve(SpecialistProfile.objects.filter(pk=master.pk), operator)

        master.refresh_from_db()
        assert master.status == SpecialistProfile.ProfileStatus.ACTIVE
