# flake8: noqa: F811 — фикстуры импортированы из узлов сборки плана и приходят параметрами
"""Каталог принимает реестр планировочных правил 1.x (DRF-2879, WP7 часть 2).

Реестр ``1.0`` ждёт утверждения владельца в ``ayla-knowledge`` (PR #24): в нём
четырнадцатый вид ``PLAN_CADENCE`` с субъектом ``plan_template`` — регулярность
действий шаблона Plan Lite (решение владельца AYLA-DEC-0093). Каталог обязан
уметь его принять РАНЬШЕ, чем он станет источником, — иначе бот останется на
старой копии и план будет судить по реестру, который владелец уже заменил.

Что держат узлы:

* реестр ``1.0`` принимается целиком, ``0.1`` — как прежде;
* неизвестный major отвергается до разбора правил;
* ``PLAN_CADENCE`` и ``plan_template`` идут только парой и только в 1.x;
* **ни одно правило шаблона не попадает в утверждения шага**: регулярность,
  курс и сроки шагу Plan Engine запрещены (§1.1, §4.8) — принять вид в реестре
  не значит применить его к шагу.

Фикстура — файл реестра из ветки PR #24 (``canon/drf2261-stage-c-contracts``,
коммит ``33dc1655``) с записанным sha256. Он НЕ источник истины: если владелец
утвердит другой текст, число здесь сменится вместе с копией.
"""
from __future__ import annotations

import copy
import datetime
import hashlib
from pathlib import Path

import pytest
import yaml

from wellness.models import PLAN_FORBIDDEN_FIELDS
from wellness.plan_compose import (
    PLAN_CADENCE_KIND,
    PLAN_TEMPLATE_SUBJECT,
    RULE_KINDS_BY_MAJOR,
    SUBJECT_KINDS_BY_MAJOR,
    parse_compose_request,
)
from wellness.plan_engine import ContractViolation
from wellness.tests.test_plan_compose_2871 import (  # noqa: F401 — фикстуры того же сценария
    DECISION_URL,
    _api,
    _token_and_flag,
    back,
    body_massage,
    category,
    curator,
    goal,
    knowledge,
    owner,
    relax,
)

pytestmark = pytest.mark.django_db

REGISTRY_1X_FILE = Path(__file__).resolve().parent / "data" / "planning-rules-registry-1.0-pr24.yaml"
REGISTRY_1X_SHA256 = "007da11487970864172d95b6e7c903fc3fd961665a39648e0feb8254ab29ae5d"  # pragma: allowlist secret
WIRE_RULE_KEYS = ("rule_id", "kind", "status", "value", "unit", "applicability", "provenance")


def _file_bytes() -> bytes:
    return REGISTRY_1X_FILE.read_bytes().replace(b"\r\n", b"\n")


