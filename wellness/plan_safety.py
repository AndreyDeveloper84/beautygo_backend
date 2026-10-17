"""Plan Engine — безопасность хода на входе каждого действия (DRF-2877, WP3 §2).

Контракт PLAN_ENGINE_CONTRACT v1.0 §6.1 («``safety_state`` на входе шага
обязателен»), §9.3 (изменение ``safety_state`` — ``BLOCKED`` fail-closed), §12.

Это безопасность РАЗГОВОРА: вердикт движка бота о словах человека в этом ходе
(``apps/orchestrator/safety`` в ai-bot-platform). Об услуге он не говорит
ничего — про услугу судит гейт здоровья каталога в создании записи, и к этому
модулю он не относится.

Каталог вердикт не вычисляет: его приносит вызывающий вместе с версией политики
и ревизией состояния, для которой он посчитан. Здесь — только форма и одно
правило: при ``STOP`` и ``UNKNOWN`` действие с планом не выполняется.

Вердикт ``CLARIFY`` действий не закрывает и ограничения сам не создаёт
(владелец 08.10: универсальный вопрос без причины не придумываем) —
ограничение открывает конкретная причина, см. ``plan_restrictions``.

``NOT_APPLICABLE`` бота не принимается: там это значит «возможность системы не
принимает решений, чувствительных к безопасности», а сборка плана и действия с
шагом — именно такие решения. Принять его значило бы молча счесть безопасность
проверенной; превратить в ``UNKNOWN`` — выдать ошибку вызывающего за штатный
отказ.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: ``PlanDecision.safety_state`` (§4.1).
SAFETY_STATES: frozenset[str] = frozenset({"NORMAL", "CLARIFY", "CAUTION", "STOP", "UNKNOWN"})
#: §12: при этих состояниях план не строится и действие с шагом не выполняется.
SAFETY_BLOCKING: frozenset[str] = frozenset({"STOP", "UNKNOWN"})


#: Длительное ограничение S1 на человеке, как его знает бот
#: (``apps/orchestrator/safety/s1_restriction`` в ai-bot-platform): его нет,
#: ждёт ответа на маршрутный вопрос, или вопрос разрешён признаком S1 («стоп»).
#: Ограничение живёт у бота и переживает разговор; каталог его не хранит и не
#: снимает — каждый раз читает то, что прислано.
S1_NONE = "none"
S1_OPEN = "open"
S1_STOP = "stop"
S1_RESTRICTIONS: frozenset[str] = frozenset({S1_NONE, S1_OPEN, S1_STOP})


class SafetyInputError(ValueError):
    """Вход безопасности не конформен. ``reason`` — машинное имя."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class SafetyInput:
    state: str
    policy_version: str
    evaluated_at_revision: int
    #: ``None`` — не спрашивалось (сборка, сохранение, ограничения); у
    #: действий, ведущих к услуге и записи, поле обязательно.
    s1_restriction: str | None = None

    @property
    def blocks(self) -> bool:
        return self.state in SAFETY_BLOCKING


def parse_safety_input(raw: Any) -> SafetyInput:
    """``{safety_state, safety_policy_version, evaluated_at_revision}`` из тела
    запроса. Отсутствие любого поля — отказ, а не «норма»: молчание о
    безопасности не читается как ``NORMAL``."""
    data = raw if isinstance(raw, dict) else {}
    state = data.get("safety_state")
    if state not in SAFETY_STATES:
        raise SafetyInputError("safety_state_invalid", str(state))
    version = data.get("safety_policy_version")
    if not isinstance(version, str) or not version.strip():
        raise SafetyInputError("safety_policy_version_missing")
    revision = data.get("evaluated_at_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise SafetyInputError("safety_revision_malformed")
    return SafetyInput(state=state, policy_version=version.strip(), evaluated_at_revision=revision)


def parse_step_safety_input(raw: Any) -> SafetyInput:
    """Тройка хода + длительное ограничение S1 — вход действий, ведущих к
    услуге и записи (кандидаты, переход шага, связь и запись от шага).

    Зарегистрированная политика ограничения: пока оно стоит, чувствительные к
    здоровью рекомендация и запись закрыты. План эту политику исполняет, а не
    вводит. Молчание об ограничении не читается как его отсутствие: поля нет —
    отказ.
    """
    safety = parse_safety_input(raw)
    data = raw if isinstance(raw, dict) else {}
    s1 = data.get("s1_restriction")
    if s1 not in S1_RESTRICTIONS:
        raise SafetyInputError("s1_restriction_invalid", str(s1))
    return SafetyInput(
        state=safety.state,
        policy_version=safety.policy_version,
        evaluated_at_revision=safety.evaluated_at_revision,
        s1_restriction=s1,
    )
