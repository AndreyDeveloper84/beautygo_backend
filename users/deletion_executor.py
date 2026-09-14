"""Исполнитель удаления аккаунта — срез D3 §7 свода владельца (DRF-1699 / DRF-1725).

Заявку заводит D1 (``users/deletion_requests.py``), персонализацию по живой
заявке останавливает D2. Здесь — само стирание: одна заявка →
``REQUESTED → PROCESSING → COMPLETED``, и ``COMPLETED`` ставится только
после того, как бот подтвердил свою половину.

### Решение по каждой связи, а не по каскаду модели

На ``User`` каталога смотрят 36 обратных связей плюс скрытые
(``related_name="+"``) и служебные (``groups``/``user_permissions``) —
замер ``Ayla/docs/MEASUREMENT_DELETION_EXECUTOR_D3.md``. Ни одна из них не
решается ``on_delete``: строка ``User`` физически **не удаляется никогда**
(``deleted_at`` + обезличивание), поэтому CASCADE/SET_NULL модели здесь не
срабатывают вовсе, а PROTECT не мешает. Каждая связь названа ровно в одной
из трёх таблиц ниже — **удалить / обезличить / хранить по основанию** — и
:func:`undecided_pointers` перед каждым прогоном сверяет таблицы с живой
переписью ORM'ом: новая модель с FK на ``User`` без решения не даёт
исполнителю стартовать (``FAILED`` с именем связи), а не удаляется «как
получится».

Ответы владельца (``OWNER_DECISIONS_2026-09-12_PACKAGE2.md`` D6–D9):

* D6 — дневник питания **удалять** (ПДн; после удаления аккаунта без
  отдельного основания не хранить);
* D7 — состоявшиеся записи **обезличить**: ``client`` → tombstone,
  хранить 5 лет (transactional/legal record без идентичности);
* D8 — подписка мастера: **хранить** до конца периода + 5 лет, способ
  оплаты стереть сразу;
* D9 — отзыв **обезличить**, текст оставить, плюс scrub имени/телефона.

### Порядок

Ориентиры/нормы → входы → дневники → цели/планы → медиа → профиль → аккаунт;
транзакционное — обезличиванием, не удалением. Всё — в одной транзакции
каталога; неполнота (перечитанная строка не пуста) → откат целиком,
``FAILED`` + ``failure_reason``, заявка открыта, повтор по ней же.

### Почему бот — после транзакции, а не внутри

Половина бота (``privacy.delete_personal_data`` + ``clear_deletion_flag``)
живёт за HTTP; держать транзакцию каталога открытой на время чужого
запроса нельзя. Поэтому: транзакция каталога → подтверждение бота →
``COMPLETED``. Бот не ответил 2xx с ``all_ok`` — заявка остаётся
``PROCESSING`` с названной причиной, ``deletion_gate`` закрыт, следующий
тик повторяет только бот-половину (все шаги каталога идемпотентны).

Прокси-строки бота (``bot:max:<id>``, ``linked_user`` → человек)
отвязываются **последними, вместе с COMPLETED**: шаг 1 каскада бота ходит
в ``…/personal-data/`` каталога, а сторож ``IsInternalBearerForSubject``
узнаёт субъекта именно по ``linked_user``. Отвязать раньше — значит
самому отказать боту в подтверждении.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta

import httpx
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from users.models import DeletionRequest

logger = logging.getLogger(__name__)

User = get_user_model()

#: Служебный пользователь, на которого переводятся ``client`` у записей и
#: отзывов (D7/D9). Один на базу; не NULL — PROTECT и отчёты салона держатся
#: на непустом клиенте.
TOMBSTONE_USERNAME = "deleted-client"

#: Значение, которым обезличиваются имена.
ERASED_NAME = "Удалён"

#: Что пишется в тексте отзыва вместо найденных ПДн (D9 scrub).
SCRUBBED = "[скрыто]"

#: Сроки хранения — в основаниях ``RETAIN``; числа из ответов владельца.
RETENTION_APPOINTMENTS = "5 лет (D7)"
RETENTION_SUBSCRIPTION = "до конца оплаченного периода + 5 лет (D8)"

# ---------------------------------------------------------------------------
# Таблицы решений — ключ ``app.Model.field`` для каждого указателя на User
# ---------------------------------------------------------------------------

#: Удалить строки (и файлы). Значение — как.
DELETE: dict[str, str] = {
    # дневники и питание — D6
    "nutrition.NutritionProfile.user": "erase_personal_calculation_inputs (#402), затем строка",
    "nutrition.FoodLog.user": "строки",
    "nutrition.FoodScan.user": "строки + файлы image через storage",
    "nutrition.WaterEntry.user": "строки",
    "nutrition.WaterLog.user": "строки",
    "nutrition.CrossDomainShownRule.user": "строки (производное от дневника)",
    "nutrition.ProfileIdempotencyKey.user": "строки (кэш тел ответов профиля)",
    # цели, планы, наблюдения — §7 «цели и планы будут удалены»
    "goals.ClientGoal.client": "строки",
    "goals.GoalAnketaRun.client": "строки (+ответы каскадом)",
    "wellness.DesiredOutcome.user": "строки (после PlanOutcomeLink)",
    "wellness.PersonalPlan.user": "строки (после PlanOutcomeLink)",
    "wellness.ProgressObservation.user": "строки (superseded_by внутри набора)",
    # диалоги, уведомления, кэши
    "ai.Conversation.user": "строки, включая мягко удалённые (all_objects)",
    "notifications.Notification.user": "строки",
    "appointments.IdempotencyKey.user": "строки (кэш тел запросов)",
    # личность
    "users.UserPersonalContext.user": "erase_personal_context(initiator=deletion_executor)",
    "users.SocialAccount.user": "строки",
    "users.DeviceToken.user": "строки",
    "users.AnonymousSession.user": "строки",
    "users.FavoriteSpecialist.user": "строки (предпочтение, не сделка)",
    "users.User_groups.user": "groups.clear()",
    "users.User_user_permissions.user": "user_permissions.clear()",
    "token_blacklist.OutstandingToken.user": "в blacklist, затем строки",
    "payments.UserPaymentMethod.user": "revoke(), затем строки (токен способа оплаты)",
}

#: Обезличить: снять указатель на человека / стереть личные поля, строку
#: оставить. Значение — как.
ANONYMISE: dict[str, str] = {
    "users.Profile.user": "full_name/bio/city/avatar/координаты",
    "users.SpecialistProfile.user": "display_name/bio/address/avatar/координаты; портфолио — файлы и строки",
    "users.TenantUserRelationship.user": "is_active=False, revoked_at, revoke_reason=account_deleted",
    "users.User.linked_user": "у прокси linked_user=NULL — последним, вместе с COMPLETED",
    "appointments.Appointment.client": "client → tombstone, notes='' (D7)",
    "appointments.Appointment.cancelled_by": "NULL (актор истории)",
    "appointments.AppointmentRevision.actor": "NULL (актор истории)",
    "appointments.SpecialistTimeOff.created_by": "NULL (актор истории)",
    "reviews.Review.client": "client → tombstone, is_anonymous=True, scrub текста (D9)",
    "billing.SpecialistSubscription.user": "payment_method_id/card_brand стереть сразу (D8)",
    "billing.BillingConsent.user": "revoked_at=now",
    "analytics.AnalyticsEvent.actor": "NULL (события без значений — счётчики, AMD-010)",
}

#: Хранить без изменений. Значение — основание и срок.
RETAIN: dict[str, str] = {
    "users.DeletionRequest.user": "юридический след заявки; строка User не удаляется физически",
    "admin.LogEntry.user": "журнал администрирования; у клиента пусто",
    "services.ServiceTemplate.approved_by": "провенанс решения по каталогу (§76), актор — сотрудник",
    "services.ServiceTemplateSynonym.confirmed_by": "провенанс решения по каталогу (§93), актор — сотрудник",
    "services.SalonService.mapping_confirmed_by": "провенанс решения по каталогу (§93), актор — сотрудник",
    "services.DraftSalonService.confirmed_by": "провенанс решения по каталогу (§93), актор — сотрудник",
    "tenants.ServiceLocation.confirmed_by": "провенанс подтверждения адреса, актор — сотрудник",
    "privacy_audit.PersonalDataAccessLog.actor": (
        "журнал доступа к ПДн (§96, D1, #407) — append-only доказательство того, кто и к чему "
        "обращался; строка User остаётся обезличенной, указатель не снимается"
    ),
}


def pointers_to_user() -> set[str]:
    """Живая перепись: каждый конкретный указатель на ``User`` во всех
    моделях, включая скрытые (``related_name="+"``) и служебные through-таблицы
    M2M. Прокси-модели (``PendingExternalIdentity``) не считаются — у них тот
    же столбец, что у ``User``.
    """
    found: set[str] = set()
    for model in apps.get_models(include_auto_created=True):
        if model._meta.proxy:
            continue
        for f in model._meta.get_fields(include_hidden=True):
            if not (f.concrete and f.is_relation) or f.many_to_many:
                continue
            if f.related_model is User:
                found.add(f"{model._meta.label}.{f.name}")
    return found


def undecided_pointers() -> dict[str, list[str]]:
    """Расхождения таблиц с переписью: ``{"missing": [...], "duplicate": [...],
    "stale": [...]}``. Пусто — можно исполнять."""
    live = pointers_to_user()
    tables = (DELETE, ANONYMISE, RETAIN)
    named = [k for t in tables for k in t]
    missing = sorted(live - set(named))
    duplicate = sorted({k for k in named if named.count(k) > 1})
    stale = sorted(set(named) - live)
    out: dict[str, list[str]] = {}
    if missing:
        out["missing"] = missing
    if duplicate:
        out["duplicate"] = duplicate
    if stale:
        out["stale"] = stale
    return out


# ---------------------------------------------------------------------------
# Исходы
# ---------------------------------------------------------------------------


class UndecidedRelation(RuntimeError):
    """Есть указатель на User без решения — исполнять нельзя."""


class IncompleteErasure(RuntimeError):
    """После записи в базе не то, что объявлено, — откат целиком."""


@dataclass
class ExecutionOutcome:
    request_id: str
    status: str
    steps: dict = field(default_factory=dict)
    failure_reason: str = ""

    @property
    def completed(self) -> bool:
        return self.status == DeletionRequest.Status.COMPLETED


# ---------------------------------------------------------------------------
# Вход
# ---------------------------------------------------------------------------


def execute(request: DeletionRequest, *, bot_client=None) -> ExecutionOutcome:
    """Исполнить одну заявку. Идемпотентно: повтор по ``PROCESSING``/``FAILED``
    проходит те же шаги (каждый — по фильтру на человека, пустой набор —
    не ошибка) и снова спрашивает бота.
    """
    if request.status == DeletionRequest.Status.COMPLETED:
        return ExecutionOutcome(str(request.pk), request.status, request.steps)

    gaps = undecided_pointers()
    if gaps:
        reason = f"undecided relations: {json.dumps(gaps, ensure_ascii=False)}"
        _mark_failed(request, reason)
        logger.error("deletion_executor.undecided request=%s %s", request.pk, reason)
        return ExecutionOutcome(str(request.pk), request.status, request.steps, reason)

    _mark_processing(request)
    user = request.user

    try:
        with transaction.atomic():
            steps = _erase_catalog(user)
    except Exception as exc:  # noqa: BLE001 — любая неполнота = FAILED с причиной
        reason = f"{exc.__class__.__name__}: {exc}"[:500]
        _mark_failed(request, reason)
        logger.exception("deletion_executor.catalog_failed request=%s", request.pk)
        return ExecutionOutcome(str(request.pk), request.status, request.steps, reason)

    bot = _confirm_with_bot(request, user, client=bot_client)
    if not bot.ok:
        # Каталог стёрт, бот не подтвердил: заявка остаётся живой,
        # deletion_gate закрыт, следующий тик повторит.
        request.steps = {**steps, "bot": bot.steps}
        request.failure_reason = bot.reason[:500]
        request.save(update_fields=["steps", "failure_reason"])
        logger.warning(
            "deletion_executor.bot_unconfirmed request=%s reason=%s", request.pk, bot.reason
        )
        return ExecutionOutcome(str(request.pk), request.status, request.steps, bot.reason)

    with transaction.atomic():
        _unlink_proxies(user)
        request.steps = {**steps, "bot": bot.steps}
        request.status = DeletionRequest.Status.COMPLETED
        request.completed_at = timezone.now()
        request.failure_reason = ""
        request.save(update_fields=["steps", "status", "completed_at", "failure_reason"])
    logger.info("deletion_executor.completed request=%s user=%s", request.pk, user.pk)
    return ExecutionOutcome(str(request.pk), request.status, request.steps)


def _mark_processing(request: DeletionRequest) -> None:
    request.status = DeletionRequest.Status.PROCESSING
    if request.started_at is None:
        request.started_at = timezone.now()
    request.save(update_fields=["status", "started_at"])


def _mark_failed(request: DeletionRequest, reason: str) -> None:
    request.status = DeletionRequest.Status.FAILED
    request.failure_reason = reason[:500]
    request.save(update_fields=["status", "failure_reason"])


# ---------------------------------------------------------------------------
# Каталог — одна транзакция
# ---------------------------------------------------------------------------


def tombstone_user():
    """Служебный ``deleted-client`` — один на базу."""
    user, _ = User.objects.get_or_create(
        username=TOMBSTONE_USERNAME,
        defaults={
            "role": "client",
            "first_name": ERASED_NAME,
            "is_active": False,
            "is_verified": False,
        },
    )
    return user


def _erase_catalog(user) -> dict:
    """Все шаги каталога по таблицам выше. Возвращает ``steps`` для заявки."""
    from appointments.models import (
        Appointment,
        AppointmentRevision,
        IdempotencyKey,
        SpecialistTimeOff,
    )
    from billing.models import BillingConsent, SpecialistSubscription
    from goals.models import ClientGoal, GoalAnketaRun
    from notifications.models import Notification
    from nutrition.models import (
        CrossDomainShownRule,
        FoodLog,
        FoodScan,
        NutritionProfile,
        ProfileIdempotencyKey,
        WaterEntry,
        WaterLog,
    )
    from nutrition.services.personal_calculation_withdrawal import (
        erase_personal_calculation_inputs,
    )
    from payments.models import UserPaymentMethod
    from reviews.models import Review
    from users.models import (
        AnonymousSession,
        DeviceToken,
        FavoriteSpecialist,
        Profile,
        SocialAccount,
        SpecialistPortfolio,
        SpecialistProfile,
        TenantUserRelationship,
    )
    from users.personal_context_erasure import erase_personal_context
    from wellness.models import (
        DesiredOutcome,
        PersonalPlan,
        PlanOutcomeLink,
        ProgressObservation,
    )
    from ai.models import Conversation
    from analytics.models import AnalyticsEvent

    now = timezone.now()
    deleted: dict[str, int] = {}
    anonymised: dict[str, int] = {}
    files_deleted = 0

    def _delete(key: str, qs) -> None:
        n, _ = qs.delete()
        deleted[key] = deleted.get(key, 0) + n

    # 1. Ориентиры/нормы → входы (тот же писатель, что у отзыва согласия
    # §2, #402), затем сам профиль: после удаления аккаунта нужен не пустой
    # профиль, а его отсутствие.
    erase_personal_calculation_inputs(user)
    _delete("nutrition.NutritionProfile", NutritionProfile.objects.filter(user=user))

    # 2. Дневники (D6) — файлы сканов раньше строк: строка без файла хуже
    # файла без строки (повтор найдёт файл по имени только через строку).
    scans = list(FoodScan.objects.filter(user=user).only("id", "image"))
    for scan in scans:
        files_deleted += _delete_file(scan.image)
    _delete("nutrition.FoodScan", FoodScan.objects.filter(user=user))
    _delete("nutrition.FoodLog", FoodLog.objects.filter(user=user))
    _delete("nutrition.WaterEntry", WaterEntry.objects.filter(user=user))
    _delete("nutrition.WaterLog", WaterLog.objects.filter(user=user))
    _delete("nutrition.CrossDomainShownRule", CrossDomainShownRule.objects.filter(user=user))
    _delete("nutrition.ProfileIdempotencyKey", ProfileIdempotencyKey.objects.filter(user=user))

    # 3. Цели и планы. PROTECT между собственными строками человека:
    # связи план↔цель снимаются первыми, наблюдения удаляются набором.
    _delete("wellness.PlanOutcomeLink", PlanOutcomeLink.objects.filter(plan__user=user))
    _delete("wellness.PlanOutcomeLink", PlanOutcomeLink.objects.filter(outcome__user=user))
    _delete("wellness.PersonalPlan", PersonalPlan.objects.filter(user=user))
    _delete("wellness.DesiredOutcome", DesiredOutcome.objects.filter(user=user))
    # PROTECT на superseded_by внутри набора: снять указатели, потом строки.
    ProgressObservation.objects.filter(user=user).update(superseded_by=None)
    _delete("wellness.ProgressObservation", ProgressObservation.objects.filter(user=user))
    _delete("goals.GoalAnketaRun", GoalAnketaRun.objects.filter(client=user))
    _delete("goals.ClientGoal", ClientGoal.objects.filter(client=user))

    # 4. Диалоги, уведомления, кэши.
    _delete("ai.Conversation", Conversation.all_objects.filter(user=user))
    _delete("notifications.Notification", Notification.objects.filter(user=user))
    _delete("appointments.IdempotencyKey", IdempotencyKey.objects.filter(user=user))

    # 5. Сделки и деньги — обезличиванием (D7/D8/D9).
    tomb = tombstone_user()
    names = _person_names(user)
    anonymised["appointments.Appointment.client"] = Appointment.objects.filter(
        client=user
    ).update(client=tomb, notes="")
    anonymised["appointments.Appointment.cancelled_by"] = Appointment.objects.filter(
        cancelled_by=user
    ).update(cancelled_by=None)
    anonymised["appointments.AppointmentRevision.actor"] = AppointmentRevision.objects.filter(
        actor=user
    ).update(actor=None)
    anonymised["appointments.SpecialistTimeOff.created_by"] = SpecialistTimeOff.objects.filter(
        created_by=user
    ).update(created_by=None)
    n_reviews = 0
    for review in Review.objects.filter(client=user):
        review.client = tomb
        review.is_anonymous = True
        review.text = scrub_personal_data(review.text, names)
        review.save(update_fields=["client", "is_anonymous", "text", "updated_at"])
        n_reviews += 1
    anonymised["reviews.Review.client"] = n_reviews
    for method in UserPaymentMethod.objects.filter(user=user):
        method.revoke()
    _delete("payments.UserPaymentMethod", UserPaymentMethod.objects.filter(user=user))
    anonymised["billing.SpecialistSubscription.user"] = SpecialistSubscription.objects.filter(
        user=user
    ).update(payment_method_id="", card_brand="", payment_method_saved_at=None, next_retry_at=None)
    anonymised["billing.BillingConsent.user"] = BillingConsent.objects.filter(
        user=user, revoked_at__isnull=True
    ).update(revoked_at=now)
    anonymised["users.TenantUserRelationship.user"] = TenantUserRelationship.objects.filter(
        user=user, is_active=True
    ).update(is_active=False, revoked_at=now, revoke_reason="account_deleted")

    # 6. Медиа и профиль.
    profile = Profile.objects.filter(user=user).first()
    if profile is not None:
        files_deleted += _delete_file(profile.avatar)
        profile.avatar = None
        profile.full_name = ERASED_NAME
        profile.bio = ""
        profile.city = ""
        profile.default_location_lat = None
        profile.default_location_lng = None
        profile.save(
            update_fields=[
                "avatar", "full_name", "bio", "city",
                "default_location_lat", "default_location_lng",
            ]
        )
        anonymised["users.Profile.user"] = 1
    sp = SpecialistProfile.objects.filter(user=user).first()
    if sp is not None:
        for item in SpecialistPortfolio.objects.filter(specialist=sp).only("id", "image"):
            files_deleted += _delete_file(item.image)
        _delete("users.SpecialistPortfolio", SpecialistPortfolio.objects.filter(specialist=sp))
        files_deleted += _delete_file(sp.avatar)
        sp.avatar = None
        sp.display_name = ERASED_NAME
        sp.bio = ""
        sp.address = ""
        sp.location_lat = None
        sp.location_lng = None
        sp.is_available = False
        sp.is_booking_enabled = False
        sp.save(
            update_fields=[
                "avatar", "display_name", "bio", "address", "location_lat",
                "location_lng", "is_available", "is_booking_enabled", "updated_at",
            ]
        )
        anonymised["users.SpecialistProfile.user"] = 1

    # 7. Аккаунт. Контекст — ДО обезличивания событий аналитики: erase
    # пишет своё аудит-событие с actor=user, и оно тоже обязано потерять актора.
    _delete("users.SocialAccount", SocialAccount.objects.filter(user=user))
    _delete("users.DeviceToken", DeviceToken.objects.filter(user=user))
    _delete("users.AnonymousSession", AnonymousSession.objects.filter(user=user))
    _delete("users.FavoriteSpecialist", FavoriteSpecialist.objects.filter(user=user))
    deleted["users.User_groups"] = user.groups.count()
    user.groups.clear()
    deleted["users.User_user_permissions"] = user.user_permissions.count()
    user.user_permissions.clear()
    deleted["token_blacklist.OutstandingToken"] = _revoke_tokens(user)

    user.phone = None
    user.email = ""
    user.first_name = ERASED_NAME
    user.last_name = ""
    # username у OTP-аккаунтов — ``user_<телефон>``: тоже ПДн.
    user.username = f"deleted:{user.pk}"
    user.is_active = False
    user.is_verified = False
    if user.deleted_at is None:
        user.deleted_at = now
    user.save(
        update_fields=[
            "phone", "email", "first_name", "last_name", "username",
            "is_active", "is_verified", "deleted_at",
        ]
    )
    erase_personal_context(user, initiator="deletion_executor")
    deleted["users.UserPersonalContext"] = 1
    anonymised["analytics.AnalyticsEvent.actor"] = AnalyticsEvent.objects.filter(
        actor=user
    ).update(actor=None)

    # 8. Полнота — по перечитанным строкам, внутри транзакции.
    residue = _residue(user)
    if residue:
        raise IncompleteErasure(json.dumps(residue, ensure_ascii=False))

    return {
        "deleted": deleted,
        "anonymised": anonymised,
        "retained": dict(RETAIN),
        "files_deleted": files_deleted,
    }


def _residue(user) -> dict[str, int]:
    """Что осталось на человеке из того, что объявлено стёртым/обезличенным."""
    from appointments.models import (
        Appointment,
        AppointmentRevision,
        IdempotencyKey,
        SpecialistTimeOff,
    )
    from billing.models import BillingConsent, SpecialistSubscription
    from goals.models import ClientGoal, GoalAnketaRun
    from notifications.models import Notification
    from nutrition.models import (
        CrossDomainShownRule,
        FoodLog,
        FoodScan,
        NutritionProfile,
        ProfileIdempotencyKey,
        WaterEntry,
        WaterLog,
    )
    from payments.models import UserPaymentMethod
    from reviews.models import Review
    from users.models import (
        AnonymousSession,
        DeviceToken,
        FavoriteSpecialist,
        Profile,
        SocialAccount,
        SpecialistProfile,
        TenantUserRelationship,
        UserPersonalContext,
    )
    from wellness.models import DesiredOutcome, PersonalPlan, ProgressObservation
    from ai.models import Conversation
    from analytics.models import AnalyticsEvent

    checks = {
        "nutrition.NutritionProfile": NutritionProfile.objects.filter(user=user),
        "nutrition.FoodLog": FoodLog.objects.filter(user=user),
        "nutrition.FoodScan": FoodScan.objects.filter(user=user),
        "nutrition.WaterEntry": WaterEntry.objects.filter(user=user),
        "nutrition.WaterLog": WaterLog.objects.filter(user=user),
        "nutrition.CrossDomainShownRule": CrossDomainShownRule.objects.filter(user=user),
        "nutrition.ProfileIdempotencyKey": ProfileIdempotencyKey.objects.filter(user=user),
        "goals.ClientGoal": ClientGoal.objects.filter(client=user),
        "goals.GoalAnketaRun": GoalAnketaRun.objects.filter(client=user),
        "wellness.DesiredOutcome": DesiredOutcome.objects.filter(user=user),
        "wellness.PersonalPlan": PersonalPlan.objects.filter(user=user),
        "wellness.ProgressObservation": ProgressObservation.objects.filter(user=user),
        "ai.Conversation": Conversation.all_objects.filter(user=user),
        "notifications.Notification": Notification.objects.filter(user=user),
        "appointments.IdempotencyKey": IdempotencyKey.objects.filter(user=user),
        "users.UserPersonalContext": UserPersonalContext.objects.filter(user=user),
        "users.SocialAccount": SocialAccount.objects.filter(user=user),
        "users.DeviceToken": DeviceToken.objects.filter(user=user),
        "users.AnonymousSession": AnonymousSession.objects.filter(user=user),
        "users.FavoriteSpecialist": FavoriteSpecialist.objects.filter(user=user),
        "payments.UserPaymentMethod": UserPaymentMethod.objects.filter(user=user),
        "appointments.Appointment.client": Appointment.objects.filter(client=user),
        "appointments.Appointment.cancelled_by": Appointment.objects.filter(cancelled_by=user),
        "appointments.AppointmentRevision.actor": AppointmentRevision.objects.filter(actor=user),
        "appointments.SpecialistTimeOff.created_by": SpecialistTimeOff.objects.filter(
            created_by=user
        ),
        "reviews.Review.client": Review.objects.filter(client=user),
        "billing.SpecialistSubscription.payment_method": SpecialistSubscription.objects.filter(
            user=user
        ).exclude(payment_method_id=""),
        "billing.BillingConsent.live": BillingConsent.objects.filter(
            user=user, revoked_at__isnull=True
        ),
        "users.TenantUserRelationship.active": TenantUserRelationship.objects.filter(
            user=user, is_active=True
        ),
        "analytics.AnalyticsEvent.actor": AnalyticsEvent.objects.filter(actor=user),
        "users.Profile.pii": Profile.objects.filter(user=user).exclude(
            full_name=ERASED_NAME, bio="", city="",
            default_location_lat=None, default_location_lng=None,
        ),
        "users.Profile.avatar": Profile.objects.filter(user=user).exclude(
            Q(avatar="") | Q(avatar__isnull=True)
        ),
        # Координаты мастера — не фильтром (DRF-1687: фильтр по ним
        # разрешён только замеру состояния), а чтением строки ниже.
        "users.SpecialistProfile.pii": SpecialistProfile.objects.filter(user=user).exclude(
            display_name=ERASED_NAME, bio="", address="",
        ),
        "users.SpecialistProfile.avatar": SpecialistProfile.objects.filter(user=user).exclude(
            Q(avatar="") | Q(avatar__isnull=True)
        ),
        "users.User.pii": User.objects.filter(pk=user.pk).exclude(
            phone=None, email="", first_name=ERASED_NAME, last_name="",
            username=f"deleted:{user.pk}", is_active=False, deleted_at__isnull=False,
        ),
        "users.User.groups": user.groups.all(),
        "users.User.user_permissions": user.user_permissions.all(),
    }
    residue = {k: n for k, qs in checks.items() if (n := qs.count())}
    sp = SpecialistProfile.objects.filter(user=user).first()
    if sp is not None and (sp.location_lat is not None or sp.location_lng is not None):
        residue["users.SpecialistProfile.coordinates"] = 1
    return residue


def _delete_file(fieldfile) -> int:
    """Снять файл с носителя. Отсутствующий файл — не ошибка: повтор после
    отката встречает уже снятые файлы."""
    name = getattr(fieldfile, "name", "") or ""
    if not name:
        return 0
    storage = getattr(fieldfile, "storage", default_storage)
    if not storage.exists(name):
        return 0
    storage.delete(name)
    if storage.exists(name):
        raise IncompleteErasure(f"file still present: {name}")
    return 1


def _revoke_tokens(user) -> int:
    from rest_framework_simplejwt.token_blacklist.models import (
        BlacklistedToken,
        OutstandingToken,
    )

    tokens = OutstandingToken.objects.filter(user=user)
    for token in tokens:
        BlacklistedToken.objects.get_or_create(token=token)
    # Строки без пользователя (SET_NULL) хранили бы jti без пользы —
    # blacklist на них уже стоит, сам токен истечёт.
    n, _ = tokens.delete()
    return n


def _unlink_proxies(user) -> int:
    return User.objects.filter(is_proxy=True, linked_user=user).update(linked_user=None)


def _person_names(user) -> list[str]:
    from users.models import Profile

    raw = {user.first_name, user.last_name}
    profile = Profile.objects.filter(user=user).only("full_name").first()
    if profile is not None:
        raw.add(profile.full_name)
        raw.update(profile.full_name.split())
    return sorted(raw)


_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s\-().]{7,}\d)(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def scrub_personal_data(text: str, names: list[str]) -> str:
    """D9 — текст отзыва остаётся, но без телефона, почты и имени человека.

    Имена короче трёх букв и само слово-заглушка не вырезаются: «Ок» или
    «Ая» вычистили бы обычные слова, а не человека.
    """
    if not text:
        return text
    out = _PHONE_RE.sub(SCRUBBED, text)
    out = _EMAIL_RE.sub(SCRUBBED, out)
    usable = [n for n in names if n and len(n) >= 3 and n != ERASED_NAME]
    for name in sorted(usable, key=len, reverse=True):
        out = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", SCRUBBED, out, flags=re.IGNORECASE)
    return out


# ---------------------------------------------------------------------------
# Бот — подтверждение своей половины
# ---------------------------------------------------------------------------

#: Путь внутренней ручки бота (ai-bot-platform, apps/identity).
BOT_DELETION_PATH_DEFAULT = "/api/v1/internal/privacy/account-deletion/"
HTTP_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class BotConfirmation:
    ok: bool
    steps: dict
    reason: str = ""


class BotDeletionClient:
    """POST в бот: та же подпись, что у outbox-издателя (HMAC-SHA256 по сырому
    телу + метка времени; секрет ``AYLA_OUTBOUND_HMAC_SECRET`` =
    ``EVENT_INGEST_HMAC_SECRET`` бота)."""

    def confirm(self, *, request_id: str, ayla_user_id: str, external_user_ids: list[str]) -> BotConfirmation:
        base = getattr(settings, "BOT_PLATFORM_BASE_URL", "") or ""
        if not base:
            return BotConfirmation(False, {}, "bot_unreachable: BOT_PLATFORM_BASE_URL is unset")
        path = getattr(settings, "BOT_PLATFORM_DELETION_PATH", "") or BOT_DELETION_PATH_DEFAULT
        url = base.rstrip("/") + path
        body = json.dumps(
            {
                "request_id": request_id,
                "ayla_user_id": ayla_user_id,
                "external_user_ids": external_user_ids,
            },
            ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        secret = getattr(settings, "AYLA_OUTBOUND_HMAC_SECRET", "") or ""
        ts_ms = int(time.time() * 1000)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ayla-deletion-executor/1",
            "X-Idempotency-Key": request_id,
            "X-Ayla-Event-Timestamp": str(ts_ms),
        }
        if secret:
            headers["X-Ayla-Event-Signature"] = "sha256=" + hmac.new(
                secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
        try:
            resp = httpx.post(url, content=body, headers=headers, timeout=HTTP_TIMEOUT_S)
        except httpx.HTTPError as exc:
            return BotConfirmation(False, {}, f"bot_unreachable: {exc.__class__.__name__}")
        if resp.status_code != 200:
            return BotConfirmation(False, {}, f"bot_http_{resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            return BotConfirmation(False, {}, "bot_malformed_response")
        payload = data.get("data", data) if isinstance(data, dict) else {}
        if not isinstance(payload, dict) or payload.get("all_ok") is not True:
            failed = payload.get("failed_steps") if isinstance(payload, dict) else None
            return BotConfirmation(False, payload if isinstance(payload, dict) else {},
                                   f"bot_not_ok: {failed}")
        return BotConfirmation(True, payload)


def _confirm_with_bot(request: DeletionRequest, user, *, client=None) -> BotConfirmation:
    client = client or BotDeletionClient()
    external_ids = list(
        User.objects.filter(is_proxy=True, linked_user=user).values_list("username", flat=True)
    )
    return client.confirm(
        request_id=str(request.pk),
        ayla_user_id=str(user.pk),
        external_user_ids=external_ids,
    )


# ---------------------------------------------------------------------------
# Очередь
# ---------------------------------------------------------------------------


#: Окно между приёмом заявки и её исполнением, дней. §7 называет только
#: верхнюю границу («срок завершения — не позднее 30 дней») и «после
#: начала удаления действие нельзя отменить» — числа для окна в §7 нет,
#: поэтому это ПАРАМЕТР с умолчанием, а не решение (вопрос владельцу в
#: OWNER_QUESTIONS). До этого окна тик брал заявку сразу: нажатие в Mini
#: App = стирание через ≤15 минут, а человек на экране видел «крайнюю
#: дату» через месяц.
DELETION_GRACE_SETTING = "DELETION_GRACE_DAYS"
DEFAULT_DELETION_GRACE_DAYS = 30


class GraceMisconfigured(ValueError):
    """Окно задано так, что по нему нельзя решать, кого исполнять."""


def deletion_grace() -> timedelta:
    """Окно из настройки; кривое значение — отказ, а не «ноль дней»."""
    raw = getattr(settings, DELETION_GRACE_SETTING, DEFAULT_DELETION_GRACE_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError) as exc:
        raise GraceMisconfigured(f"{DELETION_GRACE_SETTING}={raw!r}: не число") from exc
    if days < 0:
        raise GraceMisconfigured(f"{DELETION_GRACE_SETTING}={days}: отрицательное")
    return timedelta(days=days)


def open_requests_due(now=None):
    """Заявки, которые исполнителю пора брать: открытые, у которых прошло
    окно :func:`deletion_grace` с момента приёма. Порядок — по сроку, чтобы
    ближайший дедлайн §7 шёл первым.

    Повтор по ``PROCESSING``/``FAILED`` — тоже только после окна: до него
    каталог ничего не начинал, и начинать не должен.
    """
    now = now or timezone.now()
    return (
        DeletionRequest.objects.filter(
            status__in=DeletionRequest.OPEN_STATUSES,
            requested_at__lte=now - deletion_grace(),
        )
        .order_by("deadline_at")
    )
