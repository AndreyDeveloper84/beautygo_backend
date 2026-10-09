"""Что можно сказать человеку о процедуре — единственная санкционированная точка чтения (DRF-2606).

Решение владельца 29.09: возможность процедуры (:class:`ProcedureCapability`)
и её связь с целью (:class:`CapabilityGoalLink`) — две таблицы, у каждой своё
основание. Этот модуль — не путь ответа (его в листе не строили), а место,
где держится правило, которое любой будущий читатель обязан унаследовать:

* **вывод системы ≠ подтверждённое человеком.** ``inference`` не отдаётся;
  та же строка после подтверждения — отдаётся;
* **«неизвестно» — не разрешение.** У процедуры без подтверждённых
  возможностей состояние ``UNKNOWN``, а не пустой список, читаемый как
  «ничего не умеет» или «можно говорить что угодно» — тот же принцип, что у
  health-gate (``_booking_guards``: ``None`` → отказ, а не пропуск);
* **``NOT_SUPPORTED`` и ``PROHIBITED_CLAIM`` не отдаются никогда.** Пригодность
  для клиента не хранится флагом — она вычисляется (подтверждено И
  ``SUPPORTED`` И не истекло), поэтому запрещённое утверждение не может
  «стать клиентским» ни правкой одного поля, ни забывчивостью. Реальный
  запрет в живом ответе — дело будущего читателя; здесь — носитель.

Чего здесь нет и почему. Поля «курс / число процедур» в перечне владельца
нет, поэтому фраза «тебе нужно 10 процедур» невозможна по построению: её
негде взять. Заводить такое поле — это содержание, которое собирает владелец.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from django.db.models import Q
from django.utils import timezone

from services.models import CapabilityGoalLink, ProcedureCapability, ServiceTemplate
from services.synthetic import SyntheticGrant, knowledge_q


class KnowledgeState(str, Enum):
    #: Есть хотя бы одна подтверждённая, поддержанная, не истёкшая возможность.
    KNOWN = "known"
    #: Подтверждённого нет — ни одной строки или только выводы/запреты/истёкшее.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CapabilityReadout:
    state: KnowledgeState
    capabilities: tuple[ProcedureCapability, ...] = field(default_factory=tuple)


def _client_facing_q(now: datetime, prefix: str = "", *, include_synthetic: SyntheticGrant | None = None) -> Q:
    """Подтверждено, поддержано, не истекло — в базе, а не только в памяти.

    ``prefix`` — путь до строки с основанием (``"capability__"`` у связи):
    связь проверяет возможность ПО БАЗЕ, а не по объекту в руках вызывающего,
    который мог устареть.

    ``include_synthetic`` — разрешение читать синтетику, выданное сервером
    (:func:`services.synthetic.grant_for`), или ``None``; само правило —
    :func:`services.synthetic.knowledge_q`. У каждой возвращённой строки
    пометка лежит в ``.synthetic``.
    """
    return knowledge_q(now, prefix, include_synthetic=include_synthetic)


def client_facing_capabilities(
    template: ServiceTemplate, *, now: datetime | None = None, include_synthetic: SyntheticGrant | None = None,
) -> CapabilityReadout:
    """Возможности процедуры, которые можно сказать человеку, и явное состояние."""
    now = now or timezone.now()
    rows = list(
        # DRF-2743: запись словаря привязана к нескольким процедурам — та же
        # запись приходит для каждой из них с тем же ``claim_id``.
        ProcedureCapability.objects.filter(
            _client_facing_q(now, include_synthetic=include_synthetic), templates=template,
        ).order_by("key")
    )
    if not rows:
        return CapabilityReadout(state=KnowledgeState.UNKNOWN)
    return CapabilityReadout(state=KnowledgeState.KNOWN, capabilities=tuple(rows))


def client_facing_goal_links(
    capability: ProcedureCapability, *, now: datetime | None = None, include_synthetic: SyntheticGrant | None = None,
) -> tuple[CapabilityGoalLink, ...]:
    """Цели, которым возможность помогает, — только если подтверждены ОБА утверждения.

    «Процедура умеет X» и «X помогает цели Y» — два решения: связь не
    говорится, пока не подтверждена сама возможность, и наоборот.
    """
    now = now or timezone.now()
    return tuple(
        CapabilityGoalLink.objects.filter(
            _client_facing_q(now, include_synthetic=include_synthetic),
            _client_facing_q(now, prefix="capability__", include_synthetic=include_synthetic),
            capability_id=capability.pk,
            goal__is_active=True,
        )
        .select_related("goal")
        .order_by("goal__sort_order", "goal__key")
    )


def template_ids_helping_goal(
    goal_key: str, *, now: datetime | None = None, include_synthetic: SyntheticGrant | None = None,
) -> frozenset:
    """Шаблоны, у которых подтверждено «процедура умеет X» И «X помогает этой цели».

    DRF-2789 (R0 умного ранжирования): сильнейший сигнал глубины совпадения
    с целью. Правило то же, что у :func:`client_facing_goal_links`: оба
    утверждения подтверждены, поддержаны и не истекли, цель активна; вывод
    системы (``inference``) не считается. Здесь, а не у читателя: этот
    модуль — единственная санкционированная точка чтения знания.

    Возможность — запись словаря, привязанная к нескольким процедурам
    (DRF-2743): в ответ попадает каждая из них.
    """
    now = now or timezone.now()
    return frozenset(
        CapabilityGoalLink.objects.filter(
            _client_facing_q(now, include_synthetic=include_synthetic),
            _client_facing_q(now, prefix="capability__", include_synthetic=include_synthetic),
            goal__key=goal_key,
            goal__is_active=True,
        ).values_list("capability__templates", flat=True)
    )


def template_ids_with_capability(
    key: str, *, now: datetime | None = None, include_synthetic: SyntheticGrant | None = None,
) -> frozenset:
    """Каноны, о которых подтверждено «процедура умеет это» (DRF-2915).

    Вход — КЛЮЧ способности: им её называет план (см.
    :func:`capability_keys_helping_goal`), он уникален и не меняется от правки
    текста. Правило то же, что у :func:`client_facing_capabilities`:
    подтверждено человеком, поддержано, не истекло; вывод системы и запрет не
    считаются. Пустой ответ — «подтверждённого нет», а не «никто не умеет».

    ``include_synthetic`` — серверное разрешение читать помеченную синтетику
    (DRF-2916) или ``None``: под ним в ответ входят и каноны синтетической
    способности. Синтетическая способность висит только на синтетическом
    каноне (замок базы), поэтому настоящих канонов она не добавляет.

    Запись словаря привязана к нескольким процедурам (DRF-2743): в ответ
    попадает каждая. Состояние самого канона (выведен из оборота) здесь не
    судится — это вопрос допуска.
    """
    return template_ids_by_capability([key], now=now, include_synthetic=include_synthetic)[key]


def template_ids_by_capability(
    keys, *, now: datetime | None = None, include_synthetic: SyntheticGrant | None = None,
) -> dict[str, frozenset]:
    """То же, что :func:`template_ids_with_capability`, пачкой — одним запросом (DRF-2966).

    На КАЖДЫЙ запрошенный ключ есть ответ: у ключа без подтверждённой
    способности — пустое множество, а не отсутствие ключа.
    """
    now = now or timezone.now()
    wanted = list(dict.fromkeys(keys))
    found: dict[str, set] = {key: set() for key in wanted}
    if wanted:
        for key, template_id in ProcedureCapability.objects.filter(
            _client_facing_q(now, include_synthetic=include_synthetic), key__in=wanted,
        ).values_list("key", "templates"):
            if template_id is not None:
                found[key].add(template_id)
    return {key: frozenset(ids) for key, ids in found.items()}


def capability_keys_helping_goal(
    goal_key: str, *, now: datetime | None = None, include_synthetic: SyntheticGrant | None = None,
) -> tuple[str, ...]:
    """Ключи возможностей, о которых подтверждено «X помогает этой цели».

    DRF-2871 (сборка плана): курируемая декомпозиция «цель → способность».
    Правило то же, что у :func:`template_ids_helping_goal`: оба утверждения
    подтверждены, поддержаны и не истекли, цель активна. Возвращаются КЛЮЧИ,
    а не строки: одна возможность встречается у нескольких процедур, и для
    плана это одна способность. По алфавиту — чтобы ответ был воспроизводим;
    это не порядок важности.

    Курс и горизонт со связи (``course_pattern``, ``result_horizon``) отсюда
    не выдаются: плану они запрещены контрактом Plan Engine (§1.1, §4.8).
    """
    now = now or timezone.now()
    return tuple(
        sorted(
            set(
                CapabilityGoalLink.objects.filter(
                    _client_facing_q(now, include_synthetic=include_synthetic),
                    _client_facing_q(now, prefix="capability__", include_synthetic=include_synthetic),
                    goal__key=goal_key,
                    goal__is_active=True,
                ).values_list("capability__key", flat=True)
            )
        )
    )


def procedures_by_capability_helping_goal(goal_key: str, *, now: datetime | None = None) -> dict[str, frozenset]:
    """Ключ возможности → процедуры (шаблоны), которые её несут, — под эту цель.

    DRF-2871: сборке плана нужно знать не только КАКИЕ способности помогают
    цели, но и можно ли получить их все ОДНОЙ процедурой: тогда потребность
    сводится к одной услуге, и плана не нужно (контракт Plan Engine §6.4).
    Правило чтения то же, что у :func:`capability_keys_helping_goal`: оба
    утверждения подтверждены, поддержаны и не истекли, цель активна.
    """
    now = now or timezone.now()
    out: dict[str, set] = {}
    for key, template_id in CapabilityGoalLink.objects.filter(
        _client_facing_q(now),
        _client_facing_q(now, prefix="capability__"),
        goal__key=goal_key,
        goal__is_active=True,
    ).values_list("capability__key", "capability__templates"):
        # Запись общего словаря привязана к нескольким процедурам (DRF-2743):
        # строка выборки — пара «ключ, процедура». Привязка своего статуса не
        # имеет — она часть содержания подтверждённой записи.
        carriers = out.setdefault(key, set())
        if template_id is not None:
            carriers.add(template_id)
    return {key: frozenset(templates) for key, templates in out.items()}


class LabelState(str, Enum):
    #: У ключа ровно одна подтверждённая формулировка для человека.
    LABELLED = "labelled"
    #: Подтверждённой, поддержанной, не истёкшей возможности с таким ключом нет.
    UNKNOWN = "unknown"
    #: Возможность подтверждена, но формулировки для человека у неё нет.
    NO_TEXT = "no_text"
    #: У ключа несколько РАЗНЫХ формулировок — какая из них «подпись
    #: способности», решает владелец, не код. **При общем словаре (DRF-2743)
    #: недостижимо: у ключа одна запись.** Значение объявлено и остаётся в
    #: проводе, производителя у него нет.
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class CapabilityLabel:
    state: LabelState
    label: str | None = None
    #: Курируемый «ожидаемый эффект» той же подтверждённой записи — ответ на
    #: «зачем этот шаг» (владелец 09.10: из обоснования, не новое поле и не
    #: текст модели). ``None`` — у записи он не заполнен: пробел называется,
    #: а не заполняется подписью. От ``label`` не зависит.
    expected_effect: str | None = None
    #: Формулировка взята у синтетической возможности (services.synthetic).
    synthetic: bool = False


def capability_labels(
    keys, *, now: datetime | None = None, include_synthetic: SyntheticGrant | None = None,
) -> dict[str, CapabilityLabel]:
    """Формулировка для человека по ключу возможности — или явная причина, почему её нет.

    DRF-2871 (экран плана): у шага плана текста нет по контракту Plan Engine
    (PE-2), он несёт только ключ; подпись берётся здесь. Правило то же, что у
    остального чтения: только подтверждённое, поддержанное, не истёкшее.

    Возможность — запись общего словаря (DRF-2743): ключ уникален, запись
    привязана к нескольким процедурам, формулировка у ключа одна. Ветка
    «у ключа несколько разных текстов → ``AMBIGUOUS``» оставлена: при общем
    словаре она недостижима (у ключа одна запись), но выбрать одну из двух
    формулировок по-прежнему значило бы решить за владельца.

    Версии у источника нет (время подтверждения — не версия), поэтому и не
    возвращается.

    Вместе с подписью — ``expected_effect`` той же записи, тем же правилом
    чтения: это то же поле, что отдаёт чтение знания о процедуре
    (``services.knowledge_api``).
    """
    now = now or timezone.now()
    wanted = list(dict.fromkeys(keys))
    texts: dict[str, set[str]] = {key: set() for key in wanted}
    seen: set[str] = set()
    marked: set[str] = set()
    effects: dict[str, str] = {}
    for key, text, effect, synthetic in ProcedureCapability.objects.filter(
        _client_facing_q(now, include_synthetic=include_synthetic), key__in=wanted,
    ).values_list("key", "text_client", "expected_effect", "synthetic"):
        seen.add(key)
        if effect.strip():
            effects[key] = effect.strip()
        if synthetic:
            marked.add(key)
        if text.strip():
            texts[key].add(text.strip())
    out: dict[str, CapabilityLabel] = {}
    for key in wanted:
        synthetic = key in marked
        effect = effects.get(key)
        if key not in seen:
            out[key] = CapabilityLabel(LabelState.UNKNOWN)
        elif not texts[key]:
            out[key] = CapabilityLabel(LabelState.NO_TEXT, expected_effect=effect, synthetic=synthetic)
        elif len(texts[key]) > 1:
            out[key] = CapabilityLabel(LabelState.AMBIGUOUS, expected_effect=effect, synthetic=synthetic)
        else:
            out[key] = CapabilityLabel(
                LabelState.LABELLED, next(iter(texts[key])), expected_effect=effect, synthetic=synthetic,
            )
    return out


__all__ = [
    "CapabilityLabel",
    "CapabilityReadout",
    "KnowledgeState",
    "LabelState",
    "capability_keys_helping_goal",
    "capability_labels",
    "client_facing_capabilities",
    "client_facing_goal_links",
    "procedures_by_capability_helping_goal",
    "template_ids_helping_goal",
]
