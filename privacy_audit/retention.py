"""Ретенция журнала доступа к персданным — единственный путь удаления (DRF-1782, §96).

Период — **параметр с умолчанием, не решение**: ``PRIVACY_AUDIT_RETENTION_DAYS``
(умолчание :data:`DEFAULT_RETENTION_DAYS` = 365). Год — временное продуктовое
решение владельца до юридической проверки (§96; докстринг
``privacy_audit/models.py``). Когда проверка состоится, меняется настройка
(или её умолчание здесь) — не код удаления.

### Почему путь один и назван

Менеджер журнала отказывает в ``delete()`` — журнал append-only, и это
механизм, а не договорённость. Ретенция — единственное исключение, и оно
не обходит отказ «через ``update``» и не зовёт ORM в обход менеджера:
:meth:`PersonalDataAccessLogQuerySet.prune_before` — один метод, который
явно вызывает базовое удаление на отфильтрованном по ``occurred_at``
наборе. Всё остальное по-прежнему ``NotImplementedError``.

Кривая настройка (не число, ноль, отрицательное) — не «удалить всё» и не
«удалить по умолчанию»: :class:`RetentionMisconfigured`, ничего не удалено.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Умолчание — временное решение §96 (год), не результат юридической проверки.
DEFAULT_RETENTION_DAYS = 365

RETENTION_SETTING = "PRIVACY_AUDIT_RETENTION_DAYS"


class RetentionMisconfigured(ValueError):
    """Период задан так, что удалять по нему нельзя."""


def retention_days() -> int:
    raw = getattr(settings, RETENTION_SETTING, DEFAULT_RETENTION_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError) as exc:
        raise RetentionMisconfigured(f"{RETENTION_SETTING}={raw!r}: не число") from exc
    if days < 1:
        raise RetentionMisconfigured(f"{RETENTION_SETTING}={days}: меньше одного дня")
    return days


@dataclass(frozen=True)
class PruneOutcome:
    retention_days: int
    cutoff: datetime
    matched: int
    deleted: int
    remaining: int
    oldest_remaining: datetime | None
    dry_run: bool


def prune_expired(*, now: datetime | None = None, dry_run: bool = False) -> PruneOutcome:
    """Удалить строки старше периода. ``dry_run`` — только посчитать."""
    from privacy_audit.models import PersonalDataAccessLog

    days = retention_days()
    now = now or timezone.now()
    cutoff = now - timedelta(days=days)

    expired = PersonalDataAccessLog.objects.filter(occurred_at__lt=cutoff)
    matched = expired.count()
    deleted = 0
    if not dry_run and matched:
        deleted = expired.prune_before(cutoff)

    remaining_qs = PersonalDataAccessLog.objects.all()
    remaining = remaining_qs.count()
    oldest = remaining_qs.order_by("occurred_at").values_list("occurred_at", flat=True).first()
    outcome = PruneOutcome(
        retention_days=days,
        cutoff=cutoff,
        matched=matched,
        deleted=deleted,
        remaining=remaining,
        oldest_remaining=oldest,
        dry_run=dry_run,
    )
    logger.info(
        "privacy_audit.prune_expired retention_days=%d cutoff=%s matched=%d deleted=%d "
        "remaining=%d oldest_remaining=%s dry_run=%s",
        days, cutoff.isoformat(), matched, deleted, remaining,
        oldest.isoformat() if oldest else None, dry_run,
    )
    return outcome
