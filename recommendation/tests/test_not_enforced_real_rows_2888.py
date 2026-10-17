"""DRF-2888 — исход «проверка не действует» на настоящих строках, при обоих положениях флага.

Владелец (07.10): переиспользование проверок само по себе не закрывает обход
классификации — нужно видеть, какие проверки реально работают при текущих
флагах. Строка, которая проходит только потому, что
``BODY_CARE_UNCLASSIFIED_FAIL_CLOSED`` выключен, обязана называться
«не проверено», а не «пройдено».

- правило — у каталога (``body_care_scope.unenforced_checks``); здесь оно
  только переводится в проверки резолвера, и наборы имён сверяются;
- при выключенном флаге неклассифицированная строка едет на обходе и это
  видно; при включённом — та же строка закрыта, и «не действует» не бывает;
- строка с подтверждённой областью и классом «не действует» не несёт ни при
  каком положении флага;
- на допуск исход не влияет: полка при выключенном флаге та же, что была.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from recommendation._admission import ALL_CHECKS, UNENFORCED_LITERALS, AdmissionCheck, CheckOutcome, first_unmet
from recommendation._reason_codes import ReasonCode
from recommendation.tests.test_legal_gate_cat10_ext_2843 import (  # noqa: F401 — фикстуры
    LC,
    _does,
    _fetch,
    _master,
    _offer,
    _resolve,
    _shown,
    category,
    curator,
    tenant,
)
from services import body_care_scope
from services.models import SalonService, ServiceTemplate
from users.admission import OfferVerdict, admission_answers, offer_admission

pytestmark = pytest.mark.django_db

A = AdmissionCheck
O = CheckOutcome  # noqa: E741
FLAG = "BODY_CARE_UNCLASSIFIED_FAIL_CLOSED"
Scope = ServiceTemplate.BodyCareScope


def _classified(offering, curator) -> None:  # noqa: F811
    """Область и класс подтверждены человеком: канон явно вне Body Care и немедицинский."""
    ServiceTemplate.objects.filter(pk=offering.template_id).update(
        body_care_scope=Scope.NOT_BODY_CARE, scope_confirmed_by=curator,
        scope_confirmed_at=timezone.now(), scope_source_ref="решение владельца",
        legal_service_class=LC.NON_MEDICAL_COSMETIC, legal_class_confirmed_by=curator,
        legal_class_confirmed_at=timezone.now(), legal_class_source_ref="решение юриста",
    )


def _outcomes(facts) -> dict[AdmissionCheck, CheckOutcome]:
    return {answer.check: answer.outcome for answer in facts.admission}


def test_the_literals_of_the_catalog_and_the_checks_of_the_resolver_are_one_set():
    """Каталог добавит шестой литерал — узел покраснеет здесь, а не промолчит в отчёте."""
    assert set(UNENFORCED_LITERALS) == set(body_care_scope.UNENFORCEABLE_CHECKS)


class TestAnUnclassifiedRow:
    """Стрижка, у которой никто не назвал ни область, ни класс, — почти весь пилот сегодня."""

    @pytest.fixture
    def haircut(self, tenant, category, curator):  # noqa: F811
        master = _master(tenant, "41")
        offering = _offer(tenant, category, curator, name="Стрижка")
        _does(master, offering)
        return master, offering

    def test_with_the_flag_off_it_rides_the_bypass_and_says_so(self, haircut, settings):
        setattr(settings, FLAG, False)

        [facts] = _fetch()

        outcomes = _outcomes(facts)
        riding = {check for check, outcome in outcomes.items() if outcome is O.NOT_ENFORCED}
        assert riding == {A.SCOPE, A.LEGAL_CLASS, A.LICENSE, A.ADDRESS, A.QUALIFICATION}
        assert O.PASSED not in {outcomes[check] for check in riding}
        assert outcomes[A.MAPPING] is O.PASSED, "связь проверена на самом деле"

    def test_it_is_still_on_the_shelf_while_the_flag_is_off(self, haircut, settings):
        """Исход «не действует» — отчёт, а не гейт: выдача при выключенном флаге прежняя."""
        setattr(settings, FLAG, False)
        master, _ = haircut

        [facts] = _fetch()

        assert first_unmet(facts.admission) is None
        assert _shown(_resolve()) == {str(master.user_id)}

    def test_the_strict_fold_names_what_was_not_checked(self, haircut, settings):
        setattr(settings, FLAG, False)

        [facts] = _fetch()
        strict = first_unmet(facts.admission, not_enforced_is_unmet=True)

        assert (strict.check, strict.outcome, strict.reason) == (A.SCOPE, O.NOT_ENFORCED, None)

    def test_with_the_flag_on_the_same_row_is_closed_and_nothing_is_unenforced(self, haircut, settings):
        setattr(settings, FLAG, True)
        master, _ = haircut

        [facts] = _fetch()

        outcomes = _outcomes(facts)
        assert O.NOT_ENFORCED not in outcomes.values()
        assert outcomes[A.SCOPE] is O.FAILED
        unmet = first_unmet(facts.admission)
        assert unmet.reason is ReasonCode.ELIG_EXCLUDED_CATALOG_UNCLASSIFIED
        assert _shown(_resolve()) == set()

    def test_the_flag_is_read_at_the_moment_of_the_call(self, haircut, settings):
        """Перепись зовёт читатель дважды под подменой настройки — ответы обязаны различаться."""
        _, offering = haircut
        row = [(None, offering.pk, offering.template_id)]

        setattr(settings, FLAG, False)
        off = {a.check: a.outcome for a in admission_answers(row)[(None, offering.pk)]}
        setattr(settings, FLAG, True)
        on = {a.check: a.outcome for a in admission_answers(row)[(None, offering.pk)]}

        assert off[A.SCOPE] is O.NOT_ENFORCED and on[A.SCOPE] is O.FAILED

    def test_the_verdict_on_the_offer_follows_the_fold(self, haircut, settings):
        setattr(settings, FLAG, False)
        _, offering = haircut

        assert offer_admission([offering.pk])[offering.pk].verdict is OfferVerdict.OPEN
        strict = offer_admission([offering.pk], not_enforced_is_unmet=True)[offering.pk]
        assert strict.verdict is OfferVerdict.NOT_ADMITTED
        assert [(a.check, a.outcome) for a in strict.unmet] == [(A.SCOPE, O.NOT_ENFORCED)]


class TestAClassifiedRow:
    @pytest.fixture
    def haircut(self, tenant, category, curator):  # noqa: F811
        master = _master(tenant, "42")
        offering = _offer(tenant, category, curator, name="Стрижка классифицированная")
        _does(master, offering)
        _classified(offering, curator)
        return master, offering

    @pytest.mark.parametrize("flag", [False, True])
    def test_nothing_about_it_is_unenforced_whatever_the_flag(self, haircut, settings, flag):
        setattr(settings, FLAG, flag)
        master, _ = haircut

        [facts] = _fetch()

        outcomes = _outcomes(facts)
        assert O.NOT_ENFORCED not in outcomes.values()
        assert outcomes[A.SCOPE] is O.PASSED and outcomes[A.LEGAL_CLASS] is O.PASSED
        assert first_unmet(facts.admission, not_enforced_is_unmet=True) is None
        assert _shown(_resolve()) == {str(master.user_id)}


class TestARowWithoutACanon:
    def test_the_qualification_literal_is_not_applied_where_there_is_nothing_to_ask(
        self, tenant, category, curator, settings,  # noqa: F811
    ):
        """Каталог называет «qualification» и у строки без канона; у резолвера там «не применима»
        по своей причине — отказ несёт проверка класса. Решение, а не совпадение."""
        setattr(settings, FLAG, False)
        orphan = SalonService.objects.create(
            tenant=tenant, category=category, template=None, name="Без канона",
            duration_minutes=60, base_price=Decimal("3000"),
        )

        answers = {a.check: a.outcome for a in admission_answers([(None, orphan.pk, None)])[(None, orphan.pk)]}

        assert answers[A.QUALIFICATION] is O.NOT_APPLICABLE
        assert answers[A.LEGAL_CLASS] is O.FAILED, "отсутствие канона проверку класса не обходит"
        assert [check for check in ALL_CHECKS if check not in answers] == []
