"""Правка и удаление сохранённой записи дневника еды (DRF-1838, §109 шаг 7).

«Сохранённую запись можно изменить или удалить» — обязательный шаг §109:
текст ошибается чаще фото, и без правки дневник копит мусор за неделю.

* **Правка порции** масштабирует снимок записи (макро и микронутриенты)
  тем же ``_scale``, что и запись, — отношением новой порции к старой.
  Повторный поиск блюда не делается: снимок стабильнее справочника, это
  правило самой записи (``food_log_service``). Число названо исправленным
  клиентом (§136): ``*_estimated_confirmed`` → ``*_user_corrected``.
  Неизвестное происхождение (``NULL``) остаётся неизвестным — правка его
  не выдумывает.
* **Правка приёма пищи** чисел не касается и происхождение не меняет.
* **Удаление** переносит запись в снимок ``DeletedFoodLog`` и удаляет
  строку ``FoodLog`` (почему не колонка мягкого удаления — докстринг
  модели). Восстановление в окне возвращает ту же запись с тем же ``id``;
  после окна — 410 и снимок стирается.
* **Зеркало воды** (``WaterEntry.food_log``) не правится и не удаляется
  здесь: его ведёт отмена стакана, и две ручки на одну строку разошлись бы.

Владелец записи — только сам человек: запись ищется по ``id`` И ``user_id``,
чужая неотличима от несуществующей (404, без утечки существования).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as dt_tz
from uuid import UUID

from django.db import transaction
from django.utils.dateparse import parse_datetime

from nutrition.models import DeletedFoodLog, FoodLog, FoodScan
from nutrition.services.food_log_service import _MICRONUTRIENT_FIELDS, _scale, _scale_micro

logger = logging.getLogger(__name__)

#: Окно восстановления — то же число, что у воды
#: (``water_entry_service.RESTORE_WINDOW_MINUTES``).
RESTORE_WINDOW_MINUTES = 15

_MACRO_FIELDS = ("calories", "protein_g", "fat_g", "carbs_g")

_CORRECTED_ORIGIN = {
    FoodLog.EntryOrigin.TEXT_ESTIMATED_CONFIRMED: FoodLog.EntryOrigin.TEXT_USER_CORRECTED,
    FoodLog.EntryOrigin.PHOTO_ESTIMATED_CONFIRMED: FoodLog.EntryOrigin.PHOTO_USER_CORRECTED,
}


class FoodLogEditError(Exception):
    """Base for edit/delete/restore refusals."""


class FoodLogNotFoundError(FoodLogEditError):
    """No such entry for THIS user — a stranger's entry is reported the same."""


class FoodLogManagedByWaterError(FoodLogEditError):
    """The entry mirrors a water entry; the water undo owns it."""


class FoodLogNotScalableError(FoodLogEditError):
    """The stored portion is not positive, so a ratio cannot be taken."""


class RestoreWindowExpiredError(FoodLogEditError):
    """The restore window has closed; the snapshot is erased."""


def _now() -> datetime:
    return datetime.now(dt_tz.utc)


def _owned_for_write(user_id: int, log_id: UUID) -> FoodLog:
    try:
        log = FoodLog.objects.select_for_update().get(id=log_id, user_id=user_id)
    except FoodLog.DoesNotExist as exc:
        raise FoodLogNotFoundError(str(log_id)) from exc
    if log.water_entries.exists():
        raise FoodLogManagedByWaterError(str(log_id))
    return log


