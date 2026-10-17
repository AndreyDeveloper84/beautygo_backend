"""DRF-2888 — допуск по предложению салона: один читатель для подбора, переписи и плана.

- ``admission_answers`` отвечает по тройкам «мастер, предложение, канон» теми
  же восемью ответами, что несёт кандидат подбора: одно правило, два входа;
- мастер ``None`` — вопрос про само предложение: проверки мастера «не
  применимы», проверки предложения отвечают;
- ``offer_admission`` различает «открыто», «некому оказать» и «не допущено»;
- продаваемых мастеров перечисляет то же правило, что у пула подбора;
  демо-салоны — по личности, а операторский режим «все салоны» назван явно;
- число запросов от размера списка не зависит.
"""
from __future__ import annotations

import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from recommendation._admission import ALL_CHECKS, AdmissionCheck, CheckOutcome
from recommendation._reason_codes import ReasonCode
from recommendation.tests.test_legal_gate_cat10_ext_2843 import (  # noqa: F401 — фикстуры
    LC,
    _does,
    _fetch,
    _license,
    _master,
    _offer,
    _peel,
    _place,
    _qualify,
    category,
    curator,
    tenant,
)
from tenants.models import Tenant
from users.admission import OfferVerdict, admission_answers, offer_admission, sellable_edges
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

A = AdmissionCheck
O = CheckOutcome  # noqa: E741


def _legal_peel(tenant, category, curator, suffix: str, *, qualified=True):  # noqa: F811
    """Пилинг, у которого сошлось всё, кроме (по желанию) квалификации мастера."""
    place = _place(tenant, curator, address=f"ул. Проверенная, {suffix}")
    master = _master(tenant, suffix, place=place)
    peel = _peel(tenant, category, curator, name=f"Пилинг {suffix}")
    _does(master, peel)
    _license_for(tenant, curator, peel, place)
    if qualified:
        _qualify(master, curator)
    return master, peel


def _license_for(tenant, curator, peel, place):  # noqa: F811
    from tenants.models import MedicalLicense

    existing = MedicalLicense.objects.filter(tenant=tenant).first()
    if existing is None:
        return _license(tenant, curator, covers=[peel.template], places=[place])
    existing.covered_templates.add(peel.template)
    existing.licensed_locations.add(place)
    return existing


class TestAnswersByTriples:
    def test_the_same_eight_answers_the_candidate_of_the_shelf_carries(self, tenant, category, curator):  # noqa: F811
        """Одно правило, два входа: подбор и читатель по тройкам отвечают одинаково."""
        master, peel = _legal_peel(tenant, category, curator, "31", qualified=False)

        [facts] = _fetch()
        by_triple = admission_answers([(master.pk, peel.pk, peel.template_id)])

        assert by_triple[(master.pk, peel.pk)] == facts.admission
        assert [a.check for a in by_triple[(master.pk, peel.pk)]] == list(ALL_CHECKS)

    def test_without_a_master_the_offer_still_answers_for_itself(self, tenant, category, curator):  # noqa: F811
        _, peel = _legal_peel(tenant, category, curator, "32")

        answers = {a.check: a.outcome for a in admission_answers([(None, peel.pk, peel.template_id)])[(None, peel.pk)]}

        assert (answers[A.ADDRESS], answers[A.QUALIFICATION]) == (O.NOT_APPLICABLE, O.NOT_APPLICABLE)
        assert answers[A.LICENSE] is O.PASSED and answers[A.LEGAL_CLASS] is O.PASSED
        assert answers[A.MAPPING] is O.PASSED

    def test_an_offer_that_is_not_in_the_base_is_not_in_the_answer(self):
        ghost = uuid.uuid4()

        assert admission_answers([(None, ghost, None)]) == {}

    def test_the_number_of_queries_does_not_grow_with_the_list(self, tenant, category, curator):  # noqa: F811
        rows = []
        for index in range(1, 6):
            master, peel = _legal_peel(tenant, category, curator, f"4{index}", qualified=index % 2 == 0)
            rows.append((master.pk, peel.pk, peel.template_id))
            rows.append((None, peel.pk, peel.template_id))

        def queries(some) -> int:
            with CaptureQueriesContext(connection) as captured:
                assert admission_answers(some)
            return len(captured)

        assert queries(rows) == queries(rows[:1])


