"""DRF-2915 — предложения салонов по способности процедуры (вход шага плана).

- находятся предложения на каноне с ПОДТВЕРЖДЁННОЙ способностью; вывод
  системы, запрет и истёкшее не считаются;
- запись словаря привязана к нескольким канонам — находятся предложения всех;
- три разных «пусто» различимы закрытым списком;
- видимость та же, что у подбора: демо-салон — только тестовой личности;
- поиск допуск не обходит: найденная строка проходит те же восемь проверок;
- провод резолвера по способности по-прежнему не ищет — закреплено узлом.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from recommendation._reason_codes import ReasonCode
from recommendation._types import NeedOrigin, NeedSpec
from recommendation.tests.test_goal_fit_depth_2789 import _signed
from recommendation.tests.test_legal_gate_cat10_ext_2843 import (  # noqa: F401 — фикстуры
    _does,
    _excluded,
    _master,
    _offer,
    _resolve,
    _shown,
    category,
    curator,
    tenant,
)
from services.capabilities import template_ids_with_capability
from services.models import ProcedureCapability, SalonService, ServiceTemplate
from tenants.models import Tenant
from users.admission import OfferVerdict, offer_admission
from users.capability_offers import CapabilityOffers, NoOffers, offers_by_capability
from users.models import User

pytestmark = pytest.mark.django_db

KEY = "lymph-drainage-2915"


def _can(templates, curator, *, key=KEY, approved=True, **extra):  # noqa: F811
    return ProcedureCapability.objects.create(
        templates=list(templates), key=key, text_client="Синтетическая формулировка",
        **_signed(curator, approved, **extra),
    )


@pytest.fixture
def massage(tenant, category, curator):  # noqa: F811
    master = _master(tenant, "51")
    offering = _offer(tenant, category, curator, name="Массаж лимфодренажный")
    _does(master, offering)
    return master, offering


@pytest.fixture
def persona(db):
    return User.objects.create_user(
        username="cap2915-persona", password="x", role="client", phone="+79992915001", is_test_persona=True,
    )


class TestTheCapabilityMustBeConfirmed:
    def test_an_offer_on_a_canon_with_the_confirmed_capability_is_found(self, massage, curator):  # noqa: F811
        _, offering = massage
        _can([offering.template], curator)

        assert offers_by_capability(KEY) == CapabilityOffers((offering.pk,))

    def test_a_system_inference_is_not_a_capability(self, massage, curator):  # noqa: F811
        _, offering = massage
        _can([offering.template], curator, approved=False)

        assert offers_by_capability(KEY) == CapabilityOffers((), NoOffers.NO_CAPABILITY)

    def test_an_expired_capability_is_not_a_capability(self, massage, curator):  # noqa: F811
        _, offering = massage
        _can([offering.template], curator, valid_until=timezone.now() - timedelta(days=1))

        assert offers_by_capability(KEY).empty_because is NoOffers.NO_CAPABILITY

    def test_a_claim_that_is_not_supported_is_not_a_capability(self, massage, curator):  # noqa: F811
        _, offering = massage
        _can([offering.template], curator, claim_scope="not_supported")

        assert offers_by_capability(KEY).empty_because is NoOffers.NO_CAPABILITY

    def test_another_capability_of_the_same_canon_does_not_answer(self, massage, curator):  # noqa: F811
        _, offering = massage
        _can([offering.template], curator, key="something-else-2915")

        assert offers_by_capability(KEY).empty_because is NoOffers.NO_CAPABILITY

    def test_a_key_nobody_knows_is_no_capability_and_not_an_error(self):
        assert offers_by_capability("nobody-knows-this") == CapabilityOffers((), NoOffers.NO_CAPABILITY)


class TestWhichOffersAreFound:
    def test_one_capability_on_several_canons_finds_the_offers_of_each(
        self, massage, tenant, category, curator,  # noqa: F811
    ):
        _, first = massage
        second = _offer(tenant, category, curator, name="Прессотерапия")
        other = _offer(tenant, category, curator, name="Маникюр")
        _can([first.template, second.template], curator)

        assert set(offers_by_capability(KEY).offer_ids) == {first.pk, second.pk}
        assert other.pk not in offers_by_capability(KEY).offer_ids

    def test_two_salons_offering_the_canon_are_both_found_in_a_stable_order(
        self, massage, category, curator,  # noqa: F811
    ):
        _, first = massage
        _can([first.template], curator)
        elsewhere = Tenant.objects.create(slug="cap2915-second", name="Второй салон", is_active=True)
        second = SalonService.objects.create(
            tenant=elsewhere, category=category, template=first.template, name="Тот же массаж",
            duration_minutes=60, base_price=Decimal("2500"),
        )

        assert offers_by_capability(KEY).offer_ids == tuple(sorted([first.pk, second.pk]))

    def test_an_offer_without_a_canon_is_never_found(self, massage, tenant, category, curator):  # noqa: F811
        _, offering = massage
        _can([offering.template], curator)
        orphan = SalonService.objects.create(
            tenant=tenant, category=category, template=None, name="Массаж без канона",
            duration_minutes=60, base_price=Decimal("3000"),
        )

        assert orphan.pk not in offers_by_capability(KEY).offer_ids

    def test_the_number_of_queries_does_not_grow_with_the_offers(
        self, massage, tenant, category, curator,  # noqa: F811
    ):
        _, offering = massage
        _can([offering.template], curator)
        with CaptureQueriesContext(connection) as few:
            offers_by_capability(KEY)
        for index in range(6):
            SalonService.objects.create(
                tenant=tenant, category=category, template=offering.template, name=f"Ещё массаж {index}",
                duration_minutes=60, base_price=Decimal("3000"),
            )
        with CaptureQueriesContext(connection) as many:
            found = offers_by_capability(KEY)

        assert len(found.offer_ids) == 7
        assert len(many) == len(few)


class TestTheThreeKindsOfEmpty:
    def test_a_canon_with_the_capability_and_no_offer(self, category, curator):  # noqa: F811
        canon = ServiceTemplate.objects.create(category=category, name="Канон без предложений", name_short="Канон")
        _can([canon], curator)

        assert offers_by_capability(KEY) == CapabilityOffers((), NoOffers.NO_OFFER)

    def test_a_switched_off_offer_is_no_offer(self, massage, curator):  # noqa: F811
        _, offering = massage
        _can([offering.template], curator)
        SalonService.objects.filter(pk=offering.pk).update(is_active=False)

        assert offers_by_capability(KEY).empty_because is NoOffers.NO_OFFER

    def test_an_offer_of_a_dead_salon_is_no_offer(self, massage, tenant, curator):  # noqa: F811
        _, offering = massage
        _can([offering.template], curator)
        Tenant.objects.filter(pk=tenant.pk).update(is_active=False)

        assert offers_by_capability(KEY).empty_because is NoOffers.NO_OFFER

    def test_an_offer_only_in_a_demo_salon_is_out_of_sight_for_a_client(self, massage, tenant, curator):  # noqa: F811
        _, offering = massage
        _can([offering.template], curator)
        Tenant.objects.filter(pk=tenant.pk).update(is_demo=True)

        assert offers_by_capability(KEY) == CapabilityOffers((), NoOffers.OUT_OF_SIGHT)

    def test_the_same_offer_is_found_for_a_test_persona(self, massage, tenant, curator, persona):  # noqa: F811
        _, offering = massage
        _can([offering.template], curator)
        Tenant.objects.filter(pk=tenant.pk).update(is_demo=True)

        assert offers_by_capability(KEY, viewer=persona) == CapabilityOffers((offering.pk,))

    def test_the_reason_is_named_exactly_when_nothing_is_found(self, massage, curator):  # noqa: F811
        _, offering = massage
        assert offers_by_capability(KEY).empty_because is not None
        _can([offering.template], curator)
        assert offers_by_capability(KEY).empty_because is None


class TestTheSearchDoesNotBypassAdmission:
    def test_a_found_offer_still_answers_to_the_eight_checks(self, massage, curator, settings):  # noqa: F811
        """Найти — не значит допустить: при включённом флаге каталога неклассифицированная строка закрыта."""
        settings.BODY_CARE_UNCLASSIFIED_FAIL_CLOSED = True
        _, offering = massage
        _can([offering.template], curator)

        found = offers_by_capability(KEY)
        verdict = offer_admission(found.offer_ids)[offering.pk]

        assert found.offer_ids == (offering.pk,)
        assert verdict.verdict is OfferVerdict.NOT_ADMITTED
        assert [a.reason for a in verdict.unmet] == [ReasonCode.ELIG_EXCLUDED_CATALOG_UNCLASSIFIED]

    def test_a_found_offer_with_nobody_to_do_it_says_so(self, tenant, category, curator):  # noqa: F811
        offering = _offer(tenant, category, curator, name="Массаж без мастера")
        _can([offering.template], curator)

        found = offers_by_capability(KEY)

        assert offer_admission(found.offer_ids)[offering.pk].verdict is OfferVerdict.NO_SELLABLE_MASTER


class TestTheReaderOfKnowledge:
    def test_it_names_the_canons_and_nothing_else(self, massage, category, curator):  # noqa: F811
        _, offering = massage
        bare = ServiceTemplate.objects.create(category=category, name="Канон без способности", name_short="Без")
        _can([offering.template], curator)

        assert template_ids_with_capability(KEY) == frozenset({offering.template_id})
        assert bare.pk not in template_ids_with_capability(KEY)

    def test_a_capability_attached_to_no_canon_names_none(self, curator):  # noqa: F811
        _can([], curator)

        assert template_ids_with_capability(KEY) == frozenset()


class TestTheWireOfTheResolverStillDoesNotSearchByCapability:
    """Известный дефект провода, НЕ починенный этим листом (DRF-2915, «остаётся открытым»).

    Нужда, в которой названа только способность, считается названной, но
    источник по этому полю не ищет — исключены все. Узел держит факт, чтобы
    починка провода пришла осознанной правкой, а не побочным эффектом; плану
    этот путь не нужен — он зовёт ``offers_by_capability``.
    """

    def test_a_need_naming_only_a_capability_excludes_everybody(self, massage, curator):  # noqa: F811
        master, offering = massage
        capability = _can([offering.template], curator)
        need = NeedSpec(origin=NeedOrigin.USER_EXPLICIT, capability_refs=(capability.pk,))

        decision = _resolve(need)

        assert need.is_stated
        assert _shown(decision) == set()
        assert _excluded(decision)[str(master.user_id)] is ReasonCode.ELIG_EXCLUDED_NOT_CAPABLE

    def test_the_same_master_is_shown_when_nothing_is_named(self, massage):
        """Контроль: мастер исключён именно из-за способности в нужде, а не сам по себе."""
        master, _ = massage

        assert _shown(_resolve()) == {str(master.user_id)}


def test_a_random_key_costs_one_query():
    with CaptureQueriesContext(connection) as queries:
        offers_by_capability(f"unknown-{uuid.uuid4().hex[:8]}")

    assert len(queries) == 1
