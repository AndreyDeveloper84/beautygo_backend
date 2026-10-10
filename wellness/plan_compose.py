"""Plan Engine — сборка и проверка плана на уровне способностей (DRF-2871, WP4).

Контракт PLAN_ENGINE_CONTRACT v1.0 §1.2, §3.1, §4.1–§4.2, §5, §6, §7, §12;
контракт планировочных ограничений v1.0 §2–§5, §8. Место — каталог, по
пилотной поправке §10.1 («Plan Engine следует тому же решению о месте, что и
резолвер»): здесь лежат курируемые связи «цель → способность».

Детерминированно, без модели. Что приходит снаружи и не вычисляется здесь:

* ``safety_state`` и версия политики безопасности — от бота (движок
  безопасности живёт там; так же его принимает резолвер);
* снимок реестра планировочных правил и его версия — от бота (реестр
  вендорится туда из ayla-knowledge).

Что собирается:

* шаг на каждую способность, о которой ПОДТВЕРЖДЕНО, что она помогает цели
  человека (``services.capabilities.procedures_by_capability_helping_goal``);
* план — только когда способности нельзя получить одной процедурой: иначе
  потребность сводится к одной услуге (§6.4), и ответ — ``PLAN_NOT_JUSTIFIED``;
* роль — всегда ``OPTIONAL``: связь говорит «помогает», а не «без неё цель
  недостижима»; знания об обязательности нет, а ложный ``CORE`` навязывает
  (§5.1–§5.2). ``CORE`` не присваивается никому;
* уровень — всегда ``CAPABILITY`` (PE-6); вниз шаг идёт отдельным событием
  (``wellness.plan_engine_steps.resolve_step``);
* утверждения — только из присланного реестра, каждое с полным провенансом;
  проверка шага — таблица §8.1.

``ALTERNATIVE`` здесь не возникает: альтернатива — другой шаг ТОЙ ЖЕ
способности (§5.3), а на уровне способности два таких шага ничем не
различались бы.

Решение эфемерно (§4.1): ничего не сохраняется. Сохраняет команда
``wellness.plan_engine.create_plan_from_command`` по подтверждению человека,
и ``decision`` отсюда принимается ею без переделки.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from django.utils import timezone

from goals.models import ClientGoal
from recommendation.api import REASON_CODE_REGISTRY_VERSION, RESOLVER_SPEC_VERSION
from services.capabilities import (
    procedures_by_capability_helping_goal,
    synthetic_capability_keys_helping_goal,
)
from services.synthetic import grant_for

from .plan_engine import ContractViolation, PlanEngineDisabled, plan_engine_enabled
from .plan_safety import SAFETY_BLOCKING, SAFETY_STATES

PLAN_SPEC_VERSION = "1.0"
#: У сопоставления каталога нет схемы версий (контракт ограничений §5.3:
#: ``updated_at`` — метка времени, не версия). Названо как есть, не выдумано.
CATALOG_MAPPING_UNVERSIONED = "unversioned"

#: Закрытые перечни контракта ограничений §3, §4.2 и реестра.
RULE_KINDS: frozenset[str] = frozenset(
    {
        "CAPABILITY_SEMANTICS", "SERVICE_CAPABILITY_MAPPING", "DURATION", "EVENT_WINDOW",
        "MIN_INTERVAL", "MAX_INTERVAL", "REPETITION", "SEQUENCE", "PRECONDITION",
        "COMPATIBILITY", "INCOMPATIBILITY", "RECOVERY_WINDOW", "SAFETY_CONSTRAINT",
    }
)
RULE_STATUSES: frozenset[str] = frozenset({"KNOWN", "UNKNOWN", "INTENTIONALLY_UNSUPPORTED"})
UNKNOWN_REASONS: frozenset[str] = frozenset(
    {
        "NO_RULE_EXISTS", "RULE_NOT_APPLICABLE", "SOURCE_UNREACHABLE", "SOURCE_STALE",
        "MAPPING_MISSING", "POLICY_WITHHELD",
    }
)
#: Виды, чей субъект — способность: только они применимы к шагу уровня
#: CAPABILITY. Реестр обязан нести правило по каждому — иначе о шаге нечего
#: утверждать, а молчание читалось бы как «ограничений нет».
CAPABILITY_LEVEL_KINDS: tuple[str, ...] = ("CAPABILITY_SEMANTICS", "SERVICE_CAPABILITY_MAPPING")

#: Состояния безопасности и те из них, при которых план не строится (§12), —
#: общие со всеми действиями плана: ``wellness.plan_safety``.


class Outcome:
    """Исход сборки. ``decision`` есть только у ``PLAN``."""

    PLAN = "PLAN"
    #: §12: вход безопасности STOP / UNKNOWN.
    SAFETY_BLOCKED = "SAFETY_BLOCKED"
    #: Нет действующей цели с курируемым ключом — декомпозировать нечего.
    NO_GOAL = "NO_GOAL"
    #: Подтверждённых способностей под цель нет — честное «плана нет» (§6.2).
    NO_CURATED_DECOMPOSITION = "NO_CURATED_DECOMPOSITION"
    #: §1.2, §6.4: потребность сводится к одной услуге — плана не нужно, это
    #: обычная рекомендация. ``details.reason`` называет, почему именно.
    PLAN_NOT_JUSTIFIED = "PLAN_NOT_JUSTIFIED"


#: Причины ``PLAN_NOT_JUSTIFIED``.
NOT_JUSTIFIED_SINGLE_CAPABILITY = "single_capability"
NOT_JUSTIFIED_ONE_PROCEDURE_COVERS_ALL = "one_procedure_covers_all"


@dataclass(frozen=True)
class ComposeRequest:
    safety_state: str
    safety_policy_version: str
    registry_version: str
    rules: tuple[dict, ...]
    excluded_capability_refs: frozenset[str]


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validated_rule(rule: Any) -> dict:
    """Одно правило реестра в форме контракта ограничений §4–§5. Правило без
    полного провенанса утверждением стать не может (PE-5) — отказ целиком."""
    if not isinstance(rule, dict):
        raise ContractViolation("rule_not_object")
    rule_id = rule.get("rule_id")
    if not _text(rule_id):
        raise ContractViolation("rule_id_missing")
    if rule.get("kind") not in RULE_KINDS:
        raise ContractViolation("rule_kind_unknown", str(rule_id))
    status = rule.get("status")
    if status not in RULE_STATUSES:
        raise ContractViolation("rule_status_unknown", rule_id)
    provenance = rule.get("provenance")
    if not isinstance(provenance, dict) or not _text(provenance.get("source")) or not _text(provenance.get("version")):
        raise ContractViolation("rule_provenance_missing", rule_id)
    applicability = rule.get("applicability")
    if (
        not isinstance(applicability, dict)
        or not _text(applicability.get("scope"))
        or not _text(applicability.get("subject_kind"))
        or not isinstance(applicability.get("subject_ids", []), list)
    ):
        raise ContractViolation("rule_applicability_missing", rule_id)
    value = rule.get("value")
    if status == "UNKNOWN":
        if (
            not isinstance(value, dict)
            or value.get("reason") not in UNKNOWN_REASONS
            or not _text(value.get("asked_source"))
            or not _text(str(value.get("as_of") or ""))
        ):
            # Контракт §4: отсутствие значения — ошибка сборки, а не UNKNOWN.
            raise ContractViolation("rule_unknown_value_malformed", rule_id)
    elif status == "KNOWN" and value is None:
        raise ContractViolation("rule_known_value_missing", rule_id)
    return rule


def parse_compose_request(raw: Any) -> ComposeRequest:
    if not isinstance(raw, dict):
        raise ContractViolation("request_not_object")
    safety_state = raw.get("safety_state")
    if safety_state not in SAFETY_STATES:
        # В том числе NOT_APPLICABLE: в PlanDecision такого состояния нет, и
        # принять его значило бы молча счесть безопасность проверенной.
        raise ContractViolation("safety_state_invalid", str(safety_state))
    if not _text(raw.get("safety_policy_version")):
        raise ContractViolation("safety_policy_version_missing")
    registry = raw.get("rules_registry")
    if not isinstance(registry, dict) or not _text(registry.get("registry_version")):
        raise ContractViolation("rules_registry_missing")
    rules = registry.get("rules")
    if not isinstance(rules, list):
        raise ContractViolation("rules_registry_missing")
    validated = tuple(_validated_rule(r) for r in rules)
    ids = [r["rule_id"] for r in validated]
    if len(set(ids)) != len(ids):
        raise ContractViolation("rule_id_duplicate")
    present = {r["kind"] for r in validated if r["applicability"]["subject_kind"] == "capability"}
    missing = [k for k in CAPABILITY_LEVEL_KINDS if k not in present]
    if missing:
        raise ContractViolation("rules_registry_incomplete", missing[0])
    excluded = raw.get("excluded_capability_refs", [])
    if not isinstance(excluded, list) or not all(_text(x) for x in excluded):
        raise ContractViolation("excluded_capability_refs_malformed")
    return ComposeRequest(
        safety_state=safety_state,
        safety_policy_version=raw["safety_policy_version"].strip(),
        registry_version=registry["registry_version"].strip(),
        rules=validated,
        excluded_capability_refs=frozenset(excluded),
    )


def _applies(rule: dict, capability_ref: str) -> bool:
    """Правило применимо к шагу уровня CAPABILITY: субъект — способность, и
    либо оно общее (``subject_ids`` пуст), либо называет эту способность."""
    applicability = rule["applicability"]
    if applicability["subject_kind"] != "capability":
        return False
    subject_ids = applicability.get("subject_ids") or []
    return not subject_ids or capability_ref in subject_ids


def _assertion(rule: dict, capability_ref: str) -> dict:
    """``PlanningAssertion`` (контракт ограничений §2): вид, субъект, значение
    ``Known | Unknown(reason, asked_source, as_of)``, провенанс из четырёх полей."""
    if rule["status"] == "KNOWN":
        value: dict[str, Any] = {"state": "Known", "value": rule["value"], "unit": rule.get("unit")}
    elif rule["status"] == "UNKNOWN":
        value = {
            "state": "Unknown",
            "reason": rule["value"]["reason"],
            "asked_source": rule["value"]["asked_source"],
            "as_of": str(rule["value"]["as_of"]),
        }
    else:  # INTENTIONALLY_UNSUPPORTED — значения нет по решению владельца
        value = {
            "state": "Unknown",
            "reason": "POLICY_WITHHELD",
            "asked_source": rule["provenance"]["source"],
            "as_of": timezone.localdate().isoformat(),
        }
    return {
        "assertion_id": f"{rule['rule_id']}@{capability_ref}",
        "kind": rule["kind"],
        "subject": {"capability_id": capability_ref},
        "value": value,
        "provenance": {
            "rule_id": rule["rule_id"],
            "source": rule["provenance"]["source"],
            "version": rule["provenance"]["version"],
            "applicability": rule["applicability"],
        },
    }


def _step_verdict(assertions: list[dict]) -> str:
    """Таблица §8.1 для шага уровня CAPABILITY. Все применимые здесь виды
    существенны: неизвестная семантика способности или неизвестное
    сопоставление с услугой меняют допустимость шага. Запрещающих ``Known`` и
    ``SAFETY_CONSTRAINT`` на этом уровне нет — их субъект канон, не способность."""
    if any(a["value"]["state"] == "Unknown" for a in assertions):
        return "INCOMPLETE"
    return "VALID"


def _nothing(outcome: str, request: ComposeRequest | None = None, **details: Any) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "decision": None,
        "safety_state": request.safety_state if request else None,
        "details": details,
    }


def _not_justified_reason(capability_refs: list[str], procedures: dict[str, frozenset]) -> str | None:
    """Почему из этих способностей плана не выходит; ``None`` — выходит.

    Контракт §6.4 судит не о числе способностей, а о том, **сводится ли
    потребность к одной услуге**. Две способности, которые несёт одна и та же
    процедура, закрываются одним визитом: два массажа под цель — это два
    варианта одного шага, а не два шага. План оправдан, только когда ни одна
    процедура не несёт все его способности сразу.

    Считать разные ключи вместо этого — заменить правило удобным числом: под
    такое условие начинают подбирать «вторую способность», и план строится
    ради формы (решение владельца 07.10).
    """
    if len(capability_refs) < 2:
        return NOT_JUSTIFIED_SINGLE_CAPABILITY
    if frozenset.intersection(*(procedures[ref] for ref in capability_refs)):
        return NOT_JUSTIFIED_ONE_PROCEDURE_COVERS_ALL
    return None


def compose_plan(user, request: ComposeRequest) -> dict[str, Any]:
    """Собрать эфемерный ``PlanDecision`` по действующей цели человека."""
    if not plan_engine_enabled():
        raise PlanEngineDisabled()
    if request.safety_state in SAFETY_BLOCKING:
        return _nothing(Outcome.SAFETY_BLOCKED, request)

    goal = ClientGoal.objects.filter(client=user, state=ClientGoal.State.ACTIVE).first()
    if goal is None or not goal.goal_key:
        return _nothing(Outcome.NO_GOAL, request, reason="no_active_goal" if goal is None else "goal_has_no_key")

    # Подтверждённое знание — всегда; помеченная синтетика — только по
    # серверному разрешению субъекта (флаг стенда, серверный список, тестовая
    # персона). Из тела запроса разрешение прислать нельзя.
    grant = grant_for(user)
    procedures = procedures_by_capability_helping_goal(goal.goal_key, include_synthetic=grant)
    if not procedures:
        return _nothing(Outcome.NO_CURATED_DECOMPOSITION, request, goal_key=goal.goal_key)
    # По алфавиту ключа — ради воспроизводимости; это не порядок исполнения.
    capability_refs = [c for c in sorted(procedures) if c not in request.excluded_capability_refs]
    not_justified = _not_justified_reason(capability_refs, procedures)
    if not_justified is not None:
        # «Задача не моя», а не ошибка (§6.4): потребность уходит обычной
        # рекомендации.
        return _nothing(Outcome.PLAN_NOT_JUSTIFIED, request, goal_key=goal.goal_key, reason=not_justified)

    policy_versions = {
        "plan_spec_version": PLAN_SPEC_VERSION,
        "constraint_policy_version": request.registry_version,
        "resolver_spec_version": RESOLVER_SPEC_VERSION,
        "catalog_mapping_version": CATALOG_MAPPING_UNVERSIONED,
        "safety_policy_version": request.safety_policy_version,
        "reason_code_registry_version": REASON_CODE_REGISTRY_VERSION,
    }
    steps: list[dict] = []
    assertions: list[dict] = []
    step_validations: dict[str, str] = {}
    for capability_ref in capability_refs:  # по алфавиту ключа — не порядок исполнения (§5.4)
        step_assertions = [_assertion(r, capability_ref) for r in request.rules if _applies(r, capability_ref)]
        step_id = str(uuid.uuid4())
        steps.append(
            {
                "step_id": step_id,
                "role": "OPTIONAL",
                "outcome_ref": goal.goal_key,
                "capability_ref": capability_ref,
                "level": "CAPABILITY",
                "canonical_service_ref": None,
                "tenant_offer_ref": None,
                "assertions": [a["assertion_id"] for a in step_assertions],
                "execution": None,
                "alternative_of": None,
                "provenance": {"policy_versions": policy_versions, "engine_version": PLAN_SPEC_VERSION},
            }
        )
        assertions.extend(step_assertions)
        step_validations[step_id] = _step_verdict(step_assertions)

    # Хоть один шаг на синтетической способности — план синтетический целиком:
    # механика, проверенная на таких данных, не доказывает обоснованности.
    synthetic_refs = sorted(
        set(capability_refs)
        & synthetic_capability_keys_helping_goal(goal.goal_key, include_synthetic=grant)
    )
    return {
        "outcome": Outcome.PLAN,
        "safety_state": request.safety_state,
        "details": {},
        "synthetic": bool(synthetic_refs),
        "synthetic_capability_refs": synthetic_refs,
        "decision": {
            "decision_id": str(uuid.uuid4()),
            "goal_ref": str(goal.id),
            "justification": "JUSTIFIED",
            "safety_state": request.safety_state,
            "steps": steps,
            "assertions": assertions,
            # §12: курируемой декомпозиции ОБЯЗАТЕЛЬНОСТИ нет (ни одного CORE)
            # — план INCOMPLETE, даже когда каждый шаг VALID.
            "validation": {"status": "INCOMPLETE", "step_validations": step_validations},
            "policy_versions": policy_versions,
            "computed_at": timezone.now().isoformat(),
        },
    }