class TestTheVerdictOnAnOffer:
    def test_open_when_a_sellable_master_passes_everything(self, tenant, category, curator):  # noqa: F811
        master, peel = _legal_peel(tenant, category, curator, "51")

        verdict = offer_admission([peel.pk])[peel.pk]

        assert verdict.verdict is OfferVerdict.OPEN
        assert set(verdict.masters) == {master.pk}
        assert verdict.unmet == ()

    def test_one_passing_master_is_enough(self, tenant, category, curator):  # noqa: F811
        _, peel = _legal_peel(tenant, category, curator, "52")
        other = _master(tenant, "53")
        _does(other, peel)

        verdict = offer_admission([peel.pk])[peel.pk]

        assert verdict.verdict is OfferVerdict.OPEN
        assert len(verdict.masters) == 2

    def test_not_admitted_when_no_master_passes_and_it_says_why(self, tenant, category, curator):  # noqa: F811
        _, peel = _legal_peel(tenant, category, curator, "54", qualified=False)

        verdict = offer_admission([peel.pk])[peel.pk]

        assert verdict.verdict is OfferVerdict.NOT_ADMITTED
        assert [(a.check, a.reason) for a in verdict.unmet] == [
            (A.QUALIFICATION, ReasonCode.ELIG_EXCLUDED_PRACTITIONER_QUALIFICATION_NOT_VERIFIED),
        ]

    def test_not_admitted_by_the_offer_itself_names_the_offer_level_reason(
        self, tenant, category, curator,  # noqa: F811
    ):
        master = _master(tenant, "55")
        peel = _peel(tenant, category, curator, name="Пилинг без лицензии")
        _does(master, peel)

        verdict = offer_admission([peel.pk])[peel.pk]

        assert verdict.verdict is OfferVerdict.NOT_ADMITTED
        assert [(a.check, a.reason) for a in verdict.unmet] == [
            (A.LICENSE, ReasonCode.ELIG_EXCLUDED_MEDICAL_LICENSE_NOT_VERIFIED),
        ]

    def test_nobody_to_do_it_is_not_the_same_as_not_admitted(self, tenant, category, curator):  # noqa: F811
        """Для человека это разные объяснения: «некому оказать» и «услуга не допущена»."""
        orphan = _offer(tenant, category, curator, name="Стрижка без мастера")

        verdict = offer_admission([orphan.pk])[orphan.pk]

        assert verdict.verdict is OfferVerdict.NO_SELLABLE_MASTER
        assert verdict.masters == {} and verdict.unmet == ()

    def test_a_master_who_is_not_sellable_does_not_open_the_offer(self, tenant, category, curator):  # noqa: F811
        master, peel = _legal_peel(tenant, category, curator, "56")
        SpecialistProfile.objects.filter(pk=master.pk).update(is_booking_enabled=False)

        assert offer_admission([peel.pk])[peel.pk].verdict is OfferVerdict.NO_SELLABLE_MASTER

    def test_a_closed_offer_is_not_admitted_even_with_nobody_to_do_it(self, tenant, category, curator):  # noqa: F811
        """Причина предложения названа первой: отсутствие мастера её не прячет."""
        unlicensed = _peel(tenant, category, curator, name="Пилинг ничей")

        assert offer_admission([unlicensed.pk])[unlicensed.pk].verdict is OfferVerdict.NOT_ADMITTED

    def test_a_diagnostic_subset_of_checks_changes_the_verdict(self, tenant, category, curator):  # noqa: F811
        _, peel = _legal_peel(tenant, category, curator, "57", qualified=False)
        without_qualification = [c for c in ALL_CHECKS if c is not A.QUALIFICATION]

        assert offer_admission([peel.pk], enabled=without_qualification)[peel.pk].verdict is OfferVerdict.OPEN
        assert offer_admission([peel.pk])[peel.pk].verdict is OfferVerdict.NOT_ADMITTED

    def test_several_offers_are_answered_in_one_call(self, tenant, category, curator):  # noqa: F811
        _, open_peel = _legal_peel(tenant, category, curator, "58")
        orphan = _offer(tenant, category, curator, name="Стрижка без мастера")

        verdicts = offer_admission([open_peel.pk, orphan.pk, uuid.uuid4()])

        assert {pk: v.verdict for pk, v in verdicts.items()} == {
            open_peel.pk: OfferVerdict.OPEN, orphan.pk: OfferVerdict.NO_SELLABLE_MASTER,
        }


class TestWhoIsASellableMaster:
    @pytest.fixture
    def demo(self, category, curator):  # noqa: F811
        salon = Tenant.objects.create(slug="adm2888-demo", name="Демо", is_active=True, is_demo=True)
        master = _master(salon, "81")
        offering = _offer(salon, category, curator, name="Демо-стрижка")
        _does(master, offering)
        return master, offering

    def test_an_ordinary_client_does_not_see_a_demo_master(self, demo):
        _, offering = demo

        assert sellable_edges([offering.pk]) == {}
        assert offer_admission([offering.pk])[offering.pk].verdict is OfferVerdict.NO_SELLABLE_MASTER

    def test_a_test_persona_does(self, demo):
        master, offering = demo
        persona = User.objects.create_user(
            username="adm2888-persona", password="x", role="client", phone="+79992888001", is_test_persona=True,
        )

        assert sellable_edges([offering.pk], viewer=persona) == {offering.pk: [master.pk]}
        assert offer_admission([offering.pk], viewer=persona)[offering.pk].verdict is OfferVerdict.OPEN

    def test_the_operator_mode_counts_demo_salons_on_par(self, demo):
        master, offering = demo

        assert sellable_edges([offering.pk], all_salons=True) == {offering.pk: [master.pk]}
        assert offer_admission([offering.pk], all_salons=True)[offering.pk].verdict is OfferVerdict.OPEN

    def test_the_operator_mode_and_a_viewer_cannot_be_named_together(self, demo):
        _, offering = demo
        client = User.objects.create_user(username="adm2888-c", password="x", role="client", phone="+79992888002")

        with pytest.raises(ValueError):
            offer_admission([offering.pk], viewer=client, all_salons=True)

    def test_a_dead_salon_has_no_sellable_masters(self, tenant, category, curator):  # noqa: F811
        master, peel = _legal_peel(tenant, category, curator, "82")
        Tenant.objects.filter(pk=tenant.pk).update(is_active=False)

        assert sellable_edges([peel.pk], all_salons=True) == {}
        assert master.pk not in offer_admission([peel.pk], all_salons=True)[peel.pk].masters
