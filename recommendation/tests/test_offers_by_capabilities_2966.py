# flake8: noqa: F811
"""DRF-2966 — пакетный поиск предложений по способностям.

Решение владельца 08.10 (толкование 3, пункт «б»): показывать «доступно для
цели, но не в плане» с признаком исполнимости для клиента. Каталогу для этого
нужен тот же поиск по способности, но по списку ключей сразу.

Главное свойство — эквивалентность: пакетный ответ по каждому ключу равен
одиночному. Одиночная функция считается через пакетную, поэтому «равен»
проверяется не друг о друга (это была бы тавтология), а о заранее названный
ожидаемый ответ на каждую из шести ситуаций.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from recommendation.tests.test_goal_fit_depth_2789 import _signed
from recommendation.tests.test_legal_gate_cat10_ext_2843 import (  # noqa: F401 — фикстуры
    _does,
    _master,
    _offer,
    category,
    curator,
    tenant,
)
from recommendation.tests.test_synthetic_admission_2916 import chain, granted  # noqa: F401 — фикстуры
from recommendation.tests.test_synthetic_rows_are_not_real_2916 import demo, persona  # noqa: F401 — фикстуры
from services.capabilities import template_ids_by_capability
from services.models import ProcedureCapability, SalonService, ServiceTemplate
from services.tests.test_synthetic_test_mark import _capability
from tenants.models import Tenant
from users.admission import OfferVerdict, offer_admission
from users.capability_offers import CapabilityOffers, NoOffers, offers_by_capabilities, offers_by_capability

pytestmark = pytest.mark.django_db


def _can(templates, curator, key, *, approved=True, **extra):
    return ProcedureCapability.objects.create(
        templates=list(templates), key=key, text_client="Синтетическая формулировка",
        **_signed(curator, approved, **extra),
    )


@pytest.fixture
def six(tenant, category, curator):
    """Шесть способностей — по одной на каждую ситуацию — и ожидаемый ответ на каждую."""
    found = _offer(tenant, category, curator, name="Массаж")
    _can([found.template], curator, "found")

    inferred = _offer(tenant, category, curator, name="Прессотерапия")
    _can([inferred.template], curator, "inferred", approved=False)

    expired = _offer(tenant, category, curator, name="Обёртывание")
    _can([expired.template], curator, "expired", valid_until=timezone.now() - timedelta(days=1))

    bare = ServiceTemplate.objects.create(category=category, name="Канон без предложений", name_short="Канон")
    _can([bare], curator, "no-offer")

    hidden_salon = Tenant.objects.create(slug="cap2966-demo", name="Демо", is_active=True, is_demo=True)
    hidden_canon = ServiceTemplate.objects.create(category=category, name="Канон демо", name_short="Демо")
    hidden = SalonService.objects.create(
        tenant=hidden_salon, category=category, template=hidden_canon, name="Услуга демо",
        duration_minutes=60, base_price=Decimal("3000"),
    )
    _can([hidden_canon], curator, "out-of-sight")

    return {
        "found": CapabilityOffers((found.pk,)),
        "inferred": CapabilityOffers((), NoOffers.NO_CAPABILITY),
        "expired": CapabilityOffers((), NoOffers.NO_CAPABILITY),
        "no-offer": CapabilityOffers((), NoOffers.NO_OFFER),
        "out-of-sight": CapabilityOffers((), NoOffers.OUT_OF_SIGHT),
        "nobody-knows": CapabilityOffers((), NoOffers.NO_CAPABILITY),
        "_hidden_offer": hidden,
    }


def _expected(six) -> dict:
    return {key: value for key, value in six.items() if not key.startswith("_")}


class TestEveryKeyGetsTheAnswerOfTheSingleSearch:
    def test_six_situations_in_one_call(self, six):
        expected = _expected(six)

        assert offers_by_capabilities(list(expected)) == expected

    def test_the_single_search_gives_the_same_answers(self, six):
        """Одно правило, два входа: одиночный вход сверяется с тем же названным ожиданием."""
        for key, answer in _expected(six).items():
            assert offers_by_capability(key) == answer, key

    def test_a_key_nobody_knows_is_present_in_the_answer(self, six):
        answer = offers_by_capabilities(["found", "nobody-knows"])

        assert set(answer) == {"found", "nobody-knows"}

    def test_the_viewer_decides_what_is_in_sight(self, six, persona):
        hidden = six["_hidden_offer"]

        assert offers_by_capabilities(["out-of-sight"], viewer=persona) == {
            "out-of-sight": CapabilityOffers((hidden.pk,)),
        }

    def test_repeated_keys_are_one_answer_and_an_empty_input_costs_nothing(self, six):
        assert list(offers_by_capabilities(["found", "found", "no-offer", "found"])) == ["found", "no-offer"]
        with CaptureQueriesContext(connection) as queries:
            assert offers_by_capabilities([]) == {}
        assert len(queries) == 0


class TestCapabilitiesAndCanonsManyToMany:
    def test_one_capability_on_several_canons_and_two_capabilities_on_one_canon(self, tenant, category, curator):
        first = _offer(tenant, category, curator, name="Массаж")
        second = _offer(tenant, category, curator, name="Прессотерапия")
        _can([first.template, second.template], curator, "wide")
        _can([second.template], curator, "narrow")

        answer = offers_by_capabilities(["wide", "narrow"])

        assert answer["wide"].offer_ids == tuple(sorted([first.pk, second.pk]))
        assert answer["narrow"].offer_ids == (second.pk,)

    def test_the_offers_of_one_key_do_not_leak_into_another(self, six, tenant, category, curator):
        other = _offer(tenant, category, curator, name="Маникюр")
        _can([other.template], curator, "other")

        answer = offers_by_capabilities(["found", "other", "no-offer"])

        assert set(answer["found"].offer_ids).isdisjoint(answer["other"].offer_ids)
        assert answer["no-offer"].offer_ids == ()


class TestTheCostDoesNotGrowWithTheKeys:
    def test_one_key_and_six_keys_cost_the_same(self, six):
        with CaptureQueriesContext(connection) as one:
            offers_by_capabilities(["found"])
        with CaptureQueriesContext(connection) as many:
            offers_by_capabilities(list(_expected(six)))

        assert len(many) == len(one)

    def test_keys_nobody_knows_cost_one_query(self):
        with CaptureQueriesContext(connection) as queries:
            offers_by_capabilities(["a", "b", "c"])

        assert len(queries) == 1


class TestFeasibilityIsOneCallOfAdmissionOverEverythingFound:
    def test_found_does_not_mean_admitted(self, six, tenant, settings):
        """Поиск допуск не обходит: исполнимость — отдельный вопрос, одним вызовом на все найденные id."""
        settings.BODY_CARE_UNCLASSIFIED_FAIL_CLOSED = False
        found = offers_by_capabilities(list(_expected(six)))
        ids = [offer_id for answer in found.values() for offer_id in answer.offer_ids]
        master = _master(tenant, "81")
        _does(master, SalonService.objects.get(pk=ids[0]))

        default = offer_admission(ids)
        strict = offer_admission(ids, not_enforced_is_unmet=True)

        assert default[ids[0]].verdict is OfferVerdict.OPEN
        assert strict[ids[0]].verdict is OfferVerdict.NOT_ADMITTED, "неразмеченную строку строгая свёртка не пускает"


class TestSyntheticKeysFollowTheSameRule:
    def test_under_the_grant_a_synthetic_key_is_found_next_to_a_real_one(self, chain, granted, six):
        canon, offer, _ = chain
        _capability(canon, "synthetic-key", synthetic=True)

        answer = offers_by_capabilities(["synthetic-key", "found", "nobody-knows"], viewer=granted)

        assert answer["synthetic-key"] == CapabilityOffers((offer.pk,))
        assert answer["found"] == six["found"]
        assert answer["nobody-knows"].empty_because is NoOffers.NO_CAPABILITY

    def test_without_the_grant_it_is_no_capability(self, chain, persona):
        canon, _, _ = chain
        _capability(canon, "synthetic-key", synthetic=True)

        assert offers_by_capabilities(["synthetic-key"], viewer=persona) == {
            "synthetic-key": CapabilityOffers((), NoOffers.NO_CAPABILITY),
        }
        assert offers_by_capabilities(["synthetic-key"])["synthetic-key"].empty_because is NoOffers.NO_CAPABILITY


def test_the_knowledge_reader_answers_for_every_key(tenant, category, curator):
    offer = _offer(tenant, category, curator, name="Массаж")
    _can([offer.template], curator, "known")
    _can([], curator, "attached-to-nothing")

    assert template_ids_by_capability(["known", "attached-to-nothing", "unknown"]) == {
        "known": frozenset({offer.template_id}), "attached-to-nothing": frozenset(), "unknown": frozenset(),
    }
