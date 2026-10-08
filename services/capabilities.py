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
from services.synthetic import SyntheticGrant, knowledge_q, reads_synthetic


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


def template_ids_with_capability(key: str, *, now: datetime | None = None) -> frozenset:
    """Каноны, о которых подтверждено «процедура умеет это» (DRF-2915).

    Вход — КЛЮЧ способности: им её называет план (см.
    :func:`capability_keys_helping_goal`), он уникален и не меняется от правки
    текста. Правило то же, что у :func:`client_facing_capabilities`:
    подтверждено человеком, поддержано, не истекло; вывод системы и запрет не
    считаются. Пустой ответ — «подтверждённого нет», а не «никто не умеет».

    Отдельной функцией, потому что это точка, в которой чтение «способность
    подтверждена» расширится на помеченные синтетические данные тестового
    сценария (DRF-2916): расширение должно случиться здесь, а не у читателей.

    Запись словаря привязана к нескольким процедурам (DRF-2743): в ответ
    попадает каждая. Состояние самого канона (выведен из оборота) здесь не
    судится — это вопрос допуска.
    """
    now = now or timezone.now()
    return frozenset(
        template_id
        for template_id in ProcedureCapability.objects.filter(_client_facing_q(now), key=key).values_list(
            "templates", flat=True
        )
        if template_id is not None
    )


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


@dataclass(frozen=True)
class CapabilityVersion:
    """Версии двух утверждений, на которых стоит шаг плана (DRF-2879).

    «Процедура умеет X» и «X помогает цели» — два утверждения; у каждого свой
    идентификатор и свой отпечаток. Отпечаток — непрозрачная строка: его
    сравнивают целиком и не разбирают.
    """

    capability_id: object
    capability_fingerprint: str
    link_id: object
    link_fingerprint: str
    synthetic: bool


class UnreadableReason(str, Enum):
    """Почему утверждение сейчас не читается — причины различимы для человека."""

    #: Строки нет (удалена) или она не из тех, что спрошены.
    MISSING = "missing"
    #: Подтверждение снято или его не было; синтетика без разрешения — сюда же.
    NOT_APPROVED = "not_approved"
    #: Подтверждено, но не «поддержано»: запрет или «не поддерживается».
    NOT_SUPPORTED = "not_supported"
    #: Срок годности подтверждения истёк.
    EXPIRED = "expired"


@dataclass(frozen=True)
class ClaimVersion:
    """Текущее состояние одного утверждения — для сверки сохранённого шага.

    ``readable`` — отдаёт ли его сейчас чтение знания тем же правилом, что и
    сборка. Два исхода для шага различимы: строка читается, но ``fingerprint``
    другой — «знание изменилось» (его правили и подтвердили заново); строка
    не читается — «знание больше не действует», причина в ``unreadable_reason``.
    """

    fingerprint: str | None
    readable: bool
    unreadable_reason: UnreadableReason | None
    synthetic: bool


def claim_fingerprint(row) -> str:
    """Отпечаток содержания и основания утверждения — как его записала база.

    Читается из базы, а не с объекта: отпечаток ставит триггер, и значение в
    памяти после ``save()`` устаревает.
    """
    return type(row).objects.filter(pk=row.pk).values_list("content_fingerprint", flat=True).get()


def knowledge_versions_helping_goal(
    goal_key: str, *, now: datetime | None = None, include_synthetic: SyntheticGrant | None = None,
) -> dict[str, CapabilityVersion]:
    """По ключу способности, помогающей цели, — версии обоих её утверждений.

    Правило чтения и разрешение на синтетику — те же, что у
    :func:`capability_keys_helping_goal`; ключи ответа совпадают с её ответом
    при тех же аргументах.
    """
    now = now or timezone.now()
    rows = CapabilityGoalLink.objects.filter(
        _client_facing_q(now, include_synthetic=include_synthetic),
        _client_facing_q(now, prefix="capability__", include_synthetic=include_synthetic),
        goal__key=goal_key,
        goal__is_active=True,
    ).values_list(
        "capability__key", "capability_id", "capability__content_fingerprint", "id", "content_fingerprint",
        "synthetic",
    )
    return {
        key: CapabilityVersion(capability_id, capability_fingerprint, link_id, link_fingerprint, synthetic)
        for key, capability_id, capability_fingerprint, link_id, link_fingerprint, synthetic in rows
    }