def update_food_log(
    *,
    user_id: int,
    log_id: UUID,
    portion_multiplier: float | None = None,
    meal_type: str | None = None,
) -> FoodLog:
    with transaction.atomic():
        log = _owned_for_write(user_id, log_id)
        changed: list[str] = []
        if portion_multiplier is not None and portion_multiplier != log.portion_multiplier:
            if log.portion_multiplier <= 0:
                raise FoodLogNotScalableError(str(log_id))
            ratio = portion_multiplier / log.portion_multiplier
            for field in _MACRO_FIELDS:
                setattr(log, field, _scale(getattr(log, field), ratio))
            for field in _MICRONUTRIENT_FIELDS:
                setattr(log, field, _scale_micro(getattr(log, field), ratio))
            log.portion_multiplier = portion_multiplier
            changed += ["portion_multiplier", *_MACRO_FIELDS, *_MICRONUTRIENT_FIELDS]
            corrected = _CORRECTED_ORIGIN.get(log.entry_origin)
            if corrected is not None:
                log.entry_origin = corrected
                changed.append("entry_origin")
        if meal_type is not None and meal_type != log.meal_type:
            log.meal_type = meal_type
            changed.append("meal_type")
        if changed:
            log.save(update_fields=changed)
    logger.info(
        "nutrition.food_log.updated user_id=%s log_id=%s fields=%s",
        user_id, log_id, ",".join(sorted(set(changed))) or "-",
    )
    return log


def _snapshot(log: FoodLog) -> dict:
    out: dict = {}
    for field in FoodLog._meta.concrete_fields:
        value = getattr(log, field.attname)
        if isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, UUID):
            value = str(value)
        out[field.attname] = value
    return out


def delete_food_log(*, user_id: int, log_id: UUID) -> DeletedFoodLog:
    purge_expired_deleted_food_logs()
    with transaction.atomic():
        log = _owned_for_write(user_id, log_id)
        deleted = DeletedFoodLog.objects.create(
            id=log.id, user_id=user_id, snapshot=_snapshot(log), deleted_at=_now(),
        )
        log.delete()
    logger.info("nutrition.food_log.deleted user_id=%s log_id=%s", user_id, log_id)
    return deleted


def _recreate(snapshot: dict, user_id: int) -> FoodLog:
    data = dict(snapshot)
    for key in ("logged_at", "created_at"):
        if data.get(key):
            data[key] = parse_datetime(data[key])
    data["user_id"] = user_id
    # Скан мог уйти за окно (§134) — запись остаётся, связь нет: макро в снимке.
    if data.get("scan_id") and not FoodScan.objects.filter(
        id=data["scan_id"], user_id=user_id,
    ).exists():
        data["scan_id"] = None
    # Ключ повтора уже мог занять новый вызов — запись важнее ключа.
    if data.get("idempotency_key") and FoodLog.objects.filter(
        idempotency_key=data["idempotency_key"],
    ).exists():
        data["idempotency_key"] = None
    known = {f.attname for f in FoodLog._meta.concrete_fields}
    created_at = data.get("created_at")
    log = FoodLog(**{k: v for k, v in data.items() if k in known})
    log.save(force_insert=True)
    if created_at is not None:
        # auto_now_add перезаписал бы момент записи моментом восстановления.
        FoodLog.objects.filter(pk=log.pk).update(created_at=created_at)
        log.created_at = created_at
    return log


def restore_food_log(*, user_id: int, log_id: UUID) -> FoodLog:
    deleted = DeletedFoodLog.objects.filter(id=log_id, user_id=user_id).first()
    if deleted is None:
        raise FoodLogNotFoundError(str(log_id))
    if _now() - deleted.deleted_at > timedelta(minutes=RESTORE_WINDOW_MINUTES):
        # Окончательно — значит, и снимка больше нет.
        deleted.delete()
        raise RestoreWindowExpiredError(str(log_id))
    with transaction.atomic():
        locked = DeletedFoodLog.objects.select_for_update().filter(pk=deleted.pk).first()
        if locked is None:
            # Параллельное восстановление успело первым.
            existing = FoodLog.objects.filter(id=log_id, user_id=user_id).first()
            if existing is None:
                raise FoodLogNotFoundError(str(log_id))
            return existing
        log = _recreate(locked.snapshot, user_id)
        locked.delete()
    logger.info("nutrition.food_log.restored user_id=%s log_id=%s", user_id, log_id)
    return log


def purge_expired_deleted_food_logs(now: datetime | None = None) -> int:
    """Стереть снимки, чьё окно восстановления закрылось. Возвращает число."""
    cutoff = (now or _now()) - timedelta(minutes=RESTORE_WINDOW_MINUTES)
    purged, _ = DeletedFoodLog.objects.filter(deleted_at__lt=cutoff).delete()
    return purged
