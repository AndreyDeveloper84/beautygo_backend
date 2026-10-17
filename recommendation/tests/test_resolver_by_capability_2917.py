# flake8: noqa: F811
"""DRF-2917 — резолвер ищет мастеров по названной способности.

До этого листа провод принимал ``need.capability_refs``, считал нужду
названной, а источник по полю не искал: запрос только со способностью молча
исключал всех как «не умеет».

- способность называется КЛЮЧОМ словаря; находятся мастера, у которых есть
  услуга на каноне с подтверждённой способностью, — уровнем «по способности»;
- нет подтверждённого знания по ключу — выдача пуста и ответ это называет;
- допуск не обходится: найденный мастер проходит те же восемь проверок;
- помеченная синтетика подбору не видна и под серверным разрешением;
- одна способность на запрос; непустой канон — отказ, а не пустая выдача.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from recommendation._reason_codes import ReasonCode
from recommendation._types import MatchLevel, NeedOrigin, NeedSpec, Scope, ScopeMode
from recommendation.tests.test_goal_fit_depth_2789 import _signed
from recommendation.tests.test_legal_gate_cat10_ext_2843 import (  # noqa: F401 — фикстуры
    _does,
    _excluded,
    _fetch,
    _master,
    _offer,
    _resolve,
    _shown,
    category,
    curator,
    tenant,
)
from recommendation.tests.test_synthetic_admission_2916 import chain  # noqa: F401 — фикстуры
from recommendation.tests.test_synthetic_rows_are_not_real_2916 import demo, persona  # noqa: F401 — фикстуры
from services.models import ProcedureCapability
from services.synthetic import grant_for
from services.tests.test_synthetic_test_mark import _capability
from tenants.models import Tenant
from users.capability_offers import offers_by_capability
from users.models import User

pytestmark = pytest.mark.django_db

KEY = "lymph-drainage-2917"
TOKEN = "test-ayla-internal-token-2917"
URL = "/api/v1/internal/recommendation/resolve/"
CALLER = "bot:cap2917"


def _can(templates, curator, *, key=KEY, approved=True, **extra):
    return ProcedureCapability.objects.create(
        templates=list(templates), key=key, text_client="Синтетическая формулировка",
        **_signed(curator, approved, **extra),
    )


def _by_capability(key: str = KEY, **extra) -> NeedSpec:
    return NeedSpec(origin=NeedOrigin.USER_EXPLICIT, capability_refs=(key,), **extra)


@pytest.fixture
def two(tenant, category, curator):
    """Два мастера одного салона: массаж (на нём способность) и маникюр."""
    masseur, manicurist = _master(tenant, "71"), _master(tenant, "72")
    massage = _offer(tenant, category, curator, name="Массаж лимфодренажный")
    manicure = _offer(tenant, category, curator, name="Маникюр")
    _does(masseur, massage)
    _does(manicurist, manicure)
    return {"masseur": masseur, "manicurist": manicurist, "massage": massage, "manicure": manicure}


# -- в процессе: источник и стадии ---------------------------------------------


class TestTheMasterIsFoundByTheCapability:
    def test_a_master_whose_service_stands_on_a_capable_canon_is_shown(self, two, curator):
        _can([two["massage"].template], curator)

        decision = _resolve(_by_capability())

        assert _shown(decision) == {str(two["masseur"].user_id)}
        [found] = decision.ordered
        assert ReasonCode.MATCH_CAPABILITY_ONLY in found.reason_codes

    def test_the_other_master_is_excluded_as_not_capable(self, two, curator):
        _can([two["massage"].template], curator)

        decision = _resolve(_by_capability())

        assert _excluded(decision) == {str(two["manicurist"].user_id): ReasonCode.ELIG_EXCLUDED_NOT_CAPABLE}

    def test_the_source_says_which_service_answers(self, two, curator):
        _can([two["massage"].template], curator)

        facts = {str(f.ref.id): f for f in _fetch(_by_capability())}

        masseur = facts[str(two["masseur"].user_id)]
        assert (masseur.match_level, masseur.matched_service_ref) == (MatchLevel.CAPABILITY_ONLY, two["massage"].pk)
        assert facts[str(two["manicurist"].user_id)].match_level is MatchLevel.UNDETERMINED

    def test_one_capability_on_several_canons_finds_the_masters_of_each(self, two, curator):
        _can([two["massage"].template, two["manicure"].template], curator)

        assert _shown(_resolve(_by_capability())) == {str(two["masseur"].user_id), str(two["manicurist"].user_id)}

    def test_a_service_named_by_text_outranks_the_capability(self, two, curator):
        """Лестница: назвавший услугу сказал больше, чем назвавший способность."""
        _can([two["massage"].template, two["manicure"].template], curator)

        facts = {str(f.ref.id): f for f in _fetch(_by_capability(raw_text="маникюр"))}

        assert facts[str(two["manicurist"].user_id)].match_level is MatchLevel.SERVICE_EXACT
        assert facts[str(two["masseur"].user_id)].match_level is MatchLevel.CAPABILITY_ONLY


class TestOnlyConfirmedKnowledgeFinds:
    @pytest.mark.parametrize("how", ["inferred", "expired", "not_supported", "another_key", "nobody_knows"])
    def test_without_confirmed_knowledge_nobody_is_shown(self, two, curator, how):
        if how == "inferred":
            _can([two["massage"].template], curator, approved=False)
        elif how == "expired":
            _can([two["massage"].template], curator, valid_until=timezone.now() - timedelta(days=1))
        elif how == "not_supported":
            _can([two["massage"].template], curator, claim_scope="not_supported")
        elif how == "another_key":
            _can([two["massage"].template], curator, key="something-else-2917")

        decision = _resolve(_by_capability())

        assert _shown(decision) == set()
        assert set(_excluded(decision).values()) == {ReasonCode.ELIG_EXCLUDED_NOT_CAPABLE}

    def test_control_the_same_masters_are_shown_when_nothing_is_named(self, two):
        assert _shown(_resolve()) == {str(two["masseur"].user_id), str(two["manicurist"].user_id)}


class TestAdmissionIsNotBypassed:
    def test_a_found_master_is_still_closed_by_the_eight_checks(self, two, curator, settings):
        """Неразмеченная строка при включённом флаге каталога закрыта кодом ДОПУСКА, а не «не умеет»."""
        settings.BODY_CARE_UNCLASSIFIED_FAIL_CLOSED = True
        _can([two["massage"].template], curator)

        decision = _resolve(_by_capability())

        assert _shown(decision) == set()
        assert _excluded(decision)[str(two["masseur"].user_id)] is ReasonCode.ELIG_EXCLUDED_CATALOG_UNCLASSIFIED

    def test_the_scope_of_the_search_still_holds(self, two, curator, category):
        _can([two["massage"].template], curator)
        elsewhere = Tenant.objects.create(slug="cap2917-other", name="Другой салон", is_active=True)
        scope = Scope(ScopeMode.MARKETPLACE, tenant_refs=(elsewhere.pk,))

        from recommendation.api import resolve
        from recommendation.tests.test_legal_gate_cat10_ext_2843 import _request
        from users.recommendation_source import SpecialistCandidateSource
        import dataclasses

        decision = resolve(
            dataclasses.replace(_request(_by_capability()), scope=scope), source=SpecialistCandidateSource(),
        )

        assert _shown(decision) == set()


# -- провод ---------------------------------------------------------------------


@pytest.fixture
def caller(db, settings):
    settings.AYLA_INTERNAL_API_TOKEN = TOKEN
    return User.objects.create_user(username=CALLER, password="x", role="client", phone="+79992917001", is_proxy=True)


def _post(need: dict, *, external_user_id: str = CALLER):
    client = APIClient()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
    client.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    body = {
        "request_id": "req-2917", "surface": "BOT_CHAT", "scope": {"mode": "MARKETPLACE"},
        "need": need, "safety_state": "NORMAL", "tie_break_seed": "seed-2917", "k": 10,
    }
    return client.post(URL, body, format="json")


def _shown_on_wire(response) -> set[str]:
    return {row["candidate"]["id"] for row in response.json()["data"]["ordered"]}


class TestTheWire:
    def test_the_capability_is_named_by_its_key_and_the_answer_says_it_is_known(self, two, curator, caller):
        _can([two["massage"].template], curator)

        response = _post({"origin": "USER_EXPLICIT", "capability_refs": [KEY]})

        assert response.status_code == 200, response.content
        assert _shown_on_wire(response) == {str(two["masseur"].user_id)}
        assert response.json()["data"]["need_resolution"] == [{"kind": "capability", "key": KEY, "status": "known"}]

    def test_an_unknown_capability_is_an_empty_answer_with_its_reason(self, two, caller):
        response = _post({"origin": "USER_EXPLICIT", "capability_refs": ["nobody-knows-2917"]})

        assert response.status_code == 200, response.content
        data = response.json()["data"]
        assert data["ordered"] == []
        assert data["need_resolution"] == [
            {"kind": "capability", "key": "nobody-knows-2917", "status": "no_confirmed_knowledge"},
        ]

    def test_known_but_nobody_here_offers_it_is_told_apart_from_unknown(self, two, curator, category, caller):
        """Способность подтверждена, но в области поиска её никто не оказывает: «известна» и пусто."""
        from services.models import ServiceTemplate

        bare = ServiceTemplate.objects.create(category=category, name="Канон без предложений", name_short="Канон")
        _can([bare], curator)

        data = _post({"origin": "USER_EXPLICIT", "capability_refs": [KEY]}).json()["data"]

        assert data["ordered"] == []
        assert data["need_resolution"][0]["status"] == "known"

    def test_a_draft_capability_is_not_told_apart_from_an_unknown_one(self, two, curator, caller):
        """Наружу одна причина: чем знание не подтверждено, провод не говорит."""
        _can([two["massage"].template], curator, approved=False)

        data = _post({"origin": "USER_EXPLICIT", "capability_refs": [KEY]}).json()["data"]

        assert data["need_resolution"][0]["status"] == "no_confirmed_knowledge"

    def test_nothing_named_gives_an_empty_resolution_and_the_usual_answer(self, two, caller):
        data = _post({"origin": "MEMORY"}).json()["data"]

        assert data["need_resolution"] == []
        assert len(data["ordered"]) == 2

    def test_two_capabilities_are_refused(self, two, caller):
        response = _post({"origin": "USER_EXPLICIT", "capability_refs": [KEY, "another-2917"]})

        assert response.status_code == 400

    def test_a_canon_reference_is_refused_instead_of_silently_excluding_everybody(self, two, caller):
        response = _post({
            "origin": "USER_EXPLICIT", "canonical_service_refs": [str(two["massage"].template_id)],
        })

        assert response.status_code == 400

    def test_an_empty_canon_list_is_accepted_as_before(self, two, caller):
        """Бот шлёт оба поля пустыми списками в каждом запросе полки — это не должно сломаться."""
        response = _post({"origin": "MEMORY", "capability_refs": [], "canonical_service_refs": []})

        assert response.status_code == 200, response.content

    def test_a_key_that_is_not_a_slug_is_refused(self, two, caller):
        response = _post({"origin": "USER_EXPLICIT", "capability_refs": ["лимфодренаж с пробелом"]})

        assert response.status_code == 400


class TestSyntheticDataStaysInvisible:
    def test_a_synthetic_capability_is_unknown_to_the_resolver_even_under_the_grant(self, chain, settings):
        """Подбор разрешения на синтетику не выводит никогда: ключ неизвестен, мастер не показан.

        Вызывающий — самый опасный из возможных: тестовая личность, внесённая в
        серверный список, при включённом флаге стенда. Допуск ей синтетику
        отдаёт (DRF-2916); резолвер — нет.
        """
        settings.AYLA_INTERNAL_API_TOKEN = TOKEN
        canon, offer, master = chain
        _capability(canon, "synthetic-key-2917", synthetic=True)
        subject = User.objects.create_user(
            username="bot:syn2917", password="x", role="client", phone="+79992917002",
            is_proxy=True, is_test_persona=True,
        )
        settings.SYNTHETIC_TEST_DATA_ENABLED = True
        settings.SYNTHETIC_TEST_SUBJECT_IDS = [str(subject.pk)]
        assert grant_for(subject) is not None, "контроль: разрешение этому субъекту действительно выдаётся"
        assert offers_by_capability("synthetic-key-2917", viewer=subject).offer_ids == (offer.pk,), (
            "контроль: путь плана под тем же разрешением синтетику находит"
        )

        response = _post(
            {"origin": "USER_EXPLICIT", "capability_refs": ["synthetic-key-2917"]}, external_user_id="bot:syn2917",
        )

        assert response.status_code == 200, response.content
        data = response.json()["data"]
        assert str(master.user_id) not in _shown_on_wire(response)
        assert data["need_resolution"][0]["status"] == "no_confirmed_knowledge"
