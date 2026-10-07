"""O-1b (DRF-2831) — режим «свои салоны» на границе `resolve`.

Любимый мастер — отношение клиента с ОДНИМ салоном (NEVER_CROSSES, 24.08).
Память о мастере и салоне законна только там, где область вывел сервер:

- ``scope.mode=OWN_SALONS`` — салоны клиента называет сервер по действующим
  отношениям покупателя; ``tenant_refs`` от бота — 400;
- своих салонов нет — пустая выдача, а не весь маркетплейс;
- память о мастере поднимает его только в этом режиме;
- ``source_tenant_id``: поля нет или ``global_bot`` — все свои салоны; id
  салона — только он; ``null``, мусор, чужой салон — предпочтение не едет;
- отозванное отношение и роль сотрудника салон своим не делают.
"""
from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from recommendation._stages import apply_scope, effective_preferences, satisfies_preference
from recommendation.api import (
    MEMORY_SOURCE_GLOBAL_BOT,
    NeedOrigin,
    NeedSpec,
    Preference,
    PreferenceKind,
    PreferenceOrigin,
    Scope,
    ScopeMode,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User
from users.own_salons import own_salon_ids
from users.recommendation_source import SpecialistCandidateSource

from .conftest import StaticSource, make_facts

VALID_TOKEN = "test-ayla-internal-token-own-salons"
URL = "/api/v1/internal/recommendation/resolve/"
EXTERNAL_USER_ID = "bot:own-salons"

_CANDIDATES: list = []

pytestmark = pytest.mark.django_db


def own_salons_source_factory(*, viewer=None):
    return StaticSource(_CANDIDATES)


@pytest.fixture(autouse=True)
def _boundary(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.RECOMMENDATION_CANDIDATE_SOURCE = (
        "recommendation.tests.test_own_salons_o1b_2831.own_salons_source_factory"
    )
    _CANDIDATES.clear()
    yield
    _CANDIDATES.clear()


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client", phone="+79992831001", is_proxy=True,
    )


def _salon(tag: str) -> Tenant:
    return Tenant.objects.create(slug=f"o1b-{tag}", name=f"Салон {tag}", is_active=True)


def _relate(user, tenant, *, role=TenantUserRelationship.Role.CUSTOMER, is_active=True) -> None:
    TenantUserRelationship.objects.create(user=user, tenant=tenant, role=role, is_active=is_active)


@pytest.fixture
def salons(customer):
    """A и B — свои салоны клиента, C — чужой. В каждом по два мастера."""
    a, b, c = _salon("a"), _salon("b"), _salon("c")
    _relate(customer, a)
    _relate(customer, b)
    facts = {
        key: [make_facts(tenant_ref=tenant.id), make_facts(tenant_ref=tenant.id)]
        for key, tenant in (("a", a), ("b", b), ("c", c))
    }
    for rows in facts.values():
        _CANDIDATES.extend(rows)
    return {"a": a, "b": b, "c": c, "facts": facts}


def _post(**overrides):
    body = {
        "request_id": "req-o1b",
        "surface": "MINIAPP_HOME",
        "scope": {"mode": "OWN_SALONS"},
        "need": {"origin": "USER_EXPLICIT", "raw_text": "массаж"},
        "safety_state": "NORMAL",
        "tie_break_seed": "conv-o1b",
        "k": 10,
    }
    body.update(overrides)
    client = APIClient()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    client.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
    return client.post(URL, body, format="json")


def _tiers(**overrides) -> dict[str, int]:
    response = _post(**overrides)
    assert response.status_code == 200, response.content
    return {row["candidate"]["id"]: row["tier"] for row in response.json()["data"]["ordered"]}


def _ids(rows) -> set[str]:
    return {str(f.ref.id) for f in rows}


def _memory(kind: str, ref, **extra) -> dict:
    return {"kind": kind, "ref": str(ref), "origin": "confirmed_memory", **extra}


def _is_raised(tiers: dict[str, int], favourite) -> bool:
    favourite = str(favourite)
    return tiers[favourite] < min(t for cid, t in tiers.items() if cid != favourite)


class TestTheServerNamesTheSalons:
    def test_only_the_clients_own_salons_are_searched(self, salons):
        tiers = _tiers()

        assert set(tiers) == _ids(salons["facts"]["a"]) | _ids(salons["facts"]["b"])

    def test_no_own_salons_is_an_empty_answer_not_the_marketplace(self, customer):
        stranger_salon = _salon("x")
        _CANDIDATES.extend([make_facts(tenant_ref=stranger_salon.id), make_facts(tenant_ref=None)])

        assert _tiers() == {}

    def test_positive_control_the_marketplace_sees_everyone(self, salons):
        tiers = _tiers(scope={"mode": "MARKETPLACE"})

        assert len(tiers) == 6

    @pytest.mark.parametrize("field", ["tenant_refs", "exclude_tenant_refs"])
    def test_the_bot_cannot_name_the_salons_itself(self, salons, field):
        """Присланный список — не подсказка, а чужая область под своим именем: отказ."""
        response = _post(scope={"mode": "OWN_SALONS", field: [str(salons["c"].id)]})

        assert response.status_code == 400, response.content

    def test_a_revoked_relationship_does_not_make_a_salon_own(self, customer):
        former = _salon("former")
        _relate(customer, former, is_active=False)
        _CANDIDATES.append(make_facts(tenant_ref=former.id))

        assert _tiers() == {}

    def test_working_in_a_salon_does_not_make_it_own(self, customer):
        workplace = _salon("work")
        _relate(customer, workplace, role=TenantUserRelationship.Role.STAFF)
        _CANDIDATES.append(make_facts(tenant_ref=workplace.id))

        assert _tiers() == {}

    def test_another_clients_salons_are_not_mine(self, salons):
        other = User.objects.create_user(username="o1b-other", password="x", role="client", phone="+79992831002")
        _relate(other, salons["c"])

        assert set(_tiers()) == _ids(salons["facts"]["a"]) | _ids(salons["facts"]["b"])
        assert own_salon_ids(other) == (salons["c"].id,)


class TestMemoryWorksOnlyHere:
    def test_a_remembered_master_is_raised_in_own_salons(self, salons):
        favourite = salons["facts"]["a"][0].ref.id

        tiers = _tiers(preferences=[_memory("master", favourite)])

        assert _is_raised(tiers, favourite)

    def test_a_remembered_salon_is_raised_in_own_salons(self, salons):
        tiers = _tiers(preferences=[_memory("salon", salons["a"].id)])

        in_a, in_b = _ids(salons["facts"]["a"]), _ids(salons["facts"]["b"])
        assert max(tiers[c] for c in in_a) < min(tiers[c] for c in in_b)

    def test_the_same_memory_does_not_rank_the_marketplace(self, salons):
        """Контроль с другой стороны: вне режима память о мастере по-прежнему не едет."""
        favourite = salons["facts"]["a"][0].ref.id

        tiers = _tiers(scope={"mode": "MARKETPLACE"}, preferences=[_memory("master", favourite)])

        assert len(set(tiers.values())) == 1

    def test_a_favourite_from_a_foreign_salon_is_simply_absent(self, salons):
        foreign = salons["facts"]["c"][0].ref.id

        tiers = _tiers(preferences=[_memory("master", foreign)])

        assert str(foreign) not in tiers
        assert len(set(tiers.values())) == 1


class TestWhereTheMemoryWasWritten:
    def test_the_global_bot_marker_means_every_own_salon(self, salons):
        favourite = salons["facts"]["b"][0].ref.id

        tiers = _tiers(preferences=[_memory("master", favourite, source_tenant_id=MEMORY_SOURCE_GLOBAL_BOT)])

        assert _is_raised(tiers, favourite)

    def test_a_salon_id_keeps_the_memory_inside_that_salon(self, salons):
        favourite = salons["facts"]["a"][0].ref.id

        tiers = _tiers(preferences=[_memory("master", favourite, source_tenant_id=str(salons["a"].id))])

        assert _is_raised(tiers, favourite)

    def test_memory_written_in_one_salon_does_not_work_in_another(self, salons):
        """Память записана в салоне A, а мастер стоит в B: в B её не записывали."""
        in_b = salons["facts"]["b"][0].ref.id

        tiers = _tiers(preferences=[_memory("master", in_b, source_tenant_id=str(salons["a"].id))])

        assert len(tiers) == 4
        assert len(set(tiers.values())) == 1

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param(None, id="null"),
            pytest.param("somewhere", id="garbage"),
            pytest.param("", id="empty"),
        ],
    )
    def test_memory_of_unknown_origin_does_not_travel(self, salons, source):
        favourite = salons["facts"]["a"][0].ref.id

        tiers = _tiers(preferences=[_memory("master", favourite, source_tenant_id=source)])

        assert len(tiers) == 4, "запрос не падает, выдача цела"
        assert len(set(tiers.values())) == 1

    def test_memory_written_in_a_salon_that_is_not_own_is_dropped(self, salons):
        """Сузить до чужого салона нечего, а расширить до всех своих — применить память там,
        где её не записывали."""
        favourite = salons["facts"]["a"][0].ref.id

        tiers = _tiers(preferences=[_memory("master", favourite, source_tenant_id=str(salons["c"].id))])

        assert len(set(tiers.values())) == 1

    def test_what_is_said_now_ignores_the_field(self, salons):
        named = salons["facts"]["a"][0].ref.id

        tiers = _tiers(preferences=[{
            "kind": "master", "ref": str(named), "origin": "current_request", "source_tenant_id": None,
        }])

        assert _is_raised(tiers, named)


