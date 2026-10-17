"""DRF-2916, часть А — помеченная синтетика не существует для подбора и допуска.

Решение владельца 08.10: механику пути плана проверяют на помеченных
синтетических данных, и они нигде не должны выглядеть настоящими. Пока
серверного разрешения нет (оно появится в части Б и только у допуска),
синтетическая строка для моих читателей — как строка, которой нет в базе:

- в пуле подбора её нет даже у тестовой личности, которая видит демо-салон;
- в списке услуг мастера её нет ни в одном пуле каталога;
- допуск по тройкам и по предложению отвечает как на несуществующий id —
  при личности, без личности и в операторском режиме;
- поиск предложений по способности синтетическую способность не знает.

Каждому отрицательному узлу — контроль на такой же НАСТОЯЩЕЙ строке в том же
демо-салоне: иначе «не нашли» означало бы «фикстура не работает».
"""
from __future__ import annotations

import pytest

from recommendation.tests.test_legal_gate_cat10_ext_2843 import MARKETPLACE, UNSTATED  # noqa: I001
from recommendation.api import resolve
from recommendation.tests.test_legal_gate_cat10_ext_2843 import (  # noqa: F401 — фикстуры
    _does,
    _master,
    _offer as _verified_offer,
    _request,
    _shown,
    category,
    curator,
)
from services.catalog_reads import catalog_services_for
from services.models import SalonService
from services.tests.test_synthetic_test_mark import _capability, _classified
from services.tests.test_synthetic_test_mark import _offer as _salon_offer
from tenants.models import Tenant
from users.admission import admission_answers, offer_admission, sellable_edges
from users.capability_offers import NoOffers, offers_by_capability
from users.models import User
from users.recommendation_source import SpecialistCandidateSource

pytestmark = pytest.mark.django_db

KEY = "synthetic-wrap-2916"
REVIEW = SalonService.MappingStatus.REVIEW_REQUIRED


@pytest.fixture
def demo(db):
    return Tenant.objects.create(slug="syn2916-demo", name="Демо-салон", is_active=True, is_demo=True)


@pytest.fixture
def persona(db):
    return User.objects.create_user(
        username="syn2916-persona", password="x", role="client", phone="+79992916001", is_test_persona=True,
    )


def _row(demo, category, curator, name, *, synthetic):  # noqa: F811
    """Размеченный канон и услуга демо-салона на нём — синтетические или настоящие, в остальном одинаковые."""
    canon = _classified(category, curator, name, synthetic=synthetic)
    offer = _salon_offer(demo, category, synthetic=synthetic, template=canon, name=name, mapping_status=REVIEW)
    return canon, offer


@pytest.fixture
def synthetic(demo, category, curator):  # noqa: F811
    canon, offer = _row(demo, category, curator, "Синтетическое обёртывание", synthetic=True)
    master = _master(demo, "61")
    _does(master, offer)
    return canon, offer, master


@pytest.fixture
def real(demo, category, curator):  # noqa: F811
    """Настоящая услуга того же демо-салона с ПОДТВЕРЖДЁННОЙ связью — её мастер на полке виден."""
    offer = _verified_offer(demo, category, curator, name="Настоящее обёртывание")
    canon = offer.template
    master = _master(demo, "62")
    _does(master, offer)
    return canon, offer, master


def _services_in_pool(viewer, masters) -> set:
    """Строки, по которым источник подбора собирает факты мастеров пула."""
    source = SpecialistCandidateSource(viewer=viewer)
    assert len(source.fetch(scope=MARKETPLACE, need=UNSTATED)) == len(masters), "оба мастера в пуле личности"
    mapping = source._mapping_by_specialist([master.pk for master in masters])
    return {offer_id for facts in mapping.values() for offer_id in facts.legal_answers_by_service}


class TestThePoolOfTheResolver:
    def test_a_synthetic_offer_is_not_in_the_pool_even_for_a_test_persona(self, synthetic, real, persona):
        _, synthetic_offer, synthetic_master = synthetic
        _, real_offer, real_master = real

        found = _services_in_pool(persona, [synthetic_master, real_master])

        assert real_offer.pk in found, "контроль: настоящая строка того же демо-салона в пуле есть"
        assert synthetic_offer.pk not in found

    def test_a_master_with_only_a_synthetic_offer_is_never_shown(self, synthetic, real, persona):
        _, _, synthetic_master = synthetic
        _, _, real_master = real

        shown = _shown(resolve(_request(), source=SpecialistCandidateSource(viewer=persona)))

        assert str(real_master.user_id) in shown, "контроль: мастер настоящей услуги показан тестовой личности"
        assert str(synthetic_master.user_id) not in shown


class TestTheListOfServicesOfAMaster:
    """Один читатель на подбор, поиск, публичный каталог и движок чата."""

    def test_a_synthetic_service_is_not_listed(self, synthetic, real):
        _, synthetic_offer, synthetic_master = synthetic
        _, real_offer, real_master = real

        assert [row.id for row in catalog_services_for(real_master)] == [real_offer.pk]
        assert [row.id for row in catalog_services_for(synthetic_master)] == []
        assert synthetic_offer.pk not in {row.id for row in catalog_services_for(synthetic_master)}


class TestAdmission:
    def test_by_triples_a_synthetic_offer_answers_like_an_id_that_does_not_exist(self, synthetic, real):
        canon, offer, master = synthetic
        real_canon, real_offer, real_master = real

        assert admission_answers([(None, real_offer.pk, real_canon.pk)]) != {}
        assert admission_answers([(None, offer.pk, canon.pk), (master.pk, offer.pk, canon.pk)]) == {}

    @pytest.mark.parametrize("mode", ["persona", "nobody", "operator"])
    def test_by_offer_it_is_absent_for_every_caller(self, synthetic, real, persona, mode):
        _, offer, _ = synthetic
        _, real_offer, _ = real
        kwargs = {"persona": {"viewer": persona}, "nobody": {}, "operator": {"all_salons": True}}[mode]

        answer = offer_admission([offer.pk, real_offer.pk], **kwargs)

        assert set(answer) == {real_offer.pk}

    def test_its_master_is_not_a_sellable_edge(self, synthetic, real, persona):
        _, offer, _ = synthetic
        _, real_offer, real_master = real

        edges = sellable_edges([offer.pk, real_offer.pk], viewer=persona)

        assert edges == {real_offer.pk: [real_master.pk]}


class TestTheSearchByCapability:
    def test_a_synthetic_capability_is_no_capability(self, synthetic, persona):
        """Не OUT_OF_SIGHT: существование синтетики не раскрывается (просьба окна Plan)."""
        canon, _, _ = synthetic
        _capability(canon, KEY, synthetic=True)

        assert offers_by_capability(KEY).empty_because is NoOffers.NO_CAPABILITY
        assert offers_by_capability(KEY, viewer=persona).empty_because is NoOffers.NO_CAPABILITY

    def test_the_same_real_capability_is_found(self, real, curator, persona):  # noqa: F811
        canon, offer, _ = real
        _capability(canon, KEY, synthetic=False, curator=curator)

        assert offers_by_capability(KEY, viewer=persona).offer_ids == (offer.pk,)
