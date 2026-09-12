"""The personal-data access journal (owner ruling §96, CP-2 / DRF-1617).

One row per attempt by an **authenticated** caller to reach a subject's
personal data on the internal surface. The row records the FACT of access —
never a second copy of what was accessed.

### Composition, and where each field comes from

The owner named the composition; this maps it onto what a service-to-service
call can actually prove:

    кто выполнил          caller_purpose (which credential) + actor (resolved
                          subject, NULL when the header named nobody)
    роль                  actor_role, or "service" when no human resolves
    тенант                tenant — NULL on this surface by construction, see
                          the field's own comment
    тип операции          operation
    категория объекта     object_category
    идентификатор объекта object_id (the caller-supplied subject id — the
                          exact thing this slice stopped trusting)
    время                 occurred_at
    результат             result (+ denial_reason when refused)
    основание             basis — see "What is not yet real" below
    request_id            request_id

### Immutability is a mechanism here, not a convention

``save()`` refuses to update an existing row and the default manager refuses
to delete. Not "we don't do that" — there is no method that does it. A rule
without a guard is a coincidence, and an audit journal that the application
can quietly rewrite is not evidence of anything.

Migrations, ``QuerySet.update`` and raw SQL still reach the table: this is a
guard against the application, not against a database administrator. That
limit is stated so nobody cites this class as more than it is.

### Retention: one year, and the year is TEMPORARY

Owner §96, explicitly: a single retention period does not follow directly
from 152-ФЗ, so one year is a **product decision standing in until legal
review**. It is written here rather than only in the decision register
because the person who eventually implements pruning will read this file and
not that one — and a bare "365" a year from now reads as a researched number
instead of a placeholder. When legal review lands, this docstring and
whatever enforces the period change together.

Pruning has exactly one named path (DRF-1782): ``privacy_audit.prune_expired``
→ :meth:`PersonalDataAccessLogQuerySet.prune_before`, driven by the
``PRIVACY_AUDIT_RETENTION_DAYS`` setting (default 365 — the same TEMPORARY
year). The period is a parameter with a default, not a decision taken in
code: when legal review lands, the setting (or its default in
``privacy_audit/retention.py``) changes, and the deletion code does not.

### What is not yet real, named so it is not mistaken for real

``basis`` — «основание / номер обращения» — is recorded when a caller
supplies it and left explicitly EMPTY otherwise. No caller supplies it as of
10.09.2026: the bot's clients have no field for it. So the journal can today
answer "who reached what, when, and was it allowed" and CANNOT answer "on
what grounds". Refusing access for a missing basis is therefore a later
slice with its own blocker (the callers must learn to send one), not a line
in this file. The empty value is stored as an empty string and means
"nobody stated one" — it never defaults to something plausible.
"""
from __future__ import annotations

import uuid

from django.db import models


class PersonalDataAccessLogQuerySet(models.QuerySet):
    def delete(self):  # noqa: D102 — see class docstring
        raise NotImplementedError(
            "PersonalDataAccessLog is append-only: the access journal cannot "
            "be deleted through the application. Retention pruning has its "
            "own explicitly named path: privacy_audit.prune_expired."
        )

    def prune_before(self, cutoff) -> int:
        """The ONE deletion path (DRF-1782): rows with ``occurred_at`` before
        ``cutoff``. Explicitly narrows to the cutoff itself — a caller cannot
        widen it into ``delete()`` by passing a far-future date on an
        unfiltered set: the cutoff is applied here, not trusted from outside.
        Returns the number of journal rows removed."""
        narrowed = self.filter(occurred_at__lt=cutoff)
        deleted, per_model = models.QuerySet.delete(narrowed)
        return per_model.get(PersonalDataAccessLog._meta.label, 0)


class PersonalDataAccessLogManager(models.Manager.from_queryset(
    PersonalDataAccessLogQuerySet,
)):
    """Append-only manager — ``create`` yes, ``delete`` no."""