class TestTheResolverHoldsTheBoundaryItself:
    """Граница не держится на одной ручке: стадия и источник знают режим сами."""

    def test_an_empty_own_salons_scope_admits_nobody(self):
        candidates = [make_facts(tenant_ref=uuid.uuid4()), make_facts(tenant_ref=None)]

        result = apply_scope(candidates, Scope(ScopeMode.OWN_SALONS))

        assert result.admitted == ()
        assert len(result.excluded) == 2

    def test_positive_control_an_empty_marketplace_scope_admits_everyone(self):
        candidates = [make_facts(tenant_ref=uuid.uuid4()), make_facts(tenant_ref=None)]

        assert len(apply_scope(candidates, Scope(ScopeMode.MARKETPLACE)).admitted) == 2

    def test_own_salons_cannot_carry_exclusions(self):
        with pytest.raises(ValueError):
            Scope(ScopeMode.OWN_SALONS, exclude_tenant_refs=(uuid.uuid4(),))

    def test_the_real_source_reads_an_empty_own_salons_scope_as_nobody(self):
        tenant = _salon("src")
        user = User.objects.create_user(username="o1b-master", password="x", role="specialist", phone="+79992831003")
        profile = SpecialistProfile.objects.get(user=user)
        profile.tenant = tenant
        profile.is_available = True
        profile.is_booking_enabled = True
        profile.status = SpecialistProfile.ProfileStatus.ACTIVE
        profile.save()
        source, need = SpecialistCandidateSource(), NeedSpec(origin=NeedOrigin.MEMORY)

        assert len(source.fetch(scope=Scope(ScopeMode.MARKETPLACE), need=need)) == 1, "контроль: мастер в пуле"
        assert list(source.fetch(scope=Scope(ScopeMode.OWN_SALONS), need=need)) == []
        assert len(source.fetch(scope=Scope(ScopeMode.OWN_SALONS, tenant_refs=(tenant.id,)), need=need)) == 1

    def test_a_narrowed_preference_does_not_match_outside_its_salon(self):
        salon_a, salon_b = uuid.uuid4(), uuid.uuid4()
        master = make_facts(tenant_ref=salon_b)

        narrowed = Preference(PreferenceKind.MASTER, master.ref.id, tenant_ref=salon_a)
        everywhere = Preference(PreferenceKind.MASTER, master.ref.id)

        assert satisfies_preference(master, narrowed) is False
        assert satisfies_preference(master, everywhere) is True

    def test_the_salon_of_a_preference_survives_normalisation(self):
        salon = uuid.uuid4()
        remembered = Preference(
            PreferenceKind.MASTER, uuid.uuid4(), origin=PreferenceOrigin.CONFIRMED_MEMORY, tenant_ref=salon,
        )

        _, _, soft_memory = effective_preferences((remembered,))

        assert [p.tenant_ref for p in soft_memory] == [salon]
