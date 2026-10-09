"""DRF-2855 — мастер из памяти по ИМЕНИ на границе `resolve` (ядро DRF-2831).

Бот хранит имя, а не id («имя, не ID», решение владельца 28.09), и искать его
по всему каталогу не вправе (NEVER_CROSSES). Разрешает имя каталог:

- только в режиме «свои салоны», только среди мастеров салонов клиента;
- ровно одно совпадение — обычное мягкое предпочтение из памяти;
- ноль или несколько — не применяется, никого не выбираем, ответ несёт
  ``preference_resolution`` для уточнения у клиента;
- ``source_tenant_id`` сужает поиск так же, как сужает предпочтение по id;
- ни имени, ни id в ответе о разрешении и в журнале — только исход и число.
"""
from __future__ import annotations

import logging

import pytest
from rest_framework.test import APIClient

from recommendation import views as resolve_views
from recommendation._serializers import ResolveResponseSerializer, decision_to_payload
from recommendation.api import MEMORY_SOURCE_GLOBAL_BOT, RESOLVER_SPEC_VERSION, resolve
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User
from users.own_salons import masters_matching_name

from .conftest import StaticSource, make_facts, make_request

VALID_TOKEN = "test-ayla-internal-token-master-name"
URL = "/api/v1/internal/recommendation/resolve/"
EXTERNAL_USER_ID = "bot:master-name"

_CANDIDATES: list = []

pytestmark = pytest.mark.django_db


def master_name_source_factory(*, viewer=None):
    return StaticSource(_CANDIDATES)


@pytest.fixture(autouse=True)
def _boundary(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.RECOMMENDATION_CANDIDATE_SOURCE = (
        "recommendation.tests.test_master_name_resolution_2855.master_name_source_factory"
    )
    _CANDIDATES.clear()
    yield
    _CANDIDATES.clear()


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client", phone="+79992855001", is_proxy=True,
    )


_SEQ = iter(range(100, 1000))


def _salon(tag: str, *, own_of=None) -> Tenant:
    tenant = Tenant.objects.create(slug=f"n2855-{tag}", name=f"Салон {tag}", is_active=True)
    if own_of is not None:
        TenantUserRelationship.objects.create(
            user=own_of, tenant=tenant, role=TenantUserRelationship.Role.CUSTOMER, is_active=True,
        )
    return tenant


