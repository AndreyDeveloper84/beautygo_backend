"""DRF-1800 (M8a): мастер-соло выбирает канонические услуги.

``GET/POST /api/v1/internal/specialists/{id}/services/selection/`` под
``IsInternalBearerForSpecialistSubject``. Что стережётся:

* выбор заводит только ``SalonService`` (``REVIEW_REQUIRED``, без цены,
  провенанс ``master_select:<profile>``) и ни одного ``SpecialistService``;
* повтор и пересечение идемпотентны по (tenant, template); дубли в одном
  вызове — одна строка;
* уже существующая строка с шаблоном (в том числе решённая модератором) —
  не перепривязывается и не переименовывается;
* неизвестный шаблон или шаблон из категории чужого тенанта — 404, ничего
  не создано (всё или ничего);
* мастер салона — 409 ``salon_catalog_owner_managed``; чужой workspace — 403;
* счётчики ``selected`` / ``configured`` считает сервер;
* зеркало ``/internal/catalog/specialist-services/`` не имеет гейта по
  ``is_active`` и безопасно только потому, что ``SpecialistService.price``
  NOT NULL и в ORM, и в схеме — сторож ниже.

Каждый отказ стоит рядом с положительной половиной на тех же данных.
Красный до правки: весь файл, кроме сторожа схемы (маршрута нет → 404);
сторож схемы зелёный в обе стороны — он держит решение «строка при первой
цене», а не новый код.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from services.models import SalonService, ServiceCategory, ServiceTemplate, SpecialistService
from tenants.models import Tenant
from tenants.solo_provisioning import provision_solo_workspace
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token-1800"  # noqa: S105
OLGA = "bot:max:1800001"
IRINA = "bot:max:1800002"


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN


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
    return _provision(OLGA, "solo-max-1800olga")


@pytest.fixture
def irina() -> SpecialistProfile:
    return _provision(IRINA, "solo-max-1800irin")


@pytest.fixture
def nails(db) -> ServiceCategory:
    return ServiceCategory.objects.create(name="Ногти M8a 1800", slug="nails-m8a-1800")


def _template(category: ServiceCategory, name: str, duration: int = 60) -> ServiceTemplate:
    return ServiceTemplate.objects.create(
        category=category, name=name, name_short=name[:40], duration_default=duration,
    )


@pytest.fixture
def manicure(nails) -> ServiceTemplate:
    return _template(nails, "Маникюр M8a 1800")


@pytest.fixture
def pedicure(nails) -> ServiceTemplate:
    return _template(nails, "Педикюр M8a 1800", duration=90)


def _client(actor: str = OLGA) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RUNTIME_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = actor
    return c


def _url(profile_id) -> str:
    return f"/api/v1/internal/specialists/{profile_id}/services/selection/"


def _select(profile, *templates, actor: str = OLGA, raw_ids=None):
    ids = raw_ids if raw_ids is not None else [str(t.pk) for t in templates]
    return _client(actor).post(_url(profile.pk), {"template_ids": ids}, format="json")


def _rows(profile):
    return SalonService.objects.filter(tenant_id=profile.tenant_id)


class TestSelectionCreatesTheWorkspaceRowOnly:
    def test_first_select_creates_review_required_rows_and_no_offers(self, olga, manicure, pedicure):
        r = _select(olga, manicure, pedicure)

        assert r.status_code == 201, r.content
        data = r.json()["data"]
        assert data["created"] == 2
        assert data["selected"] == 2 and data["configured"] == 0
        assert len(data["services"]) == 2

        rows = {row.template_id: row for row in _rows(olga)}
        assert set(rows) == {manicure.pk, pedicure.pk}
        for template in (manicure, pedicure):
            row = rows[template.pk]
            assert row.name == template.name
            assert row.category_id == template.category_id
            assert row.mapping_status == SalonService.MappingStatus.REVIEW_REQUIRED
            assert row.base_price is None
            assert row.source == SalonService.Source.MANUAL
            assert row.mapping_source_ref == f"master_select:{olga.pk}"
            assert row.is_active is True
        # Строка предложения — только при первой цене (M8b).
        assert SpecialistService.objects.filter(specialist=olga).count() == 0
        assert all(item["offer"] is None and item["configured"] is False for item in data["services"])

    def test_a_repeat_and_an_overlap_are_idempotent(self, olga, manicure, pedicure):
        first = _select(olga, manicure)
        assert first.status_code == 201, first.content
        manicure_row = _rows(olga).get(template=manicure).pk

        overlap = _select(olga, manicure, pedicure)
        assert overlap.status_code == 201, overlap.content
        assert overlap.json()["data"]["created"] == 1
        assert _rows(olga).count() == 2
        assert _rows(olga).get(template=manicure).pk == manicure_row

        repeat = _select(olga, manicure, pedicure)
        assert repeat.status_code == 200, repeat.content
        assert repeat.json()["data"]["created"] == 0
        assert _rows(olga).count() == 2

    def test_duplicate_ids_in_one_call_are_one_row(self, olga, manicure):
        r = _select(olga, raw_ids=[str(manicure.pk), str(manicure.pk)])
        assert r.status_code == 201, r.content
        assert _rows(olga).count() == 1


class TestAnExistingRowIsNeverRemapped:
    def test_a_decided_row_for_the_template_is_returned_untouched(self, olga, manicure):
        decided = SalonService.objects.create(
            tenant=olga.tenant,
            template=manicure,
            category=manicure.category,
            name="Маникюр — как у модератора",
            mapping_status=SalonService.MappingStatus.VERIFIED,
            mapping_confirmed_at=timezone.now(),
            mapping_confirmed_rule="owner_review",
            mapping_rule_version="1",
            mapping_source_ref="review-1800",
        )

        r = _select(olga, manicure)

        assert r.status_code == 200, r.content
        assert r.json()["data"]["created"] == 0
        assert _rows(olga).count() == 1
        decided.refresh_from_db()
        assert decided.mapping_status == SalonService.MappingStatus.VERIFIED
        assert decided.name == "Маникюр — как у модератора"


class TestRefusals:
    def test_an_unknown_template_is_404_and_nothing_is_created(self, olga, manicure):
        missing = str(uuid.uuid4())

        r = _select(olga, raw_ids=[str(manicure.pk), missing])

        assert r.status_code == 404, r.content
        details = r.json()["error"]["details"]
        assert details == {"reason": "template_not_found", "template_ids": [missing]}
        assert _rows(olga).count() == 0  # всё или ничего
        # Положительная половина: тот же известный шаблон — выбирается.
        assert _select(olga, manicure).status_code == 201

    def test_a_template_in_another_tenants_category_is_404(self, olga, manicure):
        salon = Tenant.objects.create(slug="salon-m8a-1800", name="Чужой салон")
        curated = ServiceCategory.objects.create(
            name="Ногти салона M8a 1800", slug="salon-nails-m8a-1800", tenant=salon,
        )
        foreign = _template(curated, "Маникюр салона M8a 1800")

        r = _select(olga, foreign)

        assert r.status_code == 404, r.content
        assert _rows(olga).count() == 0
        assert _select(olga, manicure).status_code == 201

    def test_a_salon_master_does_not_edit_the_salon_catalog(self, olga, manicure):
        salon = Tenant.objects.create(slug="salon2-m8a-1800", name="Салон мастера")
        user = User.objects.create_user(
            username="salon_m8a_1800",
            password="x",  # pragma: allowlist secret
            role="specialist",
            phone="+79995180001",
        )
        user.tenant = salon
        user.save(update_fields=["tenant"])
        master = SpecialistProfile.objects.get(user=user)
        master.status = SpecialistProfile.ProfileStatus.ACTIVE
        master.tenant = salon
        master.save()
        User.objects.create(
            username="bot:max:1800900", role="client", is_proxy=True, is_guest=False, linked_user=user,
        )

        r = _select(master, manicure, actor="bot:max:1800900")
        read = _client("bot:max:1800900").get(_url(master.pk))

        assert r.status_code == 409, r.content
        assert r.json()["error"]["code"] == "SERVICE_SELECTION_REFUSED"
        assert r.json()["error"]["details"] == {"reason": "salon_catalog_owner_managed"}
        assert read.status_code == 409, read.content
        assert SalonService.objects.filter(tenant=salon).count() == 0
        # Положительная половина: соло с тем же шаблоном — выбирает.
        assert _select(olga, manicure).status_code == 201

    def test_another_masters_workspace_is_403(self, olga, irina, manicure):
        r = _select(irina, manicure, actor=OLGA)

        assert r.status_code == 403, r.content
        assert _rows(irina).count() == 0
        assert _select(olga, manicure, actor=OLGA).status_code == 201

    @pytest.mark.parametrize("ids", [[], ["not-a-uuid"], None])
    def test_a_malformed_body_is_400(self, olga, manicure, ids):
        body = {} if ids is None else {"template_ids": ids}
        r = _client().post(_url(olga.pk), body, format="json")

        assert r.status_code == 400, r.content
        assert _rows(olga).count() == 0


class TestTheServerCountsSelectedAndConfigured:
    def test_get_reads_back_selection_and_offers(self, olga, manicure, pedicure):
        assert _select(olga, manicure, pedicure).status_code == 201
        manicure_row = _rows(olga).get(template=manicure)
        SpecialistService.objects.create(
            salon_service=manicure_row, specialist=olga, price=Decimal("1500"),
        )

        r = _client().get(_url(olga.pk))

        assert r.status_code == 200, r.content
        data = r.json()["data"]
        assert data["selected"] == 2 and data["configured"] == 1
        items = {item["template_id"]: item for item in data["services"]}
        assert items[str(manicure.pk)]["configured"] is True
        assert items[str(manicure.pk)]["offer"]["price"] == "1500.00"
        assert items[str(manicure.pk)]["offer"]["duration_minutes"] == 60  # из шаблона
        assert items[str(pedicure.pk)]["configured"] is False
        assert items[str(pedicure.pk)]["offer"] is None
        # Счётчики — длины того же ответа, а не отдельное число.
        assert data["selected"] == sum(1 for i in data["services"] if i["is_active"])
        assert data["configured"] == sum(1 for i in data["services"] if i["configured"])


class TestTheBotMirrorNeverSeesAnOfferWithoutAPrice:
    """``/internal/catalog/specialist-services/`` отдаёт строки без гейта по
    ``is_active``; его 18 соседей-читателей цены (запись, карточки ИИ, поиск)
    падают или печатают ``None`` на пустой цене. Безопасно ровно потому, что
    строки без цены не бывает: M8 заводит предложение при первой цене, а не
    при выборе. Сделать ``price`` nullable — значит снять этот сторож и
    пройти всех читателей."""

    def test_price_is_not_nullable_in_the_orm_and_in_the_schema(self):
        assert SpecialistService._meta.get_field("price").null is False

        with connection.cursor() as cursor:
            description = connection.introspection.get_table_description(
                cursor, SpecialistService._meta.db_table,
            )
        nullable = {column.name: column.null_ok for column in description}
        assert nullable["price"] is False
        # Положительная стража: интроспекция читает ту самую таблицу.
        assert nullable["duration_minutes"] is True
