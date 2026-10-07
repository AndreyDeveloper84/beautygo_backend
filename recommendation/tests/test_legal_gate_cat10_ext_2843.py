"""CAT-10-ext (DRF-2843) — юридические условия §7A в подборе, мимо состояния CAT-6.

Подтверждённый медицинский класс у канона ВНЕ семейства body-care (пилинги
лица) состояние CAT-6 не видит: для него это ``not_subject``, и CAT-10 такую
строку пропускает. Закрывает её этот гейт — по классу, а не по семейству:

- лицензия салона (§7A-2), адрес мастера (§7A-3), квалификация мастера
  (§7A-4) читаются у настоящих функций §7A, по одному пакету на пул;
- исключение называет первое несошедшееся условие своим кодом;
- условия мастера — по мастеру: одну услугу салона один мастер открывает,
  другой нет;
- каталог без медицинского класса гейт не задевает;
- неопределённость (ключа нет, сбой чтения) закрывает и пишет ERROR.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from recommendation._pipeline import resolve
from recommendation._reason_codes import EXCLUSION_CODES, GATE_EXCLUSION_CODES, ReasonCode
from recommendation._stages import StagePolicy, apply_eligibility
from recommendation._types import (
    ConfigGate,
    LegalGate,
    NeedOrigin,
    NeedSpec,
    RecommendationRequest,
    SafetyState,
    Scope,
    ScopeMode,
    Surface,
)
from recommendation.tests.conftest import make_facts
from services.body_care_qualification import QualificationState
from services.models import SalonService, ServiceCategory, ServiceTemplate, SpecialistService
from tenants.models import MedicalLicense, ServiceLocation, Tenant
from users import recommendation_source
from users.models import PractitionerQualification, SpecialistProfile, User
from users.recommendation_source import SpecialistCandidateSource, _MappingFacts, legal_gates

pytestmark = pytest.mark.django_db

LC = ServiceTemplate.LegalServiceClass
PC = ServiceTemplate.PractitionerClass

MARKETPLACE = Scope(ScopeMode.MARKETPLACE)
UNSTATED = NeedSpec(origin=NeedOrigin.MEMORY)


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="cat10ext", name="Клиника", is_active=True)


@pytest.fixture
def curator(db):
    return User.objects.create_user(username="curator-2843", password="x", role="client", phone="+79995843000")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(slug="cat10ext-face", name="Уход за лицом 2843")


@pytest.fixture
def source_log(caplog):
    """У ``users`` в настройках ``propagate=False`` — перехватчик вешается на сам логгер."""
    source_logger = logging.getLogger(recommendation_source.__name__)
    source_logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        source_logger.removeHandler(caplog.handler)


def _errors(log) -> list[str]:
    return [r.getMessage() for r in log.records if r.levelno >= logging.ERROR]


def _place(tenant, curator, address="ул. Проверенная, 1", confirmed=True) -> ServiceLocation:
    if not confirmed:
        return ServiceLocation.objects.create(tenant=tenant, address=address)
    return ServiceLocation.objects.create(
        tenant=tenant, address=address, status="confirmed",
        confirmed_by=curator, confirmed_at=timezone.now(), confirmed_source_ref="договор аренды",
    )


def _master(tenant, suffix: str, place=None) -> SpecialistProfile:
    user = User.objects.create_user(
        username=f"cat10ext-{suffix}", password="x", role="specialist", phone=f"+79995843{suffix}",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = tenant
    profile.display_name = f"Мастер {suffix}"
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.works_at = place
    profile.save()
    return profile


def _offer(tenant, category, curator, *, name, legal_class=None, required=None) -> SalonService:
    """Услуга ВНЕ body-care (семейства нет) с VERIFIED-связью: пилинг лица, стрижка."""
    template = ServiceTemplate.objects.create(
        category=category, name=f"{name} канон {uuid.uuid4().hex[:6]}", name_short=name[:20], duration_default=60,
    )
    fields = {}
    if legal_class is not None:
        fields.update(
            legal_service_class=legal_class, legal_class_confirmed_by=curator,
            legal_class_confirmed_at=timezone.now(), legal_class_source_ref="решение юриста",
        )
    if required is not None:
        fields.update(
            required_practitioner_class=required, practitioner_class_confirmed_by=curator,
            practitioner_class_confirmed_at=timezone.now(), practitioner_class_source_ref="решение клиники",
        )
    if fields:
        ServiceTemplate.objects.filter(pk=template.pk).update(**fields)
    return SalonService.objects.create(
        tenant=tenant, name=name, category=category, template=template, duration_minutes=60, is_active=True,
        base_price=Decimal("3000"), mapping_status=SalonService.MappingStatus.VERIFIED,
        mapping_confirmed_rule="test_fixture", mapping_rule_version="1.0.0",
        mapping_confirmed_at=timezone.now(), mapping_source_ref="fixture:2843",
    )


def _does(master, offering) -> None:
    SpecialistService.objects.create(
        specialist=master, salon_service=offering, tenant=offering.tenant, price=Decimal("2000"), is_active=True,
    )


def _license(tenant, curator, *, covers=(), places=()) -> MedicalLicense:
    lic = MedicalLicense.objects.create(tenant=tenant, license_ref="ЛО-00-00-002843")
    MedicalLicense.objects.filter(pk=lic.pk).update(
        verified_by=curator, verified_at=timezone.now(), verification_source_ref="скан сверен",
    )
    lic.covered_templates.set(covers)
    lic.licensed_locations.set(places)
    return lic


def _qualify(master, curator, cls=PC.PHYSICIAN_COSMETOLOGIST) -> None:
    q = PractitionerQualification.objects.create(specialist=master, practitioner_class=cls)
    PractitionerQualification.objects.filter(pk=q.pk).update(
        verified_by=curator, verified_at=timezone.now(), verification_source_ref="диплом сверен",
    )


def _peel(tenant, category, curator, *, name="Пилинг лица", required=PC.PHYSICIAN_COSMETOLOGIST) -> SalonService:
    return _offer(tenant, category, curator, name=name, legal_class=LC.MEDICAL_COSMETOLOGY, required=required)


def _fetch(need=UNSTATED):
    return SpecialistCandidateSource().fetch(scope=MARKETPLACE, need=need)


def _named(text: str) -> NeedSpec:
    return NeedSpec(origin=NeedOrigin.USER_EXPLICIT, raw_text=text)


def _request(need=UNSTATED) -> RecommendationRequest:
    return RecommendationRequest(
        request_id=str(uuid.uuid4()), subject_ref="subj-2843", surface=Surface.MINIAPP_HOME,
        scope=MARKETPLACE, need=need, safety_state=SafetyState.NOT_APPLICABLE,
        tie_break_seed="seed-2843", k=10,
    )


def _resolve(need=UNSTATED):
    return resolve(_request(need), source=SpecialistCandidateSource())


def _shown(decision) -> set[str]:
    return {str(c.candidate_ref.id) for c in decision.ordered}


def _excluded(decision) -> dict[str, ReasonCode]:
    return {str(e.candidate_ref.id): e.reason_code for e in decision.excluded}


class TestTheLadderOfConditions:
    """Один мастер, один пилинг; условия добавляются по одному — код называет следующее."""

    def test_a_confirmed_medical_class_without_a_licence_is_excluded(self, tenant, category, curator):
        master = _master(tenant, "01")
        _does(master, _peel(tenant, category, curator))

        [facts] = _fetch()

        assert facts.config_gate is ConfigGate.NOT_SUBJECT, "контроль: CAT-6 считает строку вне Body Care и пропускает"
        assert facts.legal_gate is LegalGate.LICENSE_NOT_VERIFIED
        decision = _resolve()
        assert _excluded(decision) == {str(master.user_id): ReasonCode.ELIG_EXCLUDED_MEDICAL_LICENSE_NOT_VERIFIED}
        assert ReasonCode.ELIG_EXCLUDED_MEDICAL_LICENSE_NOT_VERIFIED in decision.reason_codes

    def test_a_licence_that_does_not_cover_the_canon(self, tenant, category, curator):
        master = _master(tenant, "02")
        _does(master, _peel(tenant, category, curator))
        other = _peel(tenant, category, curator, name="Другая процедура")
        _license(tenant, curator, covers=[other.template])

        assert _excluded(_resolve()) == {str(master.user_id): ReasonCode.ELIG_EXCLUDED_LICENSE_SCOPE_MISMATCH}

    @pytest.mark.parametrize("place", ["none", "unconfirmed"])
    def test_a_master_whose_place_is_unknown(self, tenant, category, curator, place):
        works_at = None if place == "none" else _place(tenant, curator, confirmed=False)
        master = _master(tenant, "03", place=works_at)
        peel = _peel(tenant, category, curator)
        _does(master, peel)
        _license(tenant, curator, covers=[peel.template])

        [facts] = _fetch()

        assert facts.legal_gate is LegalGate.LOCATION_UNKNOWN
        assert _excluded(_resolve()) == {str(master.user_id): ReasonCode.ELIG_EXCLUDED_MASTER_LOCATION_UNKNOWN}

    def test_a_master_who_works_at_an_address_outside_the_licence(self, tenant, category, curator):
        licensed = _place(tenant, curator)
        elsewhere = _place(tenant, curator, address="ул. Другая, 2")
        master = _master(tenant, "04", place=elsewhere)
        peel = _peel(tenant, category, curator)
        _does(master, peel)
        _license(tenant, curator, covers=[peel.template], places=[licensed])

        assert _excluded(_resolve()) == {str(master.user_id): ReasonCode.ELIG_EXCLUDED_LICENSE_ADDRESS_MISMATCH}

    def test_a_medical_canon_whose_qualification_requirement_nobody_confirmed(self, tenant, category, curator):
        place = _place(tenant, curator)
        master = _master(tenant, "05", place=place)
        peel = _peel(tenant, category, curator, required=None)
        _does(master, peel)
        _license(tenant, curator, covers=[peel.template], places=[place])

        [facts] = _fetch()

        assert facts.legal_gate is LegalGate.QUALIFICATION_REQUIREMENT_UNCONFIRMED
        assert _excluded(_resolve()) == {
            str(master.user_id): ReasonCode.ELIG_EXCLUDED_QUALIFICATION_REQUIREMENT_UNCONFIRMED,
        }

    def test_a_master_without_the_required_qualification(self, tenant, category, curator):
        place = _place(tenant, curator)
        master = _master(tenant, "06", place=place)
        peel = _peel(tenant, category, curator)
        _does(master, peel)
        _license(tenant, curator, covers=[peel.template], places=[place])
        _qualify(master, curator, cls=PC.NURSE_COSMETOLOGY)

        assert _excluded(_resolve()) == {
            str(master.user_id): ReasonCode.ELIG_EXCLUDED_PRACTITIONER_QUALIFICATION_NOT_VERIFIED,
        }

    def test_everything_in_place_admits(self, tenant, category, curator):
        place = _place(tenant, curator)
        master = _master(tenant, "07", place=place)
        peel = _peel(tenant, category, curator)
        _does(master, peel)
        _license(tenant, curator, covers=[peel.template], places=[place])
        _qualify(master, curator)

        [facts] = _fetch()

        assert facts.legal_gate is LegalGate.CLEARED
        assert _shown(_resolve()) == {str(master.user_id)}


class TestTheRestOfTheCatalog:
    def test_an_offer_with_no_class_and_no_family_is_untouched(self, tenant, category, curator):
        """Положительный контроль на весь каталог: стрижка проходит, даже рядом с закрытым пилингом."""
        hairdresser, doctor = _master(tenant, "08"), _master(tenant, "09")
        _does(hairdresser, _offer(tenant, category, curator, name="Стрижка"))
        _does(doctor, _peel(tenant, category, curator))

        gates = {str(f.ref.id): f.legal_gate for f in _fetch()}

        assert gates == {
            str(hairdresser.user_id): LegalGate.CLEARED,
            str(doctor.user_id): LegalGate.LICENSE_NOT_VERIFIED,
        }
        assert _shown(_resolve()) == {str(hairdresser.user_id)}

    def test_a_confirmed_non_medical_class_is_untouched(self, tenant, category, curator):
        master = _master(tenant, "10")
        _does(master, _offer(tenant, category, curator, name="Маска", legal_class=LC.NON_MEDICAL_COSMETIC))

        assert _shown(_resolve()) == {str(master.user_id)}

    def test_a_class_sent_to_legal_review_does_not_open(self, tenant, category, curator):
        master = _master(tenant, "11")
        _does(master, _offer(tenant, category, curator, name="Спорная", legal_class=LC.LEGAL_REVIEW_REQUIRED))

        assert _excluded(_resolve()) == {str(master.user_id): ReasonCode.ELIG_EXCLUDED_LEGAL_CLASS_UNCONFIRMED}


class TestTheConditionsOfTheMasterAreTheMasters:
    def test_one_offer_two_masters_only_the_qualified_one_is_shown(self, tenant, category, curator):
        place = _place(tenant, curator)
        doctor, assistant = _master(tenant, "12", place=place), _master(tenant, "13", place=place)
        peel = _peel(tenant, category, curator)
        _does(doctor, peel)
        _does(assistant, peel)
        _license(tenant, curator, covers=[peel.template], places=[place])
        _qualify(doctor, curator)

        decision = _resolve()

        assert _shown(decision) == {str(doctor.user_id)}
        assert _excluded(decision) == {
            str(assistant.user_id): ReasonCode.ELIG_EXCLUDED_PRACTITIONER_QUALIFICATION_NOT_VERIFIED,
        }

    def test_an_open_offer_does_not_admit_a_master_matched_by_a_closed_one(self, tenant, category, curator):
        master = _master(tenant, "14")
        peel = _peel(tenant, category, curator, name="Пилинг химический")
        _does(master, peel)
        _does(master, _offer(tenant, category, curator, name="Стрижка"))

        [facts] = _fetch(_named("пилинг химический"))

        assert facts.matched_service_ref == peel.pk
        assert facts.legal_gate is LegalGate.LICENSE_NOT_VERIFIED

    def test_of_two_offers_that_both_answer_the_need_the_open_one_is_taken(self, tenant, category, curator):
        master = _master(tenant, "15")
        _does(master, _peel(tenant, category, curator, name="Пилинг медицинский"))
        open_one = _offer(tenant, category, curator, name="Пилинг косметический")
        _does(master, open_one)

        [facts] = _fetch(_named("пилинг"))

        assert facts.matched_service_ref == open_one.pk
        assert facts.legal_gate is LegalGate.CLEARED

    def test_with_no_need_stated_one_open_offer_is_enough(self, tenant, category, curator):
        master = _master(tenant, "16")
        _does(master, _peel(tenant, category, curator))
        _does(master, _offer(tenant, category, curator, name="Стрижка"))

        assert _shown(_resolve()) == {str(master.user_id)}


class TestOneRowAnswersForBothGates:
    """Готовая без лицензии и лицензированная неготовая вместе мастера не открывают."""

    def _facts(self, rows) -> _MappingFacts:
        mapping = _MappingFacts()
        mapping.has_service = True
        for pk, (config, legal) in rows.items():
            mapping.status_by_service[pk] = "verified"
            mapping.config_by_service[pk] = config
            mapping.legal_by_service[pk] = legal
        return mapping

    def test_two_half_open_rows_do_not_make_an_open_master(self):
        ready_unlicensed, licensed_unready = uuid.uuid4(), uuid.uuid4()
        mapping = self._facts({
            ready_unlicensed: (ConfigGate.READY, LegalGate.LICENSE_NOT_VERIFIED),
            licensed_unready: (ConfigGate.NOT_READY, LegalGate.CLEARED),
        })

        admitted = mapping.config_gate() is not ConfigGate.NOT_READY and mapping.legal_gate() is LegalGate.CLEARED

        assert admitted is False

    def test_both_answers_come_from_the_same_row(self):
        """Какая бы строка ни отвечала, готовность и юридические условия — её собственные."""
        ready_unlicensed, licensed_unready = uuid.uuid4(), uuid.uuid4()
        rows = {
            ready_unlicensed: (ConfigGate.READY, LegalGate.LICENSE_NOT_VERIFIED),
            licensed_unready: (ConfigGate.NOT_READY, LegalGate.CLEARED),
        }
        mapping = self._facts(rows)

        assert (mapping.config_gate(), mapping.legal_gate()) in set(rows.values())

    def test_positive_control_one_fully_open_row_among_closed_ones_opens(self):
        open_row = uuid.uuid4()
        mapping = self._facts({
            uuid.uuid4(): (ConfigGate.READY, LegalGate.LICENSE_NOT_VERIFIED),
            open_row: (ConfigGate.NOT_SUBJECT, LegalGate.CLEARED),
            uuid.uuid4(): (ConfigGate.NOT_READY, LegalGate.CLEARED),
        })

        assert mapping.config_gate() is ConfigGate.NOT_SUBJECT
        assert mapping.legal_gate() is LegalGate.CLEARED

    def test_a_legacy_row_has_no_gate(self):
        mapping = _MappingFacts()

        assert mapping.legal_gate(has_offer=True, matched_service_ref=uuid.uuid4()) is None


class TestTheResolverHalf:
    def _admit(self, *facts):
        return apply_eligibility(list(facts), _request(), StagePolicy())

    @pytest.mark.parametrize("gate", [None, LegalGate.CLEARED])
    def test_cleared_or_silent_is_admitted(self, gate):
        candidate = make_facts(legal_gate=gate)

        assert [f.ref.id for f in self._admit(candidate).admitted] == [candidate.ref.id]

    @pytest.mark.parametrize("gate", [g for g in LegalGate if g is not LegalGate.CLEARED])
    def test_every_other_value_excludes_with_a_legal_code(self, gate):
        """По всему перечислению: новое значение гейта не может оказаться открытым по недосмотру."""
        result = self._admit(make_facts(legal_gate=gate))

        assert result.admitted == ()
        [exclusion] = result.excluded
        assert exclusion.reason_code in GATE_EXCLUSION_CODES

    def test_the_named_violations_keep_their_own_codes(self):
        codes = {
            gate: self._admit(make_facts(legal_gate=gate)).excluded[0].reason_code
            for gate in LegalGate if gate is not LegalGate.CLEARED
        }

        assert codes == {
            LegalGate.LICENSE_NOT_VERIFIED: ReasonCode.ELIG_EXCLUDED_MEDICAL_LICENSE_NOT_VERIFIED,
            LegalGate.LICENSE_SCOPE_MISMATCH: ReasonCode.ELIG_EXCLUDED_LICENSE_SCOPE_MISMATCH,
            LegalGate.ADDRESS_MISMATCH: ReasonCode.ELIG_EXCLUDED_LICENSE_ADDRESS_MISMATCH,
            LegalGate.QUALIFICATION_NOT_VERIFIED: ReasonCode.ELIG_EXCLUDED_PRACTITIONER_QUALIFICATION_NOT_VERIFIED,
            LegalGate.CLASS_UNCONFIRMED: ReasonCode.ELIG_EXCLUDED_LEGAL_CLASS_UNCONFIRMED,
            LegalGate.LOCATION_UNKNOWN: ReasonCode.ELIG_EXCLUDED_MASTER_LOCATION_UNKNOWN,
            LegalGate.QUALIFICATION_REQUIREMENT_UNCONFIRMED:
                ReasonCode.ELIG_EXCLUDED_QUALIFICATION_REQUIREMENT_UNCONFIRMED,
            LegalGate.UNDETERMINED: ReasonCode.ELIG_EXCLUDED_ELIGIBILITY_UNDETERMINED,
        }

    def test_readiness_is_checked_first_and_codes_do_not_mix(self):
        result = self._admit(make_facts(config_gate=ConfigGate.NOT_READY, legal_gate=LegalGate.LICENSE_NOT_VERIFIED))

        assert [e.reason_code for e in result.excluded] == [ReasonCode.ELIG_EXCLUDED_CONFIG_NOT_READY]

    def test_the_legal_codes_are_exclusion_codes(self):
        assert GATE_EXCLUSION_CODES <= EXCLUSION_CODES


class TestTheSeam:
    def _patch(self, monkeypatch, *, licenses=None, addresses=None, qualifications=None):
        monkeypatch.setattr(recommendation_source, "license_states", lambda ids: licenses or {})
        monkeypatch.setattr(recommendation_source, "address_states", lambda pairs: addresses or {})
        monkeypatch.setattr(recommendation_source, "qualification_states", lambda pairs: qualifications or {})

    def test_the_first_unmet_condition_names_the_gate(self, monkeypatch):
        master, offer, canon = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self._patch(
            monkeypatch,
            licenses={offer: "verified"},
            addresses={(master, offer): "address_mismatch"},
            qualifications={(master, canon): QualificationState("not_verified")},
        )

        assert legal_gates([(master, offer, canon)]) == {(master, offer): LegalGate.ADDRESS_MISMATCH}

    @pytest.mark.parametrize(
        "answers",
        [
            pytest.param({}, id="every-key-is-missing"),
            pytest.param({"licenses": "approved"}, id="an-unknown-licence-value"),
            pytest.param({"licenses": "verified", "addresses": "no_covering_license"}, id="two-reads-disagree"),
            pytest.param({"licenses": "verified", "addresses": "verified"}, id="the-qualification-is-missing"),
        ],
    )
    def test_an_undetermined_answer_closes_the_row_and_is_loud(self, monkeypatch, source_log, answers):
        master, offer, canon = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self._patch(
            monkeypatch,
            licenses={offer: answers["licenses"]} if "licenses" in answers else None,
            addresses={(master, offer): answers["addresses"]} if "addresses" in answers else None,
        )

        gates = legal_gates([(master, offer, canon)])

        assert gates == {(master, offer): LegalGate.UNDETERMINED}
        [message] = _errors(source_log)
        assert "legal_gates_undetermined" in message
        assert f"{master.hex}:{offer.hex}" in message, "адрес строки — без дефисов: так его не режет маска карт"

    @pytest.mark.parametrize(
        ("silent", "expected"),
        [
            pytest.param("licenses", LegalGate.UNDETERMINED, id="only-the-licence-is-missing"),
            pytest.param("addresses", LegalGate.UNDETERMINED, id="only-the-address-is-missing"),
            pytest.param("qualifications", LegalGate.UNDETERMINED, id="only-the-qualification-is-missing"),
            pytest.param(None, LegalGate.CLEARED, id="control-nothing-is-missing"),
        ],
    )
    def test_each_read_closes_the_row_on_its_own(self, monkeypatch, silent, expected):
        """Два чтения ответили «открыто», третье промолчало: молчание одного закрывает."""
        master, offer, canon = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        answers = {
            "licenses": {offer: "verified"},
            "addresses": {(master, offer): "verified"},
            "qualifications": {(master, canon): QualificationState("verified")},
        }
        answers.pop(silent, None)
        self._patch(monkeypatch, **answers)

        assert legal_gates([(master, offer, canon)]) == {(master, offer): expected}

    @pytest.mark.parametrize(
        ("read", "answer", "expected"),
        [
            ("licenses", "class_unconfirmed", LegalGate.CLASS_UNCONFIRMED),
            ("licenses", "not_verified", LegalGate.LICENSE_NOT_VERIFIED),
            ("licenses", "scope_mismatch", LegalGate.LICENSE_SCOPE_MISMATCH),
            ("licenses", "approved", LegalGate.UNDETERMINED),
            ("addresses", "class_unconfirmed", LegalGate.CLASS_UNCONFIRMED),
            ("addresses", "location_unknown", LegalGate.LOCATION_UNKNOWN),
            ("addresses", "address_mismatch", LegalGate.ADDRESS_MISMATCH),
            ("addresses", "no_covering_license", LegalGate.UNDETERMINED),
            ("qualifications", "requirement_unconfirmed", LegalGate.QUALIFICATION_REQUIREMENT_UNCONFIRMED),
            ("qualifications", "not_verified", LegalGate.QUALIFICATION_NOT_VERIFIED),
            ("qualifications", "approved", LegalGate.UNDETERMINED),
        ],
    )
    def test_each_refusal_of_one_read_closes_while_the_other_two_are_open(self, monkeypatch, read, answer, expected):
        """Каждый отказ каждой функции — отдельно: два других чтения при этом открыты."""
        master, offer, canon = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        answers = {
            "licenses": {offer: "verified"},
            "addresses": {(master, offer): "verified"},
            "qualifications": {(master, canon): QualificationState("verified")},
        }
        key = next(iter(answers[read]))
        answers[read] = {key: QualificationState(answer) if read == "qualifications" else answer}
        self._patch(monkeypatch, **answers)

        assert legal_gates([(master, offer, canon)]) == {(master, offer): expected}

    def test_a_known_refusal_is_not_an_error(self, monkeypatch, source_log):
        master, offer, canon = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self._patch(monkeypatch, licenses={offer: "not_verified"})

        assert legal_gates([(master, offer, canon)]) == {(master, offer): LegalGate.LICENSE_NOT_VERIFIED}
        assert _errors(source_log) == []

    def test_a_row_without_a_canon_does_not_pass_for_lack_of_one(self, monkeypatch, source_log):
        """Канона нет — класса нет: квалификацию спросить нечем, и это не «условие снято»."""
        master, offer = uuid.uuid4(), uuid.uuid4()
        self._patch(monkeypatch, licenses={offer: "not_required"}, addresses={(master, offer): "not_required"})

        assert legal_gates([(master, offer, None)]) == {(master, offer): LegalGate.CLASS_UNCONFIRMED}
        assert _errors(source_log) == [], "состояние данных, не дефект чтения"

    def test_a_failing_read_closes_the_rows_instead_of_dropping_the_shelf(self, monkeypatch, source_log):
        def broken(ids):
            raise RuntimeError("§7A сломан")

        master, offer = uuid.uuid4(), uuid.uuid4()
        monkeypatch.setattr(recommendation_source, "license_states", broken)

        assert legal_gates([(master, offer, uuid.uuid4())]) == {(master, offer): LegalGate.UNDETERMINED}
        [message] = _errors(source_log)
        assert "legal_gates_failed" in message

    def test_the_source_uses_the_7a_functions_themselves(self):
        from services import body_care_address, body_care_license, body_care_qualification

        assert recommendation_source.license_states is body_care_license.license_states
        assert recommendation_source.address_states is body_care_address.address_states
        assert recommendation_source.qualification_states is body_care_qualification.qualification_states

    def test_every_answer_of_the_7a_functions_is_mapped(self):
        """Новое состояние §7A не может молча стать «не определено»: карта покрывает перечни функций."""
        from services import body_care_address, body_care_license, body_care_qualification

        assert set(recommendation_source._LICENSE_GATE) == set(body_care_license.LICENSE_STATES)
        assert set(recommendation_source._ADDRESS_GATE) == (
            set(body_care_address.ADDRESS_STATES) - {body_care_address.NO_COVERING_LICENSE}
        )
        assert set(recommendation_source._QUALIFICATION_GATE) == set(body_care_qualification.QUALIFICATION_STATES)


class TestTheReadIsBatched:
    def test_the_number_of_queries_does_not_grow_with_the_pool(self, tenant, category, curator):
        def queries() -> int:
            with CaptureQueriesContext(connection) as captured:
                assert _fetch()
            return len(captured)

        place = _place(tenant, curator)
        _does(_master(tenant, "20", place=place), _peel(tenant, category, curator, name="Пилинг 0"))
        for_one = queries()

        for index in range(1, 5):
            master = _master(tenant, f"2{index}", place=place)
            _does(master, _peel(tenant, category, curator, name=f"Пилинг {index}"))
            _does(master, _offer(tenant, category, curator, name=f"Стрижка {index}"))

        assert queries() == for_one