class PersonalDataAccessLog(models.Model):
    """One authenticated attempt to reach a subject's personal data."""

    class Operation(models.TextChoices):
        EXPORT = "export", "Экспорт персональных данных"
        DELETE = "delete", "Стирание персональных данных"
        READ_CONTEXT = "read_context", "Чтение личного профиля"
        WRITE_CONTEXT = "write_context", "Запись в личный профиль"
        ERASE_CONTEXT = "erase_context", "Стирание личного профиля"
        ASK_METADATA = "ask_metadata", "Служебное о вопросах профиля"
        # Маршруты заявок на удаление (DRF-1699) появились после #318; заявка
        # — начало разрушения, её чтение — статус без персональных значений.
        DELETION_REQUEST_CREATE = "deletion_request_create", "Заявка на удаление аккаунта"
        DELETION_REQUEST_READ = "deletion_request_read", "Чтение заявки на удаление"
        # DRF-1709 (B-2.2): карточка display_name+avatar_url для зеркала бота.
        READ_PROFILE = "read_profile", "Чтение карточки пользователя"

    class ObjectCategory(models.TextChoices):
        PERSONAL_DATA = "personal_data", "Персональные данные"
        PERSONAL_CONTEXT = "personal_context", "Личный профиль (декларации)"
        DELETION_REQUEST = "deletion_request", "Заявка на удаление аккаунта"

    class Result(models.TextChoices):
        ALLOWED = "allowed", "Доступ разрешён"
        DENIED = "denied", "Доступ отклонён"

    class CallerPurpose(models.TextChoices):
        INTERNAL = "internal", "Рабочий служебный токен"
        PROVISIONING = "provisioning", "Токен провизионирования"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # --- кто выполнил ---
    caller_purpose = models.CharField(
        max_length=32, choices=CallerPurpose.choices,
        help_text="Назначение предъявленного секрета — какой сервис пришёл.",
    )
    actor = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="personal_data_accesses_performed",
        help_text=(
            "Субъект, которым назвался вызывающий (X-External-User-ID, "
            "разрешённый БЕЗ создания строки). NULL = вызывающий не назвал "
            "никого либо назвал личность, которой у нас нет."
        ),
    )
    actor_role = models.CharField(
        max_length=32, blank=True,
        help_text=(
            "Роль разрешённого субъекта, либо 'service' — у вызова от сервиса "
            "человека может не быть вовсе, и это не то же самое, что пустая "
            "роль у человека."
        ),
    )
    # Клиентский субъект в этой системе глобален: внешний идентификатор
    # bot:{channel}:{id} не несёт тенанта и резолвится по глобально
    # уникальной колонке. Поле есть, потому что состав записи назван
    # владельцем, и потому что срез B-2.2 придёт на тенантные маршруты,
    # где оно заполнится. Здесь оно NULL — по устройству, а не по забывчивости.
    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="personal_data_accesses",
    )

    # --- что именно ---
    operation = models.CharField(max_length=32, choices=Operation.choices)
    object_category = models.CharField(
        max_length=32, choices=ObjectCategory.choices,
    )
    object_id = models.UUIDField(
        db_index=True,
        help_text="Субъект, названный вызывающим в URL.",
    )

    # --- чем кончилось ---
    result = models.CharField(max_length=16, choices=Result.choices)
    denial_reason = models.CharField(
        max_length=64, blank=True,
        help_text=(
            "Раздельные внутренние причины отказа при одном грубом имени "
            "наружу: subject_mismatch, wrong_purpose, unknown_actor, "
            "unnamed_actor, view_misconfigured."
        ),
    )
    actor_named = models.BooleanField(
        help_text=(
            "Назвал ли вызывающий действующий субъект. Счётчик ступени "
            "INTERNAL_SUBJECT_AUTHZ_ENFORCE живёт здесь, а не в логе: "
            "«безымянных вызовов не было» должно доказываться запросом, "
            "который считает, а не тем, что в консоли пусто."
        ),
    )

    # --- основание и трассировка ---
    basis = models.CharField(
        max_length=128, blank=True,
        help_text=(
            "Основание / номер обращения. Пусто = никто его не назвал; "
            "умолчания у этого поля нет намеренно."
        ),
    )
    request_id = models.CharField(max_length=64, blank=True)

    objects = PersonalDataAccessLogManager()

    class Meta:
        verbose_name = "Запись журнала доступа к персданным"
        verbose_name_plural = "Журнал доступа к персональным данным"
        ordering = ("-occurred_at",)
        indexes = [
            models.Index(fields=["object_id", "-occurred_at"]),
            models.Index(fields=["result", "denial_reason"]),
            models.Index(fields=["actor_named", "-occurred_at"]),
        ]
        # The journal is itself sensitive data (§96): reading it is a grant
        # of its own. Adding / changing / deleting are not offered at all —
        # there is no legitimate actor for them, so there is no permission
        # for them either.
        default_permissions = ()
        permissions = [
            ("view_personal_data_access_log",
             "Может читать журнал доступа к персональным данным"),
        ]

    def __str__(self) -> str:
        return (
            f"{self.occurred_at:%Y-%m-%d %H:%M:%S} {self.operation} "
            f"{self.result} subject={self.object_id}"
        )

    def save(self, *args, **kwargs):
        """Append-only: a row may be written once and never rewritten."""
        if self._state.adding is False:
            raise NotImplementedError(
                "PersonalDataAccessLog rows are immutable: an access journal "
                "the application can rewrite is not evidence of anything."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise NotImplementedError(
            "PersonalDataAccessLog rows cannot be deleted through the "
            "application."
        )
