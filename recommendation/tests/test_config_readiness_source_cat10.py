"""CAT-10 (DRF-2820, чинит C1) — источниковая половина гейта готовности body-care.

Резолверная половина — ``test_config_readiness_gate_cat10.py``. Здесь —
настоящий источник над настоящей базой и настоящим состоянием CAT-6
(``services.body_care_validation``), один источник правды:

- body-care с VERIFIED-связью в любом состоянии, кроме
  ``ready_for_screening``, исключается ``ELIG_EXCLUDED_CONFIG_NOT_READY``;
- ``ready_for_screening`` допускается; услуга вне Body Care проходит как раньше;
- гейт на чтении: правка факта после ревью закрывает строку без правки связи;
- подмены предмета нет: готовая услуга Б не допускает мастера, совпавшего
  неготовой А;
- нужда не названа: достаточно одной рекомендуемой строки;
- выбор строки: готовая раньше глубокой;
- шов: ``None`` рождается только из ``not_subject``; ключа нет, незнакомое
  значение, сбой чтения — закрыто и ERROR в логе;
- чтение пакетное: число запросов не растёт с пулом.
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
from recommendation._reason_codes import ReasonCode
from recommendation._types import (
    ConfigGate,
    NeedOrigin,
    NeedSpec,
    RecommendationRequest,
    SafetyState,
    Scope,
    ScopeMode,
    Surface,
)
from services import body_care_validation
from services.body_care_validation import (
    BLOCKED,
    INCOMPLETE,
    NOT_SUBJECT,
    READY_FOR_SCREENING,
    RETIRED,
    REVIEW_REQUIRED,
    VALIDATION_STATES,
    record_config_review,
    validation_state,
)
from services.models import (
    GoalOption,
    GoalOptionCategory,
    OfferingConfigFact,
    SalonService,
    ServiceCategory,
    ServiceTemplate,
    SpecialistService,
)
from tenants.models import Tenant
from users import recommendation_source
from users.models import SpecialistProfile, User
from users.recommendation_source import SpecialistCandidateSource, config_readiness

pytestmark = pytest.mark.django_db

F = OfferingConfigFact.Field
St = OfferingConfigFact.State
Family = ServiceTemplate.ServiceFamily
LC = ServiceTemplate.LegalServiceClass
L = ServiceTemplate.Lifecycle

WRAP = {
    F.PRODUCT_NAME: "Маска",
    F.INSTRUCTION_VERSION: "v3",
    F.APPLICATION_AREA: "тело",
    F.COVERING_TYPE: "плёнка",
    F.EXPOSURE_SECONDS: 1800,
    F.REMOVAL_METHOD: "душ",
    F.ADDITIONAL_MODALITY: "нет",
}

MARKETPLACE = Scope(ScopeMode.MARKETPLACE)
UNSTATED = NeedSpec(origin=NeedOrigin.MEMORY)


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="cat10-src", name="Готовность", is_active=True)


@pytest.fixture
def curator(db):
    return User.objects.create_user(username="curator-cat10", password="x", role="client", phone="+79995820000")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(slug="cat10-body", name="Уход за телом cat10")


def _master(tenant, suffix: str) -> SpecialistProfile:
    user = User.objects.create_user(
        username=f"cat10-{suffix}", password="x", role="specialist", phone=f"+79995820{suffix}",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = tenant
    profile.display_name = f"Мастер {suffix}"
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.save()
    return profile


def _offer(tenant, master, category, curator, *, name, state=NOT_SUBJECT) -> SalonService:
    """Предложение мастера с VERIFIED-связью, приведённое в состояние CAT-6 ``state``.

    Состояние не пишется, а достигается теми же путями, что в жизни: факты,
    подтверждённый класс, ревью через ``record_config_review``.
    """
    body_care = state != NOT_SUBJECT
    retired = {
        "lifecycle": L.RETIRED, "retired_by": curator, "retired_at": timezone.now(),
        "retirement_source_ref": "реестр",
    } if state == RETIRED else {}
    template = ServiceTemplate.objects.create(
        category=category, name=f"{name} канон {uuid.uuid4().hex[:6]}", name_short=name[:20], duration_default=60,
        service_family=Family.BODY_WRAP if body_care else None, canonical_version="1" if body_care else "",
        **retired,
    )
    offering = SalonService.objects.create(
        tenant=tenant, name=name, category=category, template=template, duration_minutes=60, is_active=True,
        base_price=Decimal("3000"), configuration_version="cfg-1" if body_care else "",
        mapping_status=SalonService.MappingStatus.VERIFIED,
        mapping_confirmed_rule="test_fixture", mapping_rule_version="1.0.0",
        mapping_confirmed_at=timezone.now(), mapping_source_ref="fixture:cat10",
    )
    SpecialistService.objects.create(
        specialist=master, salon_service=offering, tenant=tenant, price=Decimal("2000"), is_active=True,
    )
    if body_care and state != INCOMPLETE:
        ServiceTemplate.objects.filter(pk=template.pk).update(
            legal_service_class=LC.NON_MEDICAL_COSMETIC, legal_class_confirmed_by=curator,
            legal_class_confirmed_at=timezone.now(), legal_class_source_ref="решение юриста",
        )
        for field, value in WRAP.items():
            OfferingConfigFact.objects.create(
                salon_service=offering, field=field, state=St.KNOWN, value=value,
                source_ref="инструкция", source_type="manufacturer_instruction",
            )
        if state != REVIEW_REQUIRED:
            record_config_review(offering, by=curator, source_ref="ревью куратора")
        if state == BLOCKED:
            _conflict(offering)
    assert validation_state(offering) == state, "фикстура не достигла заявленного состояния"
    return offering


def _conflict(offering) -> None:
    OfferingConfigFact.objects.filter(salon_service=offering, field=F.EXPOSURE_SECONDS).update(
        state=St.CONFLICT, value=[1200, 1800], source_ref="", source_type=None,
    )


@pytest.fixture
def source_log(caplog):
    """Записи логгера источника. У ``users`` в настройках ``propagate=False`` —
    до корневого перехватчика ``caplog`` они не доходят, вешаем его прямо сюда."""
    source_logger = logging.getLogger(recommendation_source.__name__)
    source_logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        source_logger.removeHandler(caplog.handler)


def _errors(log) -> list[str]:
    return [r.getMessage() for r in log.records if r.levelno >= logging.ERROR]


def _fetch(need=UNSTATED):
    return SpecialistCandidateSource().fetch(scope=MARKETPLACE, need=need)


def _named(text: str) -> NeedSpec:
    return NeedSpec(origin=NeedOrigin.USER_EXPLICIT, raw_text=text)


def _resolve(need=UNSTATED):
    return resolve(RecommendationRequest(
        request_id=str(uuid.uuid4()), subject_ref="subj-cat10", surface=Surface.MINIAPP_HOME,
        scope=MARKETPLACE, need=need, safety_state=SafetyState.NOT_APPLICABLE,
        tie_break_seed="seed-cat10", k=10,
    ), source=SpecialistCandidateSource())


def _shown(decision) -> set[str]:
    return {str(c.candidate_ref.id) for c in decision.ordered}


def _excluded(decision) -> dict[str, ReasonCode]:
    return {str(e.candidate_ref.id): e.reason_code for e in decision.excluded}


class TestTheGateReadsTheRealState:
    @pytest.mark.parametrize("state", [INCOMPLETE, REVIEW_REQUIRED, BLOCKED, RETIRED])
    def test_an_unready_body_care_offer_is_excluded_despite_a_verified_mapping(
        self, tenant, category, curator, state,
    ):
        master = _master(tenant, "01")
        _offer(tenant, master, category, curator, name="Обёртывание", state=state)

        decision = _resolve()

        assert decision.ordered == ()
        assert _excluded(decision) == {str(master.user_id): ReasonCode.ELIG_EXCLUDED_CONFIG_NOT_READY}
        assert ReasonCode.ELIG_EXCLUDED_CONFIG_NOT_READY in decision.reason_codes

    def test_a_ready_body_care_offer_is_admitted(self, tenant, category, curator):
        master = _master(tenant, "02")
        _offer(tenant, master, category, curator, name="Обёртывание", state=READY_FOR_SCREENING)

        [facts] = _fetch()

        assert facts.config_gate is ConfigGate.READY
        assert _shown(_resolve()) == {str(master.user_id)}

    def test_an_offer_outside_body_care_passes_whatever_its_neighbours_are(self, tenant, category, curator):
        """Положительный контроль на весь каталог: гейт не задевает прочее."""
        hairdresser, wrapper = _master(tenant, "03"), _master(tenant, "04")
        _offer(tenant, hairdresser, category, curator, name="Стрижка")
        _offer(tenant, wrapper, category, curator, name="Обёртывание", state=INCOMPLETE)

        facts = {str(f.ref.id): f.config_gate for f in _fetch()}

        assert facts == {
            str(hairdresser.user_id): ConfigGate.NOT_SUBJECT, str(wrapper.user_id): ConfigGate.NOT_READY,
        }
        assert _shown(_resolve()) == {str(hairdresser.user_id)}

    def test_a_state_that_falls_after_the_mapping_was_verified_closes_the_offer(self, tenant, category, curator):
        """Гейт на чтении: связь не трогали, а конфигурация перестала быть готовой."""
        master = _master(tenant, "05")
        offering = _offer(tenant, master, category, curator, name="Обёртывание", state=READY_FOR_SCREENING)
        assert _shown(_resolve()) == {str(master.user_id)}

        _conflict(offering)

        offering.refresh_from_db()
        assert offering.mapping_status == SalonService.MappingStatus.VERIFIED
        assert _excluded(_resolve()) == {str(master.user_id): ReasonCode.ELIG_EXCLUDED_CONFIG_NOT_READY}


class TestTheSubjectIsNotSubstituted:
    def test_a_ready_offer_does_not_admit_a_master_matched_by_an_unready_one(self, tenant, category, curator):
        master = _master(tenant, "06")
        unready = _offer(tenant, master, category, curator, name="Обёртывание водорослями", state=REVIEW_REQUIRED)
        _offer(tenant, master, category, curator, name="Скрабирование", state=READY_FOR_SCREENING)

        [facts] = _fetch(_named("обёртывание водорослями"))

        assert facts.matched_service_ref == unready.pk
        assert facts.config_gate is ConfigGate.NOT_READY
        assert _excluded(_resolve(_named("обёртывание водорослями"))) == {
            str(master.user_id): ReasonCode.ELIG_EXCLUDED_CONFIG_NOT_READY,
        }

    def test_of_two_offers_that_both_answer_the_need_the_ready_one_is_taken(self, tenant, category, curator):
        master = _master(tenant, "07")
        _offer(tenant, master, category, curator, name="Обёртывание горячее", state=INCOMPLETE)
        ready = _offer(tenant, master, category, curator, name="Обёртывание холодное", state=READY_FOR_SCREENING)

        [facts] = _fetch(_named("обёртывание"))

        assert facts.matched_service_ref == ready.pk
        assert facts.config_gate is ConfigGate.READY


class TestTheNeedIsNotStated:
    def test_one_recommendable_offer_is_enough(self, tenant, category, curator):
        master = _master(tenant, "08")
        _offer(tenant, master, category, curator, name="Обёртывание", state=BLOCKED)
        _offer(tenant, master, category, curator, name="Стрижка")

        [facts] = _fetch()

        assert facts.config_gate is ConfigGate.NOT_SUBJECT
        assert _shown(_resolve()) == {str(master.user_id)}

    def test_only_unready_offers_close_the_master(self, tenant, category, curator):
        master = _master(tenant, "09")
        _offer(tenant, master, category, curator, name="Обёртывание", state=BLOCKED)
        _offer(tenant, master, category, curator, name="Обёртывание второе", state=INCOMPLETE)

        [facts] = _fetch()

        assert facts.config_gate is ConfigGate.NOT_READY

    def test_an_unverified_offer_outside_body_care_does_not_open_the_gate(self, tenant, category, curator):
        """«Рекомендуемая строка» — VERIFIED и готовая. Непроверенная стрижка не
        делает мастера готовым за неготовое обёртывание."""
        master = _master(tenant, "10")
        _offer(tenant, master, category, curator, name="Обёртывание", state=BLOCKED)
        haircut = _offer(tenant, master, category, curator, name="Стрижка")
        SalonService.objects.filter(pk=haircut.pk).update(mapping_status=SalonService.MappingStatus.UNMAPPED)

        [facts] = _fetch()

        assert facts.config_gate is ConfigGate.NOT_READY


class TestTheReadyRowIsChosenBeforeTheDeeperOne:
    def test_r0_takes_the_ready_row_even_when_the_unready_one_is_deeper(self, tenant, curator):
        primary = ServiceCategory.objects.create(slug="cat10-primary", name="Основная cat10")
        secondary = ServiceCategory.objects.create(slug="cat10-secondary", name="Побочная cat10")
        goal = GoalOption.objects.create(key="goal-cat10", label="Цель cat10")
        GoalOptionCategory.objects.create(goal_option=goal, category=primary, sort_order=0)
        GoalOptionCategory.objects.create(goal_option=goal, category=secondary, sort_order=1)
        master = _master(tenant, "11")
        _offer(tenant, master, primary, curator, name="Глубокая неготовая", state=REVIEW_REQUIRED)
        ready = _offer(tenant, master, secondary, curator, name="Мелкая готовая", state=READY_FOR_SCREENING)

        [facts] = _fetch(NeedSpec(origin=NeedOrigin.GOAL, goal_key=goal.key))

        assert facts.matched_service_ref == ready.pk
        assert facts.config_gate is ConfigGate.READY
        assert facts.goal_fit_depth == 1


class TestTheSeam:
    """``config_readiness`` — единственное место, где ответ CAT-6 становится ``config_gate``."""

    def test_only_not_subject_becomes_none(self):
        """Инвариант: ни одно состояние body-care не даёт ``None`` — по всем ответам функции."""
        pks = {state: uuid.uuid4() for state in (NOT_SUBJECT, *VALIDATION_STATES)}
        answers = {pk: state for state, pk in pks.items()}

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(recommendation_source, "validation_states", lambda ids: answers)
            readiness = config_readiness(pks.values())

        assert readiness[pks[NOT_SUBJECT]] is ConfigGate.NOT_SUBJECT
        assert readiness[pks[READY_FOR_SCREENING]] is ConfigGate.READY
        closing = set(VALIDATION_STATES) - {READY_FOR_SCREENING}
        assert closing == {INCOMPLETE, REVIEW_REQUIRED, BLOCKED, RETIRED}
        assert {readiness[pks[state]] for state in closing} == {ConfigGate.NOT_READY}

    @pytest.mark.parametrize(
        "answer",
        [
            pytest.param(lambda pk: {}, id="the-key-is-missing"),
            pytest.param(lambda pk: {pk: "approved"}, id="an-unknown-value"),
            pytest.param(lambda pk: {pk: None}, id="none"),
        ],
    )
    def test_an_undetermined_answer_closes_the_row_and_is_loud(self, monkeypatch, source_log, answer):
        pk = uuid.uuid4()
        monkeypatch.setattr(recommendation_source, "validation_states", lambda ids: answer(pk))

        readiness = config_readiness([pk])

        assert readiness == {pk: ConfigGate.UNDETERMINED}
        [message] = _errors(source_log)
        assert "config_readiness_undetermined" in message
        assert str(pk) in message

    def test_a_known_closing_state_is_not_an_error(self, monkeypatch, source_log):
        """Положительный контроль: штатное «не готово» в ERROR не пишется."""
        pk = uuid.uuid4()
        monkeypatch.setattr(recommendation_source, "validation_states", lambda ids: {pk: INCOMPLETE})

        readiness = config_readiness([pk])

        assert readiness == {pk: ConfigGate.NOT_READY}
        assert _errors(source_log) == []

    def test_a_failing_read_closes_the_rows_instead_of_dropping_the_shelf(self, monkeypatch, source_log):
        def broken(ids):
            raise RuntimeError("CAT-6 сломан")

        pk = uuid.uuid4()
        monkeypatch.setattr(recommendation_source, "validation_states", broken)

        readiness = config_readiness([pk])

        assert readiness == {pk: ConfigGate.UNDETERMINED}
        [message] = _errors(source_log)
        assert "config_readiness_failed" in message

    def test_a_body_care_row_the_function_dropped_is_excluded_not_waved_through(
        self, tenant, category, curator, monkeypatch,
    ):
        """Сквозной: нарушенный контракт функции у настоящей body-care строки — закрытая строка,
        и причина названа дефектом чтения, а не «конфигурация не готова»."""
        master = _master(tenant, "12")
        _offer(tenant, master, category, curator, name="Обёртывание", state=READY_FOR_SCREENING)
        monkeypatch.setattr(recommendation_source, "validation_states", lambda ids: {})

        assert _excluded(_resolve()) == {str(master.user_id): ReasonCode.ELIG_EXCLUDED_ELIGIBILITY_UNDETERMINED}

    def test_the_source_uses_the_cat6_function_itself(self):
        """Один источник состояния: шов читает функцию CAT-6, а не свою копию лестницы."""
        assert recommendation_source.validation_states is body_care_validation.validation_states


class TestTheReadIsBatched:
    def test_the_number_of_queries_does_not_grow_with_the_pool(self, tenant, category, curator):
        def queries() -> int:
            with CaptureQueriesContext(connection) as captured:
                facts = _fetch()
            assert facts
            return len(captured)

        first = _master(tenant, "13")
        _offer(tenant, first, category, curator, name="Обёртывание 0", state=READY_FOR_SCREENING)
        for_one = queries()

        for index, state in enumerate((INCOMPLETE, REVIEW_REQUIRED, NOT_SUBJECT, READY_FOR_SCREENING), start=1):
            master = _master(tenant, f"2{index}")
            _offer(tenant, master, category, curator, name=f"Обёртывание {index}", state=state)
            _offer(tenant, master, category, curator, name=f"Ещё одно {index}", state=state)

        assert queries() == for_one
