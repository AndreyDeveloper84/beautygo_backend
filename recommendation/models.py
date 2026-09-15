"""Объект Recommendation — immutable decision record (контракт v1.0 §3, Final Reconciliation v1.0 §5).

Что это и чем не является
-------------------------

Recommendation — **решение** Ayla «что человеку разумно сделать сейчас»:
Canonical NBA = **WHAT** (B2). Это не услуга, не мастер, не слот и не
запись (`Recommendation ≠ Service ≠ Offer ≠ Provider ≠ Booking`, канон v1.1
§10). Поля execution-слоя (`candidate_id`, `rank`, `service_ref`,
`provider_ref`, `price_snapshot`, `availability_ref`, `distance_meters`) в
записи **отсутствуют по построению** — они живут в ExecutionOption /
PendingBookingIntent и сюда попадают только *ссылками* на снимки (B10).
Сторож в тестах держит список запрещённых имён полей.

Кто пишет
---------

Никто напрямую. Единственный вход — ``recommendation.records.persist``:
он проверяет минимум пилота (§5), провенанс (версии политик) и пишет
RecommendationSet + записи одной транзакцией. **Формирование NBA — не
здесь**: это мозг (срез 6) и резолвер; модель хранит уже принятое
решение и не содержит ни одной ветки выбора.

Неизменяемость
--------------

Запись immutable после создания (OQ-R1 ACCEPT; канон v1.1 §10.1):

* ``save()`` существующей строки — ``ImmutableRecordError``;
* ``QuerySet.update()`` и ``delete()`` — ``ImmutableRecordError`` (менеджер
  подменён; ``update()`` в обход ``save()`` — известный обход, и он закрыт
  здесь);
* изменение семантики решения = **новая** запись с ``supersedes``;
  представление меняется через ``presentation_version``, не через id.

Срок
----

``actionable_until`` — 2 ч ConversationState / active recommendation
context (B13, канон v1.1 Decision 1): после него контекст не продолжается
напрямую, но запись **не удаляется** — хранится для attribution/audit по
*отдельной* retention policy (число лет открыто, Final Reconciliation §8
O2). Поля retention в записи нет намеренно. ``expires ≠ delete``.

События
-------

Append-only ``RecommendationEvent``: ``created / presented /
explanation_requested / alternative_requested / engaged /
booking_intent_created`` (канон v1.1 §17.3; контракт v1.0 §15).
``accepted`` и ``declined`` **не существуют** (B8): «ENGAGED доказывает
взаимодействие, booking_intent.created — переход к исполнению; generic
accepted интерпретируется сильнее доказанного».
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.db import models

#: B13: actionability = 2 ч активного контекста. Не срок жизни записи.
ACTIONABILITY_TTL = timedelta(hours=2)

#: Версия схемы записи (контракт v1.0 §3 `record_schema_version`).
#: 1.1 — DRF-1905: исход, готовность, безопасность, снимок и версии — у набора;
#: 1.2 — DRF-1906: снимок контекста — строка ContextSnapshot, FK вместо JSON-ссылки
RECORD_SCHEMA_VERSION = "1.2"


class ImmutableRecordError(RuntimeError):
    """Попытка изменить или удалить immutable decision record."""


class _ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ImmutableRecordError(
            f"{self.model.__name__}: update() запрещён — запись immutable (OQ-R1, канон v1.1 §10.1); "
            "изменение решения = новая запись с supersedes"
        )

    def delete(self):
        raise ImmutableRecordError(
            f"{self.model.__name__}: delete() запрещён — истечение actionable_until не удаляет запись; "
            "хранение — по отдельной retention policy (B13)"
        )

    # bulk_update идёт через update() базового QuerySet — перекрыт тем же отказом
    def bulk_update(self, objs, fields, batch_size=None):
        raise ImmutableRecordError(f"{self.model.__name__}: bulk_update() запрещён — запись immutable")


class _ImmutableManager(models.Manager.from_queryset(_ImmutableQuerySet)):
    pass


class _ImmutableModel(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecordError(
                f"{type(self).__name__} {self.pk}: запись immutable — изменение решения = новая запись"
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError(f"{type(self).__name__} {self.pk}: delete() запрещён (expires ≠ delete, B13)")


class ExecutionMode(models.TextChoices):
    SHADOW = "SHADOW", "SHADOW"   #: посчитано, человеку не показано (C1)
    LIVE = "LIVE", "LIVE"         #: показано / может быть показано человеку


class ResultStatus(models.TextChoices):
    """Исход прохода — контракт §32 + OD-9. С DRF-1905 — свойство НАБОРА, а не записи."""

    CLEAR_PRIMARY = "CLEAR_PRIMARY", "CLEAR_PRIMARY"
    MULTIPLE_SUITABLE = "MULTIPLE_SUITABLE", "MULTIPLE_SUITABLE"
    INSUFFICIENT_CONTEXT = "INSUFFICIENT_CONTEXT", "INSUFFICIENT_CONTEXT"
    SAFETY_BOUNDARY = "SAFETY_BOUNDARY", "SAFETY_BOUNDARY"
    NO_ACTION = "NO_ACTION", "NO_ACTION"


class ReadinessState(models.TextChoices):
    """Канон v1.1 Decision 5 — DecisionReadiness (контракт v1.0 §30). Свойство набора."""

    READY = "READY", "READY"
    NEEDS_DISCRIMINATION = "NEEDS_DISCRIMINATION", "NEEDS_DISCRIMINATION"
    NEEDS_REQUIRED_CONTEXT = "NEEDS_REQUIRED_CONTEXT", "NEEDS_REQUIRED_CONTEXT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE", "INSUFFICIENT_EVIDENCE"
    BLOCKED = "BLOCKED", "BLOCKED"


#: Исходы §32, которые «не являются NBA»: у набора нет ни primary, ни alternatives.
#: ``NO_ACTION`` сюда не входит — до размещения в таксономии (OQ-R11) он записывается как NBA.
NO_NBA_STATUSES = (ResultStatus.INSUFFICIENT_CONTEXT, ResultStatus.SAFETY_BOUNDARY)


class _ContextSnapshotQuerySet(_ImmutableQuerySet):
    def erase_for_subject(self, subject_ref: str, now) -> int:
        """Единственный переход снимка (DRF-1906, В2): стирание содержимого исполнителем D3.

        ``content`` → ``{}``, ``erased_at`` → ``now``; ``id``, ``snapshot_version`` и
        ``content_digest`` остаются — ссылка набора цела, digest доказывает, *что*
        было, не храня *что*. Уже стёртые не трогаются: повтор не сдвигает момент.
        Прочие ``update()`` отказывают, как у записей.
        """
        return models.QuerySet.update(
            self.filter(subject_ref=subject_ref, erased_at__isnull=True), content={}, erased_at=now,
        )


class _ContextSnapshotManager(models.Manager.from_queryset(_ContextSnapshotQuerySet)):
    pass


class ContextSnapshot(_ImmutableModel):
    """Снимок фактов хода, на которые опиралось решение (контракт §7; DRF-1906).

    Хранит каталог; пишет только ``records.persist`` в одной транзакции с набором.
    Содержимое — только коды, даты и значения закрытых словарей схемы версии
    (``recommendation.snapshots``); ``content_digest`` пересчитан каталогом.

    Связь с человеком — строкой ``subject_ref``, как у набора (В1-а): сторож FK
    исполнителя D3 её не видит, поэтому стирание — явный шаг D3 (часть 2).
    Неизменяем, кроме одного перехода — ``objects.erase_for_subject`` (В2).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subject_ref = models.CharField(max_length=64)
    snapshot_version = models.CharField(max_length=32)
    #: sha256 канонического JSON содержимого — hex, 64 знака.
    content_digest = models.CharField(max_length=64)
    content = models.JSONField(default=dict)
    created_at = models.DateTimeField()
    #: Момент стирания исполнителем D3; NULL — содержимое на месте.
    erased_at = models.DateTimeField(null=True, blank=True)

    objects = _ContextSnapshotManager()

    class Meta:
        indexes = [models.Index(fields=["subject_ref", "created_at"], name="ctxsnap_subject_created_idx")]
        constraints = [
            # Стёртый снимок пуст по построению — не «стёрт, но с содержимым».
            models.CheckConstraint(
                condition=models.Q(erased_at__isnull=True) | models.Q(content={}),
                name="ctxsnap_erased_has_empty_content",
            ),
        ]

    def as_ref(self) -> dict:
        """Ссылка §7 — строится из снимка, вручную её не пишет никто."""
        return {
            "snapshot_id": str(self.pk),
            "snapshot_version": self.snapshot_version,
            "content_digest": self.content_digest,
        }

    def __str__(self) -> str:
        return f"ContextSnapshot {self.id} ({self.snapshot_version})"