def _master(tenant, display_name: str, *, sellable=True) -> SpecialistProfile:
    """Настоящий мастер салона и кандидат подбора с тем же ключом пользователя."""
    n = next(_SEQ)
    user = User.objects.create_user(
        username=f"n2855-m{n}", password="x", role="specialist", phone=f"+79992855{n}",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = tenant
    profile.display_name = display_name
    profile.is_available = sellable
    profile.is_booking_enabled = True
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.save()
    _CANDIDATES.append(make_facts(cid=user.id, tenant_ref=tenant.id))
    return profile


@pytest.fixture
def salons(customer):
    """A и B — свои салоны клиента, C — чужой."""
    return {"a": _salon("a", own_of=customer), "b": _salon("b", own_of=customer), "c": _salon("c")}


@pytest.fixture
def resolve_log(caplog):
    with caplog.at_level(logging.INFO, logger=resolve_views.__name__):
        yield caplog


def _by_name(name: str, *stems: str, **extra) -> dict:
    return {
        "kind": "master", "name": name, "name_stems": list(stems) or [name.casefold()],
        "origin": "confirmed_memory", **extra,
    }


def _post(**overrides):
    body = {
        "request_id": "req-2855",
        "surface": "MINIAPP_HOME",
        "scope": {"mode": "OWN_SALONS"},
        "need": {"origin": "USER_EXPLICIT", "raw_text": "массаж"},
        "safety_state": "NORMAL",
        "tie_break_seed": "conv-2855",
        "k": 10,
    }
    body.update(overrides)
    client = APIClient()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    client.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
    return client.post(URL, body, format="json")


def _data(**overrides) -> dict:
    response = _post(**overrides)
    assert response.status_code == 200, response.content
    return response.json()["data"]


def _tiers(data) -> dict[str, int]:
    return {row["candidate"]["id"]: row["tier"] for row in data["ordered"]}


def _is_raised(data, master) -> bool:
    tiers, favourite = _tiers(data), str(master.user_id)
    return tiers[favourite] < min(t for cid, t in tiers.items() if cid != favourite)


def _nobody_is_raised(data) -> bool:
    return len(set(_tiers(data).values())) == 1


class TestExactlyOne:
    def test_the_only_anna_of_the_clients_salons_is_raised(self, salons):
        anna = _master(salons["a"], "Анна Петрова")
        _master(salons["a"], "Мария Сидорова")
        _master(salons["b"], "Ольга Кузнецова")

        data = _data(preferences=[_by_name("Анна", "анн")])

        assert data["preference_resolution"] == [{"kind": "master", "status": "resolved", "matches": 1}]
        assert _is_raised(data, anna)

    def test_an_inflected_name_resolves_through_its_stem(self, salons):
        """«к Анне»: в памяти склонённое имя, основу дал бот — морфологии в каталоге нет."""
        anna = _master(salons["a"], "Анна")
        _master(salons["a"], "Мария")

        data = _data(preferences=[_by_name("Анне", "анн")])

        assert data["preference_resolution"][0]["status"] == "resolved"
        assert _is_raised(data, anna)

    def test_every_stem_must_be_answered(self, salons):
        """Имя и фамилия: вторая Анна с другой фамилией не делает ответ двусмысленным."""
        petrova = _master(salons["a"], "Анна Петрова")
        _master(salons["a"], "Анна Сидорова")

        data = _data(preferences=[_by_name("Анна Петрова", "анн", "петров")])

        assert data["preference_resolution"] == [{"kind": "master", "status": "resolved", "matches": 1}]
        assert _is_raised(data, petrova)

    @pytest.mark.parametrize(("stored", "stem"), [("Алёна", "ален"), ("Алена", "алён"), ("АЛЁНА", "Алён")])
    def test_yo_and_ye_are_one_letter_on_both_sides(self, salons, stored, stem):
        alena = _master(salons["a"], stored)
        _master(salons["a"], "Мария")

        data = _data(preferences=[_by_name("Алёне", stem)])

        assert data["preference_resolution"][0]["status"] == "resolved"
        assert _is_raised(data, alena)

    def test_the_case_of_the_stored_name_does_not_matter(self, salons):
        anna = _master(salons["a"], "АННА")
        _master(salons["a"], "Мария")

        assert _is_raised(_data(preferences=[_by_name("анна", "анн")]), anna)


class TestZeroOrSeveral:
    def test_two_annas_in_the_clients_salons_are_ambiguous_and_nobody_is_raised(self, salons):
        _master(salons["a"], "Анна Петрова")
        _master(salons["b"], "Анна Сидорова")
        _master(salons["b"], "Мария")

        data = _data(preferences=[_by_name("Анна", "анн")])

        assert data["preference_resolution"] == [{"kind": "master", "status": "ambiguous", "matches": 2}]
        assert len(_tiers(data)) == 3
        assert _nobody_is_raised(data)

    def test_an_anna_only_in_a_foreign_salon_is_not_found(self, salons):
        """Межсалонного разрешения имени нет: Анна чужого салона — не Анна клиента."""
        foreign_anna = _master(salons["c"], "Анна")
        _master(salons["a"], "Мария")
        _master(salons["a"], "Ольга")

        data = _data(preferences=[_by_name("Анна", "анн")])

        assert data["preference_resolution"] == [{"kind": "master", "status": "not_found", "matches": 0}]
        assert str(foreign_anna.user_id) not in _tiers(data)
        assert _nobody_is_raised(data)

    def test_a_foreign_namesake_does_not_make_the_own_one_ambiguous(self, salons):
        anna = _master(salons["a"], "Анна")
        _master(salons["a"], "Мария")
        _master(salons["c"], "Анна")

        data = _data(preferences=[_by_name("Анна", "анн")])

        assert data["preference_resolution"] == [{"kind": "master", "status": "resolved", "matches": 1}]
        assert _is_raised(data, anna)

    def test_a_namesake_who_cannot_be_recommended_still_counts(self, salons):
        """Поиск идёт до гейтов подбора: «две Анны, одна закрыта» — двусмысленно, а не «одна»."""
        _master(salons["a"], "Анна Петрова")
        closed = _master(salons["a"], "Анна Сидорова")
        _CANDIDATES[:] = [f for f in _CANDIDATES if f.ref.id != closed.user_id]
        _master(salons["a"], "Мария")

        data = _data(preferences=[_by_name("Анна", "анн")])

        assert data["preference_resolution"] == [{"kind": "master", "status": "ambiguous", "matches": 2}]
        assert _nobody_is_raised(data)

    def test_a_master_who_is_not_sellable_is_not_a_match(self, salons):
        _master(salons["a"], "Анна", sellable=False)
        _master(salons["a"], "Мария")

        data = _data(preferences=[_by_name("Анна", "анн")])

        assert data["preference_resolution"] == [{"kind": "master", "status": "not_found", "matches": 0}]


class TestWhereTheMemoryWasWritten:
    def test_a_salon_id_searches_only_that_salon(self, salons):
        anna_a = _master(salons["a"], "Анна Петрова")
        _master(salons["b"], "Анна Сидорова")
        _master(salons["b"], "Мария")

        data = _data(preferences=[_by_name("Анна", "анн", source_tenant_id=str(salons["a"].id))])

        assert data["preference_resolution"] == [{"kind": "master", "status": "resolved", "matches": 1}]
        assert _is_raised(data, anna_a)

    def test_the_global_bot_marker_searches_every_own_salon(self, salons):
        _master(salons["a"], "Анна Петрова")
        _master(salons["b"], "Анна Сидорова")

        data = _data(preferences=[_by_name("Анна", "анн", source_tenant_id=MEMORY_SOURCE_GLOBAL_BOT)])

        assert data["preference_resolution"] == [{"kind": "master", "status": "ambiguous", "matches": 2}]

    @pytest.mark.parametrize("source", ["null", "garbage", "foreign"])
    def test_memory_of_unknown_or_foreign_origin_is_not_even_looked_up(self, salons, source):
        """Одна Анна в своих салонах есть — и всё равно не resolved: имя не искали."""
        _master(salons["a"], "Анна")
        _master(salons["a"], "Мария")
        _master(salons["c"], "Анна")
        value = {"null": None, "garbage": "somewhere", "foreign": str(salons["c"].id)}[source]

        data = _data(preferences=[_by_name("Анна", "анн", source_tenant_id=value)])

        assert data["preference_resolution"] == [{"kind": "master", "status": "not_applicable", "matches": 0}]
        assert _nobody_is_raised(data)


class TestOnlyInOwnSalons:
    def test_a_name_is_dropped_outside_own_salons_and_the_log_does_not_carry_it(self, salons, resolve_log):
        _master(salons["a"], "Анна")
        _master(salons["a"], "Мария")

        data = _data(scope={"mode": "MARKETPLACE"}, preferences=[_by_name("Анна-Редкоеимя", "анн")])

        assert data["preference_resolution"] == [{"kind": "master", "status": "not_applicable", "matches": 0}]
        assert _nobody_is_raised(data)
        warnings = [r.getMessage() for r in resolve_log.records if r.levelno == logging.WARNING]
        assert any("name_dropped" in message for message in warnings)
        assert "едкоеимя" not in resolve_log.text.casefold()

    def test_a_request_without_names_carries_an_empty_resolution(self, salons):
        _master(salons["a"], "Анна")

        assert _data()["preference_resolution"] == []


class TestNothingAboutThePersonLeaks:
    def test_the_log_carries_only_the_outcome_and_the_count(self, salons, resolve_log):
        anna = _master(salons["a"], "Анна Редкаяфамилия")
        _master(salons["a"], "Мария")

        _data(preferences=[_by_name("Анна Редкаяфамилия", "анн", "редкаяфамил")])

        lines = [r.getMessage() for r in resolve_log.records if "name_resolution" in r.getMessage()]
        assert lines == ["recommendation.preference.name_resolution status=resolved matches=1"]
        text = resolve_log.text.casefold()
        assert "редкаяфамил" not in text
        assert str(anna.user_id) not in " ".join(lines)

    def test_the_resolution_entry_has_exactly_three_keys(self, salons):
        _master(salons["a"], "Анна")
        _master(salons["b"], "Анна")

        [entry] = _data(preferences=[_by_name("Анна", "анн")])["preference_resolution"]

        assert set(entry) == {"kind", "status", "matches"}

    def test_rows_of_another_own_salon_carry_no_trace_of_the_favourite(self, salons):
        """Полка из двух салонов: строка мастера салона B не несёт кодов про Анну из A."""
        anna = _master(salons["a"], "Анна")
        other = _master(salons["b"], "Мария")

        data = _data(preferences=[_by_name("Анна", "анн")])

        rows = {row["candidate"]["id"]: row for row in data["ordered"]}
        favourite_codes = {c for c in rows[str(anna.user_id)]["reason_codes"] if "PREFERENCE" in c}
        assert favourite_codes, "контроль: у самой Анны код предпочтения есть"
        assert not [c for c in rows[str(other.user_id)]["reason_codes"] if "PREFERENCE" in c]
        assert str(anna.user_id) not in str(rows[str(other.user_id)])


class TestTheContract:
    @pytest.mark.parametrize(
        "preference",
        [
            pytest.param({"kind": "master", "origin": "confirmed_memory"}, id="neither-ref-nor-name"),
            pytest.param(
                {"kind": "master", "origin": "confirmed_memory", "name": "Анна", "name_stems": ["анн"],
                 "ref": "5f0c1c6e-6d0b-4b0e-9a57-3f2f1f6f0001"},
                id="both-ref-and-name",
            ),
            pytest.param({"kind": "master", "origin": "confirmed_memory", "name": "Анна"}, id="a-name-without-stems"),
            pytest.param(
                {"kind": "master", "origin": "confirmed_memory", "name": "Анна", "name_stems": []}, id="no-stems",
            ),
            pytest.param(
                {"kind": "master", "origin": "confirmed_memory", "name": "Ан", "name_stems": ["ан"]},
                id="a-stem-too-short",
            ),
            pytest.param(
                {"kind": "master", "origin": "confirmed_memory", "name_stems": ["анн"],
                 "ref": "5f0c1c6e-6d0b-4b0e-9a57-3f2f1f6f0001"},
                id="stems-without-a-name",
            ),
            pytest.param(
                {"kind": "salon", "origin": "confirmed_memory", "name": "Салон", "name_stems": ["салон"]},
                id="a-salon-by-name",
            ),
            pytest.param(
                {"kind": "master", "origin": "current_request", "name": "Анна", "name_stems": ["анн"]},
                id="a-name-said-now",
            ),
        ],
    )
    def test_a_malformed_preference_is_refused(self, salons, preference):
        assert _post(preferences=[preference]).status_code == 400

    def test_positive_control_a_preference_by_id_still_works(self, salons):
        anna = _master(salons["a"], "Анна")
        _master(salons["a"], "Мария")

        data = _data(preferences=[{"kind": "master", "ref": str(anna.user_id), "origin": "confirmed_memory"}])

        assert data["preference_resolution"] == []
        assert _is_raised(data, anna)

    def test_several_names_are_answered_in_the_order_they_were_sent(self, salons):
        _master(salons["a"], "Анна")
        _master(salons["a"], "Мария Иванова")
        _master(salons["b"], "Мария Петрова")

        data = _data(preferences=[
            _by_name("Ольга", "ольг"), _by_name("Мария", "мари"),
            _by_name("Анна", "анн", source_tenant_id=None), _by_name("Анна", "анн"),
        ])

        assert [(e["status"], e["matches"]) for e in data["preference_resolution"]] == [
            ("not_found", 0), ("ambiguous", 2), ("not_applicable", 0), ("resolved", 1),
        ]

    def test_an_explicit_strength_travels_with_a_name(self, salons):
        anna = _master(salons["a"], "Анна")
        _master(salons["a"], "Мария")

        data = _data(preferences=[_by_name("Анна", "анн", strength="soft", source_tenant_id=str(salons["a"].id))])

        assert _is_raised(data, anna)

    def test_the_field_is_part_of_the_response_schema_and_the_version_moved(self):
        payload = decision_to_payload(resolve(make_request(), source=StaticSource([make_facts()])))

        assert payload["preference_resolution"] == []
        assert ResolveResponseSerializer(data=payload).is_valid()
        without = {k: v for k, v in payload.items() if k != "preference_resolution"}
        assert not ResolveResponseSerializer(data=without).is_valid()
        assert tuple(int(p) for p in RESOLVER_SPEC_VERSION.split(".")[:2]) >= (1, 2)


class TestTheSearchItself:
    def test_no_salons_means_nobody(self, salons):
        _master(salons["a"], "Анна")

        assert masters_matching_name(("анн",), ()) == ()

    def test_no_stems_means_nobody(self, salons):
        _master(salons["a"], "Анна")

        assert masters_matching_name((), (salons["a"].id,)) == ()

    def test_a_stem_is_matched_inside_a_word_like_the_bot_does(self, salons):
        """Бот ищет мастера вхождением основы; каталог отвечает тем же множеством."""
        anna = _master(salons["a"], "Анна")
        zhanna = _master(salons["a"], "Жанна")

        assert set(masters_matching_name(("анн",), (salons["a"].id,))) == {anna.user_id, zhanna.user_id}

    def test_stems_do_not_join_across_words(self, salons):
        _master(salons["a"], "Яна Нина")

        assert masters_matching_name(("анин",), (salons["a"].id,)) == ()
