"""Требования конфигурации по семействам body-care и INCOMPLETE — CAT-5 (контракт §6).

Контракт Body Care §6 даёт для каждого семейства набор «минимально значимых»
параметров и требует (§6.4): «отсутствие safety-critical параметра должно
переводить конфигурацию в INCOMPLETE». Этот модуль отвечает на один вопрос:
**хватает ли у предложения салона известных фактов (CAT-3/CAT-4), чтобы
конфигурацию вообще можно было проверять.** Он не решает, безопасна ли
услуга, и не ставит ничего в подбор — его результат читает свёртка состояния
валидации (CAT-6).

### Четыре исхода

``not_subject``  у канона нет семейства body-care (стрижка, маникюр) — эти
                 требования к услуге не относятся вовсе;
``incomplete``   не хватает требуемого известного факта — или требования
                 семейства пока не выразимы фактами (SPA, кислоты);
``conflict``     по требуемому полю источники расходятся — сигнал BLOCKED
                 для CAT-6, а не «не хватает»;
``complete``     каждое требование удовлетворено известным фактом.

### Что засчитывается

* требование удовлетворено, если хотя бы одно его поле — ``KNOWN``.
  ``KNOWN`` уже гарантирует значение, источник и вид источника
  (CheckConstraint'ы CAT-3/CAT-4) — провенанс проверять второй раз незачем;
* ``NOT_APPLICABLE`` на требуемом поле **не** удовлетворяет: иначе «не
  применимо» стало бы обходом проверки (риск C3 аудита). Если у обёртывания
  нет дополнительной модальности, это известный факт «нет», а не NA;
* нет строки — ``UNKNOWN`` (CAT-3), то есть не удовлетворено.

### Что не решено здесь — и почему не выдумано

Какие из «минимально значимых» параметров safety-critical, контракт не
говорит (решение клиники, D-4). До него обязательными считаются **все**
«минимально значимые» §6 — fail-closed: клиника потом только сужает список.
Требования SPA (компоненты, переходы) и кислот (класс, концентрация, pH…)
закрытыми 19 полями §3.1 не выражаются — это модели CAT-7 и CAT-8; до них
такие предложения всегда ``incomplete``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from services.models import OfferingConfigFact, SalonService, ServiceTemplate

NOT_SUBJECT = "not_subject"
INCOMPLETE = "incomplete"
CONFLICT = "conflict"
COMPLETE = "complete"

#: Версия набора требований. Меняется вместе с любым изменением
#: ``REQUIREMENTS`` — решение, принятое по требованиям без версии, нельзя
#: потом воспроизвести.
REQUIREMENTS_VERSION = "bc-req-0.1"

F = OfferingConfigFact.Field
Family = ServiceTemplate.ServiceFamily


@dataclass(frozen=True)
class Requirement:
    """Одно требование §6: удовлетворяет ЛЮБОЕ из полей в состоянии KNOWN."""

    name: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class FamilyRequirements:
    family: str
    requirements: tuple[Requirement, ...]
    #: False — требования семейства закрытыми 19 полями §3.1 не выражаются
    #: (SPA → CAT-7, кислоты → CAT-8); такое предложение всегда incomplete.
    expressible: bool = True


@dataclass(frozen=True)
class ConfigCheck:
    state: str
    #: Имена требований §6, которым не хватило известного факта.
    missing: tuple[str, ...] = field(default_factory=tuple)
    #: Поля, по которым источники расходятся (CONFLICT).
    conflicts: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""
    requirements_version: str = REQUIREMENTS_VERSION


_PRODUCT = Requirement("product", (F.PRODUCT_NAME, F.PRODUCT_ARTICLE, F.MANUFACTURER))
_INSTRUCTION = Requirement("instruction", (F.INSTRUCTION_VERSION, F.INSTRUCTION_REGION))
_AREA = Requirement("area", (F.APPLICATION_AREA,))

#: Требования по семействам — контракт §6, «минимально значимые», все
#: обязательны до решения клиники (см. докстринг модуля).
REQUIREMENTS: dict[str, FamilyRequirements] = {
    Family.BODY_WRAP: FamilyRequirements(
        Family.BODY_WRAP,
        (
            _PRODUCT,
            _INSTRUCTION,
            _AREA,
            Requirement("covering", (F.COVERING_TYPE,)),
            Requirement("exposure", (F.EXPOSURE_SECONDS,)),
            Requirement("removal", (F.REMOVAL_METHOD,)),
            Requirement("additional_modality", (F.ADDITIONAL_MODALITY,)),
        ),
    ),
    Family.MECHANICAL_SCRUB: FamilyRequirements(
        Family.MECHANICAL_SCRUB,
        (
            _PRODUCT,
            _AREA,
            # §6.3 mechanical_mode — в закрытом словаре §3.1 это `mode`.
            Requirement("mechanical_mode", (F.MODE,)),
            _INSTRUCTION,
            # §6.3 duration_or_protocol_reference — «или» в самом имени.
            Requirement("duration_or_protocol_reference", (F.EXPOSURE_SECONDS, F.PROTOCOL_SOURCE)),
        ),
    ),
    Family.SPA_BODY: FamilyRequirements(Family.SPA_BODY, (), expressible=False),
    Family.ACID_CARE: FamilyRequirements(Family.ACID_CARE, (), expressible=False),
}


def _check(
    family: str | None,
    facts: Mapping[str, str],
    requirements: Mapping[str, FamilyRequirements],
) -> ConfigCheck:
    if not family:
        return ConfigCheck(NOT_SUBJECT, reason="услуга вне body-care: у канона нет семейства")
    spec = requirements.get(family)
    if spec is None or not spec.expressible:
        return ConfigCheck(
            INCOMPLETE,
            reason=f"требования семейства {family} пока не выразимы фактами конфигурации",
        )
    missing: list[str] = []
    conflicts: list[str] = []
    for requirement in spec.requirements:
        states = [facts.get(f, OfferingConfigFact.State.UNKNOWN) for f in requirement.fields]
        conflicts.extend(
            f for f, s in zip(requirement.fields, states) if s == OfferingConfigFact.State.CONFLICT
        )
        if OfferingConfigFact.State.KNOWN not in states:
            missing.append(requirement.name)
    if conflicts:
        return ConfigCheck(
            CONFLICT, tuple(missing), tuple(conflicts), reason="источники расходятся"
        )
    if missing:
        return ConfigCheck(INCOMPLETE, tuple(missing), reason="не хватает известных фактов")
    return ConfigCheck(COMPLETE)


def check_configurations(
    salon_service_ids: Iterable[object],
    *,
    requirements: Mapping[str, FamilyRequirements] = REQUIREMENTS,
) -> dict[object, ConfigCheck]:
    """Проверка конфигурации для пула предложений — два запроса на весь пул.

    Ключ — ``pk`` предложения. Отсутствующий в базе ``pk`` в ответ не
    попадает: «не нашли» и «не подлежит» не должны выглядеть одинаково.
    """
    ids = list(salon_service_ids)
    families = dict(
        SalonService.objects.filter(pk__in=ids).values_list("pk", "template__service_family")
    )
    facts: dict[object, dict[str, str]] = defaultdict(dict)
    for sid, fname, state in OfferingConfigFact.objects.filter(
        salon_service_id__in=families
    ).values_list("salon_service_id", "field", "state"):
        facts[sid][fname] = state
    return {sid: _check(fam, facts.get(sid, {}), requirements) for sid, fam in families.items()}


def check_configuration(
    salon_service: SalonService,
    *,
    requirements: Mapping[str, FamilyRequirements] = REQUIREMENTS,
) -> ConfigCheck:
    """Проверка конфигурации одного предложения."""
    return check_configurations([salon_service.pk], requirements=requirements)[salon_service.pk]
