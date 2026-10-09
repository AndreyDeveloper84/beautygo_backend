# flake8: noqa: F811
"""DRF-2916, часть Б — допуск помеченной синтетики: одно исключение и его границы.

Решение владельца 08.10 и его границы (``docs/AYLA_PLAN_SYNTHETIC_VALIDATION_DECISION_2026-10-08.md``):

1. «просьба вызывающего» — не поле запроса: право выдаёт сервер по личности;
2. вся цепочка тестовая — салон, мастер, предложение;
3. исключение — ТОЛЬКО подтверждение связи; связь с каноном существует;
   прочие проверки настоящие, «неизвестно» — не разрешение;
4. исход отдельный и читаемый — не «пройдена»;
5. изоляция: выключен флаг / нет пометки / нет разрешения / чужая личность /
   прямой запрос — ветка не срабатывает; обычный подбор синтетику не видит.

Узлы названы по этим границам. Положительный узел один; остальные — о том,
когда ветка НЕ срабатывает.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from django.utils import timezone

from recommendation._admission import (
    ALL_CHECKS,
    SYNTHETIC_OUTCOME_LABEL,
    AdmissionCheck,
    CheckOutcome,
    build_answers,
    first_unmet,
)
from recommendation._reason_codes import ReasonCode
from recommendation._types import ConfigGate, LegalGate, MappingStatus
from recommendation.api import resolve
from recommendation.tests.test_legal_gate_cat10_ext_2843 import (  # noqa: F401 — фикстуры
    _does,
    _master,
    _request,
    _shown,
    category,
    curator,
)
from recommendation.tests.test_synthetic_rows_are_not_real_2916 import (  # noqa: F401 — фикстуры
    REVIEW,
    demo,
    persona,
    real,
    synthetic,
)
from services.models import SalonService, ServiceTemplate
from services.synthetic import SYNTHETIC_RULE, grant_for
from services.tests.test_synthetic_test_mark import _canon, _capability
from services.tests.test_synthetic_test_mark import _offer as _salon_offer
from tenants.models import Tenant
from users.admission import OfferVerdict, admission_answers, offer_admission
from users.capability_offers import NoOffers, offers_by_capability
from users.models import SpecialistProfile, User
from users.recommendation_source import SpecialistCandidateSource

pytestmark = pytest.mark.django_db

A = AdmissionCheck
O = CheckOutcome  # noqa: E741
KEY = "synthetic-wrap-2916b"
THE_OTHER_SEVEN = tuple(check for check in ALL_CHECKS if check is not A.MAPPING)


@pytest.fixture
def granted(settings, persona):  # noqa: F811
    """Все три серверных условия: флаг стенда, субъект в списке, тестовая личность."""
    settings.SYNTHETIC_TEST_DATA_ENABLED = True
    settings.SYNTHETIC_TEST_SUBJECT_IDS = [str(persona.pk)]
    assert grant_for(persona) is not None
    return persona


def _by_check(answers) -> dict[AdmissionCheck, object]:
    return {answer.check: answer for answer in answers}


def _outside_body_care(demo, category, curator, name, *, synthetic):  # noqa: F811
    """Размеченный канон ВНЕ Body Care и услуга демо-салона на нём.

    Так выглядит цепочка, которая проходит остальные семь проверок честно:
    область и немедицинский класс подтверждены, готовность конфигурации Body
    Care к ней не относится. Основание — как требует база: у синтетики
    область подтверждает правило, у настоящей строки — человек.
    """
    scope = (
        {"scope_confirmed_rule": SYNTHETIC_RULE, "scope_rule_version": "1"} if synthetic
        else {"scope_confirmed_by": curator}
    )
    canon = _canon(
        category, name, synthetic=synthetic,
        body_care_scope=ServiceTemplate.BodyCareScope.NOT_BODY_CARE,
        scope_confirmed_at=timezone.now(), scope_source_ref="тестовый набор", **scope,
        legal_service_class=ServiceTemplate.LegalServiceClass.NON_MEDICAL_COSMETIC,
        legal_class_confirmed_by=curator, legal_class_confirmed_at=timezone.now(),
        legal_class_source_ref=SYNTHETIC_RULE if synthetic else "заключение юриста",
    )
    offer = _salon_offer(demo, category, synthetic=synthetic, template=canon, name=name, mapping_status=REVIEW)
    return canon, offer


@pytest.fixture
def chain(demo, category, curator):  # noqa: F811
    """Синтетическая цепочка, у которой сходится всё, кроме подтверждения связи."""
    canon, offer = _outside_body_care(demo, category, curator, "Синтетический уход", synthetic=True)
    master = _master(demo, "71")
    _does(master, offer)
    return canon, offer, master


# -- положительный узел -------------------------------------------------------


class TestTheOneException:
    def test_under_the_grant_the_offer_is_open_and_visibly_synthetic(self, chain, granted):
        _, offer, master = chain

        answer = offer_admission([offer.pk], viewer=granted, not_enforced_is_unmet=True)[offer.pk]

        mapping = _by_check(answer.offer)[A.MAPPING]
        assert (mapping.outcome, mapping.reason, mapping.catalog_answer) == (O.SYNTHETIC, None, "REVIEW_REQUIRED")
        assert answer.synthetic is True
        assert answer.verdict is OfferVerdict.OPEN, [(a.check, a.outcome, a.reason) for a in answer.offer]
        assert set(answer.masters) == {master.pk}
        assert _by_check(answer.masters[master.pk])[A.MAPPING].outcome is O.SYNTHETIC

    def test_the_outcome_is_never_passed_and_has_a_name_for_a_person(self, chain, granted):
        _, offer, _ = chain

        answer = offer_admission([offer.pk], viewer=granted)[offer.pk]

        assert O.PASSED is not _by_check(answer.offer)[A.MAPPING].outcome
        assert O.SYNTHETIC.value == "SYNTHETIC"
        assert "синтетические данные" in SYNTHETIC_OUTCOME_LABEL and "для теста" in SYNTHETIC_OUTCOME_LABEL

    def test_the_other_seven_checks_answer_exactly_as_for_a_real_twin(
        self, chain, granted, demo, category, curator,  # noqa: F811
    ):
        """Граница 3: у настоящей строки с той же разметкой остальные семь ответов те же — ни один не ослаблен."""
        _, offer, master = chain
        _, twin = _outside_body_care(demo, category, curator, "Настоящий близнец", synthetic=False)
        _does(master, twin)

        answers = admission_answers(
            [(master.pk, offer.pk, offer.template_id), (master.pk, twin.pk, twin.template_id)], viewer=granted,
        )
        of_synthetic = _by_check(answers[(master.pk, offer.pk)])
        of_twin = _by_check(answers[(master.pk, twin.pk)])

        for check in THE_OTHER_SEVEN:
            assert (of_synthetic[check].outcome, of_synthetic[check].reason) == (
                of_twin[check].outcome, of_twin[check].reason,
            ), check
        assert of_twin[A.MAPPING].outcome is O.FAILED, "настоящей строке без подтверждения связи разрешение не помогает"


# -- граница 3: исключение только для подтверждения связи ----------------------


class TestOnlyTheConfirmationOfTheMapping:
    @pytest.mark.parametrize("status", [MappingStatus.UNMAPPED, MappingStatus.NOT_RECOMMENDABLE, MappingStatus.UNKNOWN])
    def test_a_synthetic_row_that_is_not_linked_fails_as_usual(self, status):
        answers = _by_check(build_answers(
            mapping_status=status, canon_retired=False, config_gate=ConfigGate.NOT_SUBJECT,
            license_gate=LegalGate.CLEARED, address_gate=LegalGate.CLEARED, qualification_gate=LegalGate.CLEARED,
            synthetic=True,
        ))

        assert (answers[A.MAPPING].outcome, answers[A.MAPPING].reason) == (
            O.FAILED, ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE,
        )

    def test_a_synthetic_row_without_a_canon_fails_as_usual(self):
        answers = _by_check(build_answers(
            mapping_status=MappingStatus.REVIEW_REQUIRED, canon_retired=None, config_gate=None,
            license_gate=None, address_gate=None, qualification_gate=None, has_canon=False, synthetic=True,
        ))

        assert answers[A.MAPPING].outcome is O.FAILED

    def test_in_the_database_too_a_synthetic_offer_that_is_not_linked_is_refused(
        self, granted, demo, category, curator,  # noqa: F811
    ):
        canon, _ = _outside_body_care(demo, category, curator, "Синтетика в другом статусе", synthetic=True)
        offer = _salon_offer(
            demo, category, synthetic=True, template=canon, name="Не связана",
            mapping_status=SalonService.MappingStatus.UNMAPPED,
        )

        answer = offer_admission([offer.pk], viewer=granted)[offer.pk]

        assert answer.synthetic is True, "строка прочитана как синтетическая — и всё равно не допущена"
        assert answer.verdict is OfferVerdict.NOT_ADMITTED
        assert [(a.check, a.outcome) for a in answer.unmet] == [(A.MAPPING, O.FAILED)]

    def test_an_unclassified_synthetic_canon_is_not_helped(self, granted, demo, category):  # noqa: F811
        """«Неизвестно — не разрешение»: без разметки области строгая свёртка её не пускает."""
        canon = _canon(category, "Синтетика без разметки", synthetic=True)
        offer = _salon_offer(demo, category, synthetic=True, template=canon, name="Без разметки", mapping_status=REVIEW)

        strict = offer_admission([offer.pk], viewer=granted, not_enforced_is_unmet=True)[offer.pk]

        assert strict.verdict is OfferVerdict.NOT_ADMITTED
        assert strict.unmet[0].check is not A.MAPPING
        assert strict.unmet[0].outcome in {O.NOT_ENFORCED, O.FAILED}

    def test_a_synthetic_body_care_offer_without_a_reviewed_configuration_is_refused(self, synthetic, granted):
        """Синтетика В области Body Care отвечает за готовность конфигурации как настоящая — исключение не про неё."""
        _, offer, _ = synthetic

        answer = offer_admission([offer.pk], viewer=granted)[offer.pk]

        assert answer.synthetic is True
        assert answer.verdict is OfferVerdict.NOT_ADMITTED
        assert [(a.check, a.reason) for a in answer.unmet] == [(A.CONFIG, ReasonCode.ELIG_EXCLUDED_CONFIG_NOT_READY)]

    def test_a_retired_synthetic_canon_is_refused(self, chain, granted, curator):  # noqa: F811
        canon, offer, _ = chain
        ServiceTemplate.objects.filter(pk=canon.pk).update(
            lifecycle=ServiceTemplate.Lifecycle.RETIRED, retired_at=timezone.now(),
            retirement_source_ref="тестовый набор", retired_by=curator,
        )

        answer = offer_admission([offer.pk], viewer=granted)[offer.pk]

        assert answer.verdict is OfferVerdict.NOT_ADMITTED
        assert answer.unmet[0].reason is ReasonCode.ELIG_EXCLUDED_CANON_RETIRED


# -- границы 1 и 5: когда ветка НЕ срабатывает ---------------------------------


class TestTheBranchDoesNotFire:
    def test_with_the_flag_of_the_stand_off(self, synthetic, granted, settings):
        _, offer, _ = synthetic
        settings.SYNTHETIC_TEST_DATA_ENABLED = False

        assert offer_admission([offer.pk], viewer=granted) == {}

    def test_for_a_subject_who_is_not_on_the_server_list(self, synthetic, granted, settings):
        _, offer, _ = synthetic
        settings.SYNTHETIC_TEST_SUBJECT_IDS = []

        assert offer_admission([offer.pk], viewer=granted) == {}

    def test_for_a_listed_subject_who_is_not_a_test_persona(self, synthetic, granted):
        _, offer, _ = synthetic
        User.objects.filter(pk=granted.pk).update(is_test_persona=False)

        assert offer_admission([offer.pk], viewer=User.objects.get(pk=granted.pk)) == {}

    def test_for_another_test_persona_who_is_not_listed(self, synthetic, granted):
        """Чужая личность: тестовая, видит демо-салон, но в серверном списке её нет."""
        _, offer, _ = synthetic
        stranger = User.objects.create_user(
            username="syn2916-stranger", password="x", role="client", phone="+79992916002", is_test_persona=True,
        )

        assert offer_admission([offer.pk], viewer=stranger) == {}

    def test_without_a_viewer_and_in_the_operator_mode(self, synthetic, granted):
        _, offer, _ = synthetic

        assert offer_admission([offer.pk]) == {}
        assert offer_admission([offer.pk], all_salons=True) == {}
        assert admission_answers([(None, offer.pk, offer.template_id)]) == {}

    def test_there_is_no_parameter_to_ask_for_synthetic_data(self, synthetic, granted):
        """Граница 1: попросить нельзя ничем — ни именованным аргументом, ни булевым."""
        _, offer, _ = synthetic

        for call in (offer_admission, admission_answers):
            with pytest.raises(TypeError):
                call([offer.pk], include_synthetic=True)

    def test_a_real_offer_is_never_marked_and_never_excused(self, real, granted):
        _, offer, _ = real

        answer = offer_admission([offer.pk], viewer=granted)[offer.pk]

        assert answer.synthetic is False
        assert O.SYNTHETIC not in {a.outcome for a in answer.offer}


# -- граница 2: вся цепочка тестовая -------------------------------------------


class TestTheWholeChainIsATestOne:
    def test_a_master_moved_to_another_salon_does_not_open_the_offer(self, chain, granted):
        """Замок базы проверяет салон мастера при записи предложения; перевод мастера позже он не видит."""
        _, offer, master = chain
        assert offer_admission([offer.pk], viewer=granted)[offer.pk].verdict is OfferVerdict.OPEN, "контроль"
        elsewhere = Tenant.objects.create(slug="syn2916-real", name="Настоящий салон", is_active=True)
        SpecialistProfile.objects.filter(pk=master.pk).update(tenant=elsewhere)

        answer = offer_admission([offer.pk], viewer=granted)[offer.pk]

        assert answer.verdict is OfferVerdict.NO_SELLABLE_MASTER
        assert answer.masters == {}
        assert admission_answers([(master.pk, offer.pk, offer.template_id)], viewer=granted) == {}

    def test_the_search_by_capability_finds_only_the_synthetic_chain(self, synthetic, real, granted, curator):  # noqa: F811
        canon, offer, _ = synthetic
        real_canon, real_offer, _ = real
        _capability(canon, KEY, synthetic=True)
        _capability(real_canon, "real-wrap-2916b", synthetic=False, curator=curator)

        assert offers_by_capability(KEY, viewer=granted).offer_ids == (offer.pk,)
        assert offers_by_capability("real-wrap-2916b", viewer=granted).offer_ids == (real_offer.pk,)

    def test_without_the_grant_the_synthetic_capability_does_not_exist(self, synthetic, granted, settings):
        canon, _, _ = synthetic
        _capability(canon, KEY, synthetic=True)
        settings.SYNTHETIC_TEST_SUBJECT_IDS = []

        assert offers_by_capability(KEY, viewer=granted).empty_because is NoOffers.NO_CAPABILITY
        assert offers_by_capability(KEY).empty_because is NoOffers.NO_CAPABILITY


# -- граница 5: обычный подбор синтетику не видит и под разрешением -------------


class TestTheOrdinarySelectionStaysBlind:
    def test_the_resolver_does_not_show_the_synthetic_master_to_the_granted_subject(self, synthetic, real, granted):
        _, offer, synthetic_master = synthetic
        _, _, real_master = real
        source = SpecialistCandidateSource(viewer=granted)

        shown = _shown(resolve(_request(), source=source))
        mapping = source._mapping_by_specialist([synthetic_master.pk, real_master.pk])

        assert str(real_master.user_id) in shown
        assert str(synthetic_master.user_id) not in shown
        assert offer.pk not in {pk for facts in mapping.values() for pk in facts.legal_answers_by_service}

    def test_only_the_admission_reader_may_tell_the_builder_a_row_is_synthetic(self):
        """Сторож: признак ``synthetic=`` в ``build_answers`` передаёт одно место — ``users/admission.py``."""
        root = pathlib.Path(__file__).resolve().parents[2]
        callers = set()
        for path in root.rglob("*.py"):
            relative = path.relative_to(root).as_posix()
            if "/tests/" in relative or relative.startswith(("venv/", ".venv/")) or "/migrations/" in relative:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", getattr(node.func, "attr", None)) == "build_answers"
                    and any(keyword.arg == "synthetic" for keyword in node.keywords)
                ):
                    callers.add(relative)

        assert callers == {"users/admission.py"}


def test_the_fold_does_not_count_the_synthetic_outcome_as_unmet_and_does_not_hide_it():
    answers = build_answers(
        mapping_status=MappingStatus.REVIEW_REQUIRED, canon_retired=False, config_gate=ConfigGate.NOT_SUBJECT,
        license_gate=LegalGate.CLEARED, address_gate=LegalGate.CLEARED, qualification_gate=LegalGate.CLEARED,
        synthetic=True,
    )

    assert first_unmet(answers, not_enforced_is_unmet=True) is None
    assert [a.outcome for a in answers if a.check is A.MAPPING] == [O.SYNTHETIC]