def _iso(value):
    if isinstance(value, dict):
        return {k: _iso(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_iso(v) for v in value]
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def _wire() -> dict:
    data = yaml.safe_load(_file_bytes())
    return {
        "registry_version": data["registry_version"],
        "rules": [{key: _iso(rule.get(key)) for key in WIRE_RULE_KEYS} for rule in data["rules"]],
    }


def _body(registry: dict | None = None) -> dict:
    return {
        "safety_state": "NORMAL",
        "safety_policy_version": "registry-1x-node",
        "rules_registry": registry if registry is not None else _wire(),
    }


def _cadence_rule(registry: dict) -> dict:
    return next(r for r in registry["rules"] if r["kind"] == PLAN_CADENCE_KIND)


class TestTheFile:
    def test_it_is_the_file_from_the_knowledge_pr(self) -> None:
        assert hashlib.sha256(_file_bytes()).hexdigest() == REGISTRY_1X_SHA256

    def test_its_closed_lists_are_what_the_catalog_reads_for_major_1(self) -> None:
        data = yaml.safe_load(_file_bytes())
        assert data["registry_version"] == "1.0"
        assert set(data["kinds"]) == RULE_KINDS_BY_MAJOR[1]
        subjects = {r["applicability"]["subject_kind"] for r in data["rules"]}
        assert PLAN_TEMPLATE_SUBJECT in subjects and subjects <= SUBJECT_KINDS_BY_MAJOR[1]
        cadence = [r for r in data["rules"] if r["kind"] == PLAN_CADENCE_KIND]
        assert len(data["rules"]) == 20 and len(cadence) == 7


class TestAcceptance:
    def test_the_whole_registry_parses(self) -> None:
        request = parse_compose_request(_body())
        assert request.registry_version == "1.0"
        assert len(request.rules) == 20

    @pytest.mark.parametrize("version", ["2.0", "10.1", "1", "1.x", "v1.0", "", "one"])
    def test_an_unknown_major_or_shape_is_refused_before_the_rules(self, version) -> None:
        registry = _wire()
        registry["registry_version"] = version
        # Правила намеренно испорчены: отказ обязан прийти по версии, не по ним.
        registry["rules"] = ["not-a-rule"]
        with pytest.raises(ContractViolation) as exc:
            parse_compose_request(_body(registry))
        assert exc.value.reason in ("registry_version_unsupported", "rules_registry_missing")
        if version:
            assert exc.value.reason == "registry_version_unsupported"

    def test_the_fourteenth_kind_is_not_read_under_major_0(self) -> None:
        registry = _wire()
        registry["registry_version"] = "0.1"
        with pytest.raises(ContractViolation) as exc:
            parse_compose_request(_body(registry))
        assert exc.value.reason == "rule_kind_unknown"

    def test_a_plan_template_subject_is_not_read_under_major_0(self) -> None:
        registry = _wire()
        registry["registry_version"] = "0.1"
        registry["rules"] = [r for r in registry["rules"] if r["kind"] != PLAN_CADENCE_KIND]
        registry["rules"][0]["applicability"]["subject_kind"] = PLAN_TEMPLATE_SUBJECT
        with pytest.raises(ContractViolation) as exc:
            parse_compose_request(_body(registry))
        assert exc.value.reason == "rule_subject_kind_unknown"

    def test_a_fifteenth_kind_is_refused(self) -> None:
        registry = _wire()
        _cadence_rule(registry)["kind"] = "PLAN_STREAK"
        with pytest.raises(ContractViolation) as exc:
            parse_compose_request(_body(registry))
        assert exc.value.reason == "rule_kind_unknown"

    def test_an_unknown_subject_kind_is_refused(self) -> None:
        registry = _wire()
        registry["rules"][0]["applicability"]["subject_kind"] = "person"
        with pytest.raises(ContractViolation) as exc:
            parse_compose_request(_body(registry))
        assert exc.value.reason == "rule_subject_kind_unknown"

    @pytest.mark.parametrize("subject", ["canonical_service", "capability", "tenant_offer", "category"])
    def test_plan_cadence_cannot_describe_a_service_as_a_course(self, subject) -> None:
        registry = _wire()
        _cadence_rule(registry)["applicability"]["subject_kind"] = subject
        with pytest.raises(ContractViolation) as exc:
            parse_compose_request(_body(registry))
        assert exc.value.reason == "plan_cadence_subject_mismatch"

    def test_a_plan_template_cannot_carry_a_service_rule(self) -> None:
        registry = _wire()
        duration = next(r for r in registry["rules"] if r["kind"] == "DURATION")
        duration["applicability"]["subject_kind"] = PLAN_TEMPLATE_SUBJECT
        with pytest.raises(ContractViolation) as exc:
            parse_compose_request(_body(registry))
        assert exc.value.reason == "plan_cadence_subject_mismatch"


class TestCadenceNeverReachesAStep:
    def test_a_plan_composed_under_1x_has_no_template_rule_on_any_step(self, goal, knowledge) -> None:
        resp = _api().post(DECISION_URL, _body(), format="json")
        assert resp.status_code == 200, resp.content
        decision = resp.json()["data"]["decision"]
        assert decision["policy_versions"]["constraint_policy_version"] == "1.0"
        kinds = {a["kind"] for a in decision["assertions"]}
        # Присутствие раньше отсутствия: утверждения есть, и они — способностные.
        assert kinds == {"CAPABILITY_SEMANTICS", "SERVICE_CAPABILITY_MAPPING"}
        assert PLAN_CADENCE_KIND not in kinds
        flat = repr(decision)
        assert "per_week" not in flat and "target_count" not in flat

        def keys(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from keys(v)
            elif isinstance(node, list):
                for item in node:
                    yield from keys(item)

        assert not set(keys(decision)) & PLAN_FORBIDDEN_FIELDS

    def test_a_cadence_rule_naming_the_goal_still_does_not_apply(self, goal, knowledge, relax) -> None:
        """Шаблон назван по цели человека — и всё равно не про шаг: субъект правила
        — шаблон плана, а шаг — способность."""
        registry = _wire()
        _cadence_rule(registry)["applicability"]["subject_ids"] = [relax.key, "muscle-tension-relief"]
        decision = _api().post(DECISION_URL, _body(registry), format="json").json()["data"]["decision"]
        assert PLAN_CADENCE_KIND not in {a["kind"] for a in decision["assertions"]}

    def test_the_same_plan_comes_out_under_0x_and_1x(self, goal, knowledge) -> None:
        """Новая версия реестра добавила правила о шаблонах — плану из
        способностей они ничего не меняют."""
        one = _api().post(DECISION_URL, _body(), format="json").json()["data"]["decision"]
        old = copy.deepcopy(_wire())
        old["registry_version"] = "0.1"
        old["rules"] = [r for r in old["rules"] if r["kind"] != PLAN_CADENCE_KIND]
        zero = _api().post(DECISION_URL, _body(old), format="json").json()["data"]["decision"]

        def shape(decision: dict) -> list:
            by_id = {a["assertion_id"]: a for a in decision["assertions"]}
            return [
                (s["capability_ref"], s["role"], s["level"], sorted(by_id[a]["provenance"]["rule_id"] for a in s["assertions"]))
                for s in decision["steps"]
            ]

        assert shape(one) == shape(zero)
        assert one["validation"]["status"] == zero["validation"]["status"] == "INCOMPLETE"
