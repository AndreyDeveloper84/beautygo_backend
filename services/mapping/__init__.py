"""Level A resolver: ``SalonService → ServiceTemplate`` — только dry-run (MAP-AUTO-04).

Пакет **ничего не пишет**. Он читает строки салона, канон и синонимы, и для
каждой услуги салона выдаёт ровно одну ``ResolverRow`` с решением, причиной,
кандидатами и полным evidence. Запись (apply) — отдельный шаг MAP-AUTO-06 за
OD-NEW-7; здесь ``authorize_apply()`` всегда отказывает с названной причиной.

Границы (план §3 MAP-AUTO-04, §6, §10; HANDOFF §7):

* **Level A только**: пара «название ↔ канон». Capability, Desired Outcome,
  Goal, медицинская семантика — не вход и не выход; пакет не импортирует
  ``goals``, ``recommendation``, ``ai`` (AST-сторож);
* **без числового score**: правила детерминированы, кандидаты либо
  удовлетворяют правилу, либо нет; порядок — по ``canonical_code``;
* **fail-closed**: любая неоднозначность (>1 кандидат, маркер состава,
  маркер здоровья, кандидат не ``approved``, синоним на два канона) — не
  auto, а ``REVIEW_REQUIRED`` с одной причиной из закрытого словаря
  (план, приложение B);
* **правила за флагом**: R1 (exact pair) и R2 (approved synonym) включаются
  явно; без флага исход — ``AUTO_NOT_ENABLED`` с именем правила
  (OD-NEW-1/2 — решения владельца, здесь не принимаются); R0 (внешний
  код) объявлен, входа сегодня не имеет;
* **второй легитимный читатель** ``mapping_status`` (после
  ``users/recommendation_source.py``) — и только ради ``SKIP_DECIDED``:
  ``VERIFIED`` / ``NOT_RECOMMENDABLE`` — терминальные решения человека,
  резолвер их не пересматривает. Каталог/поиск/запись статус по-прежнему
  не читают (сторож #405 — ``services/mapping/`` в их список не входит);
* ``CANON_GAP`` — только слово владельца (принцип 08:30): ``UNRESOLVED``
  несёт подсказку ``POSSIBLE_CANON_GAP``, не решение;
* safety здесь не решается: ``safety_handoff`` переносит маркеры и
  ``canonical_rhc | unknown`` дальше, ``unknown`` остаётся ``unknown``.
"""
from services.mapping.authorize import ApplyNotAuthorized, authorize_apply
from services.mapping.resolve import resolve_tenant
from services.mapping.types import Decision, Reason, ResolverRow, RulesEnabled

__all__ = [
    "ApplyNotAuthorized",
    "Decision",
    "Reason",
    "ResolverRow",
    "RulesEnabled",
    "authorize_apply",
    "resolve_tenant",
]
