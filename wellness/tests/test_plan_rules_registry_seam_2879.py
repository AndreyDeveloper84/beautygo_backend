# flake8: noqa: F811 — фикстуры импортированы из узлов сборки плана и приходят параметрами
"""Шов «реестр планировочных правил бота → сборка плана каталога» (DRF-2879, WP7 часть 1).

Бот вендорит реестр из ``ayla-knowledge`` и шлёт его правила в тело
``POST /internal/me/plan/decision/``. Каталог отвергает запрос целиком, если
реестр не конформен. Две проверки живут в двух репозиториях и могут разойтись
молча — поэтому обе стороны держат узел на ОДНОМ И ТОМ ЖЕ файле:

* здесь — ``wellness/tests/data/planning-rules-registry.yaml``, побайтная копия
  ``ai-bot-platform apps/planning_rules/data/planning-rules-registry.yaml``;
* число — sha256 этого файла, оно же записано в ``source.json`` бота и в узле
  бота (PR ai-bot-platform#2342).

Когда источник сменится, обе стороны покраснеют на одном числе, и копию
придётся завезти осознанно — вместе с проверкой, что каталог её принимает.

Тело собирается здесь так же, как его собирает бот: семь ключей правила,
``as_of`` — строка ISO, ``note`` не шлётся. Сам сериализатор — код бота; узел
его не импортирует и не подменяет, он проверяет СВОЮ сторону на настоящем файле.
"""
from __future__ import annotations

import datetime
import hashlib
from pathlib import Path

import pytest
import yaml

from wellness.plan_compose import CAPABILITY_LEVEL_KINDS, RULE_KINDS_BY_MAJOR, parse_compose_request
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

REGISTRY_FILE = Path(__file__).resolve().parent / "data" / "planning-rules-registry.yaml"
#: sha256 файла в источнике (``ayla-knowledge@main``, синхронизация бота 08.09.2026).
REGISTRY_SHA256 = "7cebfa0370d95337cb2d7299b45f35826a72dda4d2bd0b198c4b4041630ed47a"  # pragma: allowlist secret
WIRE_RULE_KEYS = ("rule_id", "kind", "status", "value", "unit", "applicability", "provenance")


def _file_bytes() -> bytes:
    # Рабочая копия под Windows с autocrlf приходит в CRLF; в репозитории и в
    # CI файл лежит в LF, как в источнике. Число считается по LF.
    return REGISTRY_FILE.read_bytes().replace(b"\r\n", b"\n")


def _iso(value):
    if isinstance(value, dict):
        return {k: _iso(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_iso(v) for v in value]
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def _wire_registry() -> dict:
    data = yaml.safe_load(_file_bytes())
    return {
        "registry_version": data["registry_version"],
        "rules": [{key: _iso(rule.get(key)) for key in WIRE_RULE_KEYS} for rule in data["rules"]],
    }


def _body(registry: dict | None = None) -> dict:
    return {
        "safety_state": "NORMAL",
        "safety_policy_version": "seam-node",
        "rules_registry": registry if registry is not None else _wire_registry(),
        "excluded_capability_refs": [],
    }


class TestTheFile:
    def test_it_is_the_file_the_bot_vendors(self) -> None:
        assert hashlib.sha256(_file_bytes()).hexdigest() == REGISTRY_SHA256

    def test_its_closed_lists_are_the_catalogs(self) -> None:
        data = yaml.safe_load(_file_bytes())
        assert set(data["kinds"]) == RULE_KINDS_BY_MAJOR[0]
        assert data["registry_version"] == "0.1"
        assert len(data["rules"]) == 13

    def test_dates_cross_the_wire_as_strings(self) -> None:
        raw = yaml.safe_load(_file_bytes())["rules"]
        # Присутствие раньше отсутствия: в файле as_of — дата, не строка.
        assert any(isinstance((r["value"] or {}).get("as_of"), datetime.date) for r in raw)
        for rule in _wire_registry()["rules"]:
            if rule["status"] == "UNKNOWN":
                assert isinstance(rule["value"]["as_of"], str)


class TestTheCatalogAcceptsIt:
    def test_the_real_registry_parses(self) -> None:
        request = parse_compose_request(_body())
        assert request.registry_version == "0.1"
        assert len(request.rules) == 13
        present = {r["kind"] for r in request.rules if r["applicability"]["subject_kind"] == "capability"}
        assert present == set(CAPABILITY_LEVEL_KINDS)

    def test_a_plan_is_composed_from_it(self, goal, knowledge) -> None:
        resp = _api().post(DECISION_URL, _body(), format="json")
        assert resp.status_code == 200, resp.content
        data = resp.json()["data"]
        assert data["outcome"] == "PLAN"
        decision = data["decision"]
        assert decision["policy_versions"]["constraint_policy_version"] == "0.1"
        by_id = {a["assertion_id"]: a for a in decision["assertions"]}
        for step in decision["steps"]:
            rule_ids = sorted(by_id[a]["provenance"]["rule_id"] for a in step["assertions"])
            # Из тринадцати правил к шагу-способности применимы ровно два.
            assert rule_ids == ["PR-CAPABILITY_SEMANTICS-0001", "PR-SERVICE_CAPABILITY_MAPPING-0001"]
        assert decision["validation"]["status"] == "INCOMPLETE"

    def test_the_node_would_notice_a_registry_the_catalog_refuses(self) -> None:
        """Контроль: тот же файл без одного способностного правила — отказ.
        Без этого «реестр принят» был бы зелёным и при выключенной проверке."""
        registry = _wire_registry()
        registry["rules"] = [r for r in registry["rules"] if r["kind"] != "CAPABILITY_SEMANTICS"]
        with pytest.raises(ContractViolation) as exc:
            parse_compose_request(_body(registry))
        assert exc.value.reason == "rules_registry_incomplete"

    def test_a_date_left_as_an_object_would_not_survive_json(self) -> None:
        """Почему сериализатор обязан переводить дату: объект даты в JSON не уходит."""
        import json

        raw = yaml.safe_load(_file_bytes())["rules"][0]
        with pytest.raises(TypeError):
            json.dumps(raw)
        json.dumps(_wire_registry()["rules"][0])
