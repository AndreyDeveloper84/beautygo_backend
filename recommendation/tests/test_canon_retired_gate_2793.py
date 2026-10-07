"""DRF-2793 — связь VERIFIED на выведенном (retired) каноне не рекомендуется.

Рекомендуемость смотрела только на статус СВЯЗИ. Вывод канона из оборота
связь не трогает: она остаётся VERIFIED, и мастер оставался в подборе. Для
канона Body Care это закрывал CAT-6 (состояние ``retired``), для канона вне
Body Care — никто: там CAT-6 отвечает ``not_subject`` раньше, чем смотрит
на статус канона.

Что заперто:

- выведенный канон вне Body Care закрывает кандидата своим кодом;
- канон Body Care закрывается тем же кодом, а не «конфигурация не готова»;
- гейт на чтении: канон вывели после проверки связи — связь не тронута;
- живой канон в любом другом статусе проходит (``provisional`` /
  ``candidate`` — открытый вопрос владельцу, не этот лист);
- подмены предмета нет, и строка на выведенном каноне не выбирается, когда
  у мастера есть живая;
- новых запросов чтение статуса не добавляет.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from recommendation._pipeline import resolve
from recommendation._reason_codes import EXCLUSION_CODES, GATE_EXCLUSION_CODES, REGISTRY_VERSION, ReasonCode
from recommendation._stages import StagePolicy, apply_eligibility
from recommendation._types import (
    ConfigGate,
    LegalGate,
    MappingStatus,
    NeedOrigin,
    NeedSpec,
    RecommendationRequest,
    SafetyState,
    Scope,
    ScopeMode,
    Surface,
)
from recommendation.tests.conftest import StaticSource, make_facts
from services.models import SalonService, ServiceCategory, ServiceTemplate, SpecialistService
from tenants.models import Tenant
from users.models import SpecialistProfile, User
from users.recommendation_source import SpecialistCandidateSource, _MappingFacts

L = ServiceTemplate.Lifecycle

MARKETPLACE = Scope(ScopeMode.MARKETPLACE)
UNSTATED = NeedSpec(origin=NeedOrigin.MEMORY)


def _request(need=UNSTATED) -> RecommendationRequest:
    return RecommendationRequest(
        request_id=str(uuid.uuid4()), subject_ref="subj-2793", surface=Surface.MINIAPP_HOME,
        scope=MARKETPLACE, need=need, safety_state=SafetyState.NOT_APPLICABLE,
        tie_break_seed="seed-2793", k=10,
    )


def _admit(*facts):
    return apply_eligibility(list(facts), _request(), StagePolicy())


class TestTheResolverHalf:
    def test_a_retired_canon_excludes_with_its_own_code(self):
        result = _admit(make_facts(canon_retired=True))

        assert result.admitted == ()
        assert [e.reason_code for e in result.excluded] == [ReasonCode.ELIG_EXCLUDED_CANON_RETIRED]

    @pytest.mark.parametrize("retired", [False, None])
    def test_a_live_canon_or_a_silent_source_is_admitted(self, retired):
        candidate = make_facts(canon_retired=retired)

        assert [f.ref.id for f in _admit(candidate).admitted] == [candidate.ref.id]

    def test_the_mapping_is_still_checked_first(self):
        result = _admit(make_facts(mapping_status=MappingStatus.REVIEW_REQUIRED, canon_retired=True))

        assert [e.reason_code for e in result.excluded] == [ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE]

    def test_a_retired_canon_is_named_before_what_else_is_wrong_with_it(self):
        result = _admit(make_facts(
            canon_retired=True, config_gate=ConfigGate.NOT_READY, legal_gate=LegalGate.CLASS_UNCONFIRMED,
        ))

        assert [e.reason_code for e in result.excluded] == [ReasonCode.ELIG_EXCLUDED_CANON_RETIRED]

    def test_an_empty_shelf_names_the_reason(self):
        decision = resolve(_request(), source=StaticSource([make_facts(canon_retired=True)]))

        assert decision.ordered == ()
        assert ReasonCode.ELIG_EXCLUDED_CANON_RETIRED in decision.reason_codes

    def test_the_code_is_registered(self):
        assert ReasonCode.ELIG_EXCLUDED_CANON_RETIRED in EXCLUSION_CODES
        assert ReasonCode.ELIG_EXCLUDED_CANON_RETIRED in GATE_EXCLUSION_CODES
        assert tuple(int(p) for p in REGISTRY_VERSION.split(".")[:2]) >= (1, 6)


class TestTheLiveRowIsChosen:
    def _facts(self, rows) -> _MappingFacts:
        mapping = _MappingFacts()
        mapping.has_service = True
        for pk, retired in rows.items():
            mapping.status_by_service[pk] = "verified"
            mapping.config_by_service[pk] = ConfigGate.NOT_SUBJECT
            mapping.legal_by_service[pk] = LegalGate.CLEARED
            if retired:
                mapping.retired_services.add(pk)
        return mapping

    def test_one_live_row_answers_for_the_master_whatever_the_order(self):
        first, second = uuid.uuid4(), uuid.uuid4()

        assert self._facts({first: True, second: False}).canon_retired() is False
        assert self._facts({first: False, second: True}).canon_retired() is False

    def test_only_retired_rows_close_the_master(self):
        assert self._facts({uuid.uuid4(): True, uuid.uuid4(): True}).canon_retired() is True

    def test_a_legacy_row_has_no_canon_status(self):
        mapping = _MappingFacts()

        assert mapping.canon_retired(has_offer=True, matched_service_ref=uuid.uuid4()) is None

    def test_a_retired_row_does_not_win_by_being_ready(self):
        """Канон жив — раньше готовности: выведенная «готовая» не обгоняет живую «неготовую»."""
        retired_ready, live_unready = uuid.uuid4(), uuid.uuid4()
        mapping = self._facts({retired_ready: True, live_unready: False})
        mapping.config_by_service[retired_ready] = ConfigGate.READY
        mapping.config_by_service[live_unready] = ConfigGate.NOT_READY

        assert mapping.canon_retired() is False
        assert mapping.config_gate() is ConfigGate.NOT_READY


@pytest.mark.django_db
class TestTheRealSource:
    @pytest.fixture
    def tenant(self):
        return Tenant.objects.create(slug="r2793", name="Салон 2793", is_active=True)

    @pytest.fixture
    def curator(self):
        return User.objects.create_user(username="curator-2793", password="x", role="client", phone="+79995793000")

    @pytest.fixture
    def category(self):
        return ServiceCategory.objects.create(slug="r2793-hair", name="Волосы 2793")

    def _master(self, tenant, suffix: str) -> SpecialistProfile:
        user = User.objects.create_user(
            username=f"r2793-{suffix}", password="x", role="specialist", phone=f"+79995793{suffix}",
        )
        profile = SpecialistProfile.objects.get(user=user)
        profile.tenant = tenant
        profile.display_name = f"Мастер {suffix}"
        profile.is_available = True
        profile.is_booking_enabled = True
        profile.status = SpecialistProfile.ProfileStatus.ACTIVE
        profile.save()
        return profile

    def _offer(self, tenant, master, category, *, name) -> SalonService:
        """Услуга ВНЕ Body Care (семейства нет) с VERIFIED-связью на живом каноне."""
        template = ServiceTemplate.objects.create(
            category=category, name=f"{name} канон {uuid.uuid4().hex[:6]}", name_short=name[:20], duration_default=60,
        )
        offering = SalonService.objects.create(
            tenant=tenant, name=name, category=category, template=template, duration_minutes=60, is_active=True,
            base_price=Decimal("3000"), mapping_status=SalonService.MappingStatus.VERIFIED,
            mapping_confirmed_rule="test_fixture", mapping_rule_version="1.0.0",
            mapping_confirmed_at=timezone.now(), mapping_source_ref="fixture:2793",
        )
        SpecialistService.objects.create(
            specialist=master, salon_service=offering, tenant=tenant, price=Decimal("2000"), is_active=True,
        )
        return offering

    def _retire(self, offering, curator) -> None:
        ServiceTemplate.objects.filter(pk=offering.template_id).update(
            lifecycle=L.RETIRED, retired_by=curator, retired_at=timezone.now(), retirement_source_ref="реестр",
        )

    def _fetch(self, need=UNSTATED):
        return SpecialistCandidateSource().fetch(scope=MARKETPLACE, need=need)

    def _resolve(self, need=UNSTATED):
        return resolve(_request(need), source=SpecialistCandidateSource())

    @staticmethod
    def _shown(decision) -> set[str]:
        return {str(c.candidate_ref.id) for c in decision.ordered}

    @staticmethod
    def _excluded(decision) -> dict[str, ReasonCode]:
        return {str(e.candidate_ref.id): e.reason_code for e in decision.excluded}

    def test_a_verified_offer_on_a_retired_canon_outside_body_care_is_excluded(self, tenant, category, curator):
        """Ядро листа: CAT-6 такую строку не видит (``not_subject``), связь VERIFIED — и она была в подборе."""
        master = self._master(tenant, "01")
        offering = self._offer(tenant, master, category, name="Стрижка")
        assert self._shown(self._resolve()) == {str(master.user_id)}, "контроль: на живом каноне мастер в подборе"

        self._retire(offering, curator)

        [facts] = self._fetch()
        offering.refresh_from_db()
        assert offering.mapping_status == SalonService.MappingStatus.VERIFIED, "связь вывод канона не трогает"
        assert facts.config_gate is ConfigGate.NOT_SUBJECT, "CAT-6 строку по-прежнему пропускает"
        assert facts.canon_retired is True
        decision = self._resolve()
        assert self._excluded(decision) == {str(master.user_id): ReasonCode.ELIG_EXCLUDED_CANON_RETIRED}
        assert ReasonCode.ELIG_EXCLUDED_CANON_RETIRED in decision.reason_codes

    @pytest.mark.parametrize("lifecycle", [value for value in L.values if value != L.RETIRED])
    def test_every_other_lifecycle_passes(self, tenant, category, lifecycle):
        """По всему перечислению: закрывает только ``retired``. Остальные — вопрос владельцу, не этот лист."""
        master = self._master(tenant, "02")
        offering = self._offer(tenant, master, category, name="Стрижка")
        # `approved` схема принимает только с провенансом (CHECK) — он здесь не предмет узла.
        provenance = {
            "approved_at": timezone.now(), "approval_source_ref": "fixture:2793",
            "approved_rule": "test_fixture", "approval_rule_version": "1.0.0",
        } if lifecycle == L.APPROVED else {}
        ServiceTemplate.objects.filter(pk=offering.template_id).update(lifecycle=lifecycle, **provenance)

        [facts] = self._fetch()

        assert facts.canon_retired is False
        assert self._shown(self._resolve()) == {str(master.user_id)}

    def test_a_live_offer_does_not_admit_a_master_matched_by_a_retired_one(self, tenant, category, curator):
        master = self._master(tenant, "03")
        retired = self._offer(tenant, master, category, name="Стрижка старая")
        self._offer(tenant, master, category, name="Укладка")
        self._retire(retired, curator)
        need = NeedSpec(origin=NeedOrigin.USER_EXPLICIT, raw_text="стрижка старая")

        [facts] = self._fetch(need)

        assert facts.matched_service_ref == retired.pk
        assert facts.canon_retired is True
        assert self._excluded(self._resolve(need)) == {str(master.user_id): ReasonCode.ELIG_EXCLUDED_CANON_RETIRED}

    def test_of_two_offers_that_both_answer_the_need_the_live_one_is_taken(self, tenant, category, curator):
        master = self._master(tenant, "04")
        retired = self._offer(tenant, master, category, name="Стрижка классическая")
        live = self._offer(tenant, master, category, name="Стрижка модельная")
        self._retire(retired, curator)

        [facts] = self._fetch(NeedSpec(origin=NeedOrigin.USER_EXPLICIT, raw_text="стрижка"))

        assert facts.matched_service_ref == live.pk
        assert facts.canon_retired is False

    def test_with_no_need_stated_one_live_offer_is_enough(self, tenant, category, curator):
        master = self._master(tenant, "05")
        self._retire(self._offer(tenant, master, category, name="Стрижка старая"), curator)
        self._offer(tenant, master, category, name="Укладка")

        assert self._shown(self._resolve()) == {str(master.user_id)}

    def test_neighbours_are_unaffected(self, tenant, category, curator):
        retired_master, live_master = self._master(tenant, "06"), self._master(tenant, "07")
        self._retire(self._offer(tenant, retired_master, category, name="Стрижка старая"), curator)
        self._offer(tenant, live_master, category, name="Укладка")

        assert self._shown(self._resolve()) == {str(live_master.user_id)}

    def test_reading_the_canon_status_adds_no_queries(self, tenant, category, curator):
        def queries() -> int:
            with CaptureQueriesContext(connection) as captured:
                assert self._fetch()
            return len(captured)

        first = self._offer(tenant, self._master(tenant, "10"), category, name="Стрижка 0")
        live = queries()
        self._retire(first, curator)
        for index in range(1, 4):
            master = self._master(tenant, f"1{index}")
            self._retire(self._offer(tenant, master, category, name=f"Стрижка {index}"), curator)

        assert queries() == live