class RecommendationSet(_ImmutableModel):
    """Одна выдача: исход прохода + (при NBA) primary и ≤2 alternatives (OQ-R1; контракт §3, §32).

    DRF-1905: исход, готовность, вердикт безопасности, снимок контекста и версии
    политик — одно на проход и живут здесь. ``reason_codes`` / ``evidence_refs`` /
    ``explanation`` набора объясняют ИСХОД (в том числе «почему NBA нет»); у каждого
    варианта остаются свои (WHY альтернативы, C04.2).

    ``primary`` — колонка, а не выборка по роли: только так CHECK на строке набора
    может связать исход и наличие NBA. Набор immutable и не обновляется после
    создания primary, поэтому ``persist`` заранее выбирает id primary и пишет набор
    с ним; FK отложен до COMMIT (``DEFERRABLE INITIALLY DEFERRED`` — сторож в тестах).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: Псевдонимизированный субъект (контракт §3): ключ пользователя Ayla, не ФИО/телефон.
    subject_ref = models.CharField(max_length=64)
    #: Связь с Intent Resolution Output 0.5 (канон). Строка: контракт intent живёт в боте.
    intent_id = models.CharField(max_length=64)
    #: SemanticResolutionResult — A1 (WD §11.2); может отсутствовать (INSUFFICIENT/DISCOVERY).
    semantic_resolution_ref = models.CharField(max_length=128, blank=True, default="")
    #: C1 владельца: DecisionReadiness на пилоте в теневом режиме — записи, которых
    #: человек не видел, обязаны быть отличимы от живых (замер порогов, attribution).
    #: Умолчание SHADOW — fail-closed: живой только тот набор, что назван живым.
    execution_mode = models.CharField(max_length=8, choices=ExecutionMode.choices, default=ExecutionMode.SHADOW)
    #: Ход диалога, не содержимое: {conversation_id, trace_id} — связь с dialog_transcript
    #: (DRF-1754) и ConversationState (2 ч, B13). Снимок контекста этого не заменяет.
    conversation_ref = models.JSONField(default=dict, blank=True)

    # --- исход прохода (DRF-1905, §32) ---------------------------------------
    result_status = models.CharField(max_length=24, choices=ResultStatus.choices)
    readiness_state = models.CharField(max_length=24, choices=ReadinessState.choices)
    reason_codes = models.JSONField(default=list)             #: почему такой исход — из Decision Policy
    evidence_refs = models.JSONField(default=list)            #: {source, ref, said_at?} — основания исхода
    #: {displayable: bool, user_visible_reasons: [], internal_only: []} — owner ruling 2026-07-29
    explanation = models.JSONField(default=dict)
    #: {state, rule_id, policy_version, evidence_ref, activated_at} — owner 11.09 §3; B6
    safety_evaluation_ref = models.JSONField(default=dict)
    #: Decision Snapshot хода (контракт §7; DRF-1906). Одна правда: ссылка
    #: {snapshot_id, snapshot_version, content_digest} строится из строки снимка.
    context_snapshot = models.ForeignKey(
        ContextSnapshot, on_delete=models.PROTECT, related_name="recommendation_sets",
    )
    #: NBA набора; NULL ⇔ исход из NO_NBA_STATUSES (CHECK ниже).
    primary = models.ForeignKey(
        "Recommendation", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )

    # --- версии политик (провенанс прохода) ----------------------------------
    decision_policy_version = models.CharField(max_length=32)
    taxonomy_version = models.CharField(max_length=32)
    safety_policy_version = models.CharField(max_length=32)
    catalog_mapping_version = models.CharField(max_length=32)
    presentation_policy_version = models.CharField(max_length=32)
    record_schema_version = models.CharField(max_length=8, default=RECORD_SCHEMA_VERSION)

    created_at = models.DateTimeField()

    objects = _ImmutableManager()

    class Meta:
        indexes = [models.Index(fields=["subject_ref", "created_at"], name="recset_subject_created_idx")]
        constraints = [
            # §32: SAFETY_BOUNDARY / INSUFFICIENT_CONTEXT «не являются NBA» — primary нет;
            # любой другой исход без primary — выдуманная пустота. CHECK проверяется при вставке.
            models.CheckConstraint(
                condition=(
                    (models.Q(result_status__in=[s.value for s in NO_NBA_STATUSES]) & models.Q(primary__isnull=True))
                    | (~models.Q(result_status__in=[s.value for s in NO_NBA_STATUSES])
                       & models.Q(primary__isnull=False))
                ),
                name="recset_primary_iff_nba_status",
            ),
        ]

    def __str__(self) -> str:
        return f"RecommendationSet {self.id} ({self.result_status})"


class Recommendation(_ImmutableModel):
    class Role(models.TextChoices):
        PRIMARY = "primary", "primary"
        ALTERNATIVE = "alternative", "alternative"

    class RerankReason(models.TextChoices):
        ALTERNATIVE_REQUESTED = "ALTERNATIVE_REQUESTED", "ALTERNATIVE_REQUESTED"
        REJECTED = "REJECTED", "REJECTED"
        CONSTRAINT_ADDED = "CONSTRAINT_ADDED", "CONSTRAINT_ADDED"

    class Family(models.TextChoices):
        """B9: provisional для Controlled Pilot — «не вечная таксономия без validation»."""

        ADDRESS = "ADDRESS", "ADDRESS"
        SUPPORT = "SUPPORT", "SUPPORT"
        RECOVER = "RECOVER", "RECOVER"
        OBSERVE = "OBSERVE", "OBSERVE"

    #: DRF-1905: исход и готовность — свойства набора; имена оставлены для прежних вызывающих.
    ResultStatus = ResultStatus
    ReadinessState = ReadinessState

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recommendation_set = models.ForeignKey(RecommendationSet, on_delete=models.PROTECT, related_name="recommendations")
    role = models.CharField(max_length=12, choices=Role.choices)
    #: Lineage R1 → R2 (канон v1.1 §10.2): у alternative — primary, от которой выполнен rerank.
    parent = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="children")
    rerank_reason = models.CharField(max_length=24, choices=RerankReason.choices, blank=True, default="")
    #: Изменение семантики решения — новая запись; старая остаётся (§21).
    supersedes = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="superseded_by",
    )

    # --- decision_subject = Canonical NBA = WHAT (B2) ---------------------
    direction_code = models.CharField(max_length=64)          #: рабочий код направления (taxonomy_version)
    target_outcomes = models.JSONField(default=list)          #: DesiredOutcome refs (A1)
    family = models.CharField(max_length=12, choices=Family.choices)

    # Исход прохода, готовность, вердикт безопасности, снимок контекста хода и
    # версии политик — у НАБОРА (DRF-1905): одно на проход. Здесь — WHY варианта.
    reason_codes = models.JSONField(default=list)             #: из Decision Policy, не из LLM
    #: user_stated | confirmed_memory | policy | safety | journey (контракт §12)
    evidence_refs = models.JSONField(default=list)
    #: {displayable: bool, user_visible_reasons: [], internal_only: []} — owner ruling 2026-07-29, owner 24.08
    explanation = models.JSONField(default=dict)
    consent_evaluation_ref = models.JSONField(default=dict)

    # --- снимки варианта (B10) — ССЫЛКИ, не копии -------------------------
    memory_snapshot_ref = models.JSONField(null=True, blank=True)     #: Phase 1 — null
    #: Execution Mapping Snapshot — в ExecutionOption; здесь только ref, если уже есть
    execution_mapping_snapshot_ref = models.JSONField(null=True, blank=True)
    #: Transaction Snapshot — в PendingBookingIntent / Booking; здесь только ref
    transaction_snapshot_ref = models.JSONField(null=True, blank=True)

    presentation_version = models.PositiveIntegerField(default=1)
    record_schema_version = models.CharField(max_length=8, default=RECORD_SCHEMA_VERSION)

    created_at = models.DateTimeField()
    #: B13: = created_at + 2 ч. Не срок жизни записи.
    actionable_until = models.DateTimeField()

    objects = _ImmutableManager()

    class Meta:
        indexes = [
            models.Index(fields=["recommendation_set", "role"], name="rec_set_role_idx"),
            models.Index(fields=["actionable_until"], name="rec_actionable_idx"),
        ]
        constraints = [
            # Одна primary на выдачу (OQ-R1: RecommendationSet = одна выдача).
            models.UniqueConstraint(
                fields=["recommendation_set"], condition=models.Q(role="primary"),
                name="recommendation_one_primary_per_set",
            ),
            # У alternative есть родитель и причина rerank; у primary — нет (канон v1.1 §10.2).
            models.CheckConstraint(
                condition=(
                    models.Q(role="primary", parent__isnull=True, rerank_reason="")
                    | (models.Q(role="alternative", parent__isnull=False) & ~models.Q(rerank_reason=""))
                ),
                name="recommendation_alternative_has_parent_and_reason",
            ),
            # actionable_until строго после created_at (B13).
            models.CheckConstraint(
                condition=models.Q(actionable_until__gt=models.F("created_at")),
                name="recommendation_actionable_after_created",
            ),
        ]

    def is_actionable(self, now) -> bool:
        """Контекст ещё продолжается напрямую. Ложь ≠ «записи нет» (B13)."""
        return now < self.actionable_until

    def __str__(self) -> str:
        return f"Recommendation {self.id} ({self.role}: {self.family}/{self.direction_code})"


class RecommendationEvent(_ImmutableModel):
    """Append-only события записи. `accepted`/`declined` не существуют (B8)."""

    class Kind(models.TextChoices):
        CREATED = "recommendation.created", "recommendation.created"
        PRESENTED = "recommendation.presented", "recommendation.presented"
        EXPLANATION_REQUESTED = "recommendation.explanation_requested", "recommendation.explanation_requested"
        ALTERNATIVE_REQUESTED = "recommendation.alternative_requested", "recommendation.alternative_requested"
        ENGAGED = "recommendation.engaged", "recommendation.engaged"
        BOOKING_INTENT_CREATED = "booking_intent.created", "booking_intent.created"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recommendation = models.ForeignKey(Recommendation, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=48, choices=Kind.choices)
    occurred_at = models.DateTimeField()
    #: Кто произвёл факт (Channel Delivery / Interaction, Booking / Handoff, Recommendation) — строкой
    producer = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)   #: только идентификаторы, без текста и ПДн (§15)

    objects = _ImmutableManager()

    class Meta:
        indexes = [models.Index(fields=["recommendation", "kind"], name="recevent_rec_kind_idx")]

    def __str__(self) -> str:
        return f"{self.kind} @ {self.recommendation_id}"