def _claim_versions(model, ids, *, now, include_synthetic, extra_q=None) -> dict:
    ids = list(ids)
    readable = set(
        model.objects.filter(
            _client_facing_q(now, include_synthetic=include_synthetic), *(extra_q or ()), pk__in=ids,
        ).values_list("pk", flat=True)
    )
    grant_reads = reads_synthetic(include_synthetic)
    out: dict = {pk: ClaimVersion(None, False, UnreadableReason.MISSING, False) for pk in ids}
    for pk, fingerprint, status, scope, valid_until, synthetic in model.objects.filter(pk__in=ids).values_list(
        "pk", "content_fingerprint", "status", "claim_scope", "valid_until", "synthetic",
    ):
        if pk in readable:
            reason = None
        elif not (status == "approved" or (synthetic and grant_reads)):
            reason = UnreadableReason.NOT_APPROVED
        elif scope != "supported":
            reason = UnreadableReason.NOT_SUPPORTED
        elif valid_until is not None and valid_until <= now:
            reason = UnreadableReason.EXPIRED
        else:
            # Само утверждение в порядке, не читается то, от чего оно зависит
            # (у связи — её возможность или цель).
            reason = UnreadableReason.NOT_APPROVED
        out[pk] = ClaimVersion(fingerprint, reason is None, reason, synthetic)
    return out


def knowledge_versions(
    capability_ids=(), link_ids=(), *, now: datetime | None = None, include_synthetic: SyntheticGrant | None = None,
) -> dict[str, dict]:
    """Текущие версии утверждений по идентификаторам — без знания цели.

    Возвращает ``{"capabilities": {id: ClaimVersion}, "links": {id: ClaimVersion}}``.
    Связь читается, только если читается и её возможность, а цель активна —
    как у :func:`client_facing_goal_links`.
    """
    now = now or timezone.now()
    return {
        "capabilities": _claim_versions(
            ProcedureCapability, capability_ids, now=now, include_synthetic=include_synthetic,
        ),
        "links": _claim_versions(
            CapabilityGoalLink, link_ids, now=now, include_synthetic=include_synthetic,
            extra_q=(
                _client_facing_q(now, prefix="capability__", include_synthetic=include_synthetic),
                Q(goal__is_active=True),
            ),
        ),
    }


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
    """
    now = now or timezone.now()
    wanted = list(dict.fromkeys(keys))
    texts: dict[str, set[str]] = {key: set() for key in wanted}
    seen: set[str] = set()
    marked: set[str] = set()
    for key, text, synthetic in ProcedureCapability.objects.filter(
        _client_facing_q(now, include_synthetic=include_synthetic), key__in=wanted,
    ).values_list("key", "text_client", "synthetic"):
        seen.add(key)
        if synthetic:
            marked.add(key)
        if text.strip():
            texts[key].add(text.strip())
    out: dict[str, CapabilityLabel] = {}
    for key in wanted:
        synthetic = key in marked
        if key not in seen:
            out[key] = CapabilityLabel(LabelState.UNKNOWN)
        elif not texts[key]:
            out[key] = CapabilityLabel(LabelState.NO_TEXT, synthetic=synthetic)
        elif len(texts[key]) > 1:
            out[key] = CapabilityLabel(LabelState.AMBIGUOUS, synthetic=synthetic)
        else:
            out[key] = CapabilityLabel(LabelState.LABELLED, next(iter(texts[key])), synthetic=synthetic)
    return out


__all__ = [
    "CapabilityLabel",
    "CapabilityReadout",
    "CapabilityVersion",
    "ClaimVersion",
    "UnreadableReason",
    "claim_fingerprint",
    "knowledge_versions",
    "knowledge_versions_helping_goal",
    "KnowledgeState",
    "LabelState",
    "capability_keys_helping_goal",
    "capability_labels",
    "client_facing_capabilities",
    "client_facing_goal_links",
    "procedures_by_capability_helping_goal",
    "template_ids_helping_goal",
]
