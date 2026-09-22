"""Стойкий отказ распознавателя фото — сигнал операторам (DRF-2318).

Живой проход владельца 22.09: OpenAI ответил 429 ``billing_not_active``, резерв
тоже отказал, и человек получил «попробуй через минуту». Через минуту ничего
не меняется — счёт не оплачен, ключ отвергнут, квота исчерпана. Такой отказ
чинит человек, а не ретрай, поэтому о нём должны узнать операторы.

Рельс — тот же, что у сигнала бюджета (DRF-2196, :mod:`nutrition.services.food_scan_budget`):
``system.module.health.degraded`` через outbox, бот поднимает страницу в MAX. Имя
модуля — своё, ``nutrition.food_scan.provider``: под ``nutrition.food_scan``
потребитель бота читает метрику бюджета (``used``/``limit``/``day``) и сигнал
без ``day`` молча отбросил бы (``eventbus.system.day_invalid``) — операторы бы
ничего не узнали. Бот понимает этот модуль своим ядром (PR бота DRF-2318). Один сигнал на (провайдер, причина) за
UTC-час — дедуп через кэш, как у dead-letter outbox (DRF-2306): сто снимков за
час простоя — одна страница.

В сигнале нет ни текста ошибки провайдера, ни людей: провайдер, причина
закрытым словом, час. Отказ публикации сигнала скан не роняет.
"""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

_SIGNAL_TOPIC = "system.module.health.degraded"
_SIGNAL_MODULE = "nutrition.food_scan.provider"
# Ключ часа живёт дольше часа: переживает сдвиг часов воркеров (как DRF-2306).
_DEDUP_TTL_SECONDS = 2 * 60 * 60


def signal_provider_down(*, provider: str, reason: str) -> bool:
    """Сигнал «провайдер отказал стойко» — если этот час по (провайдер, причина) ещё не звучал.

    Возвращает ``True``, если событие записано. Никогда не бросает.
    """
    hour = timezone.now().strftime("%Y-%m-%dT%H")
    key = f"food_scan:provider_down:{provider}:{reason}:{hour}"
    try:
        if not cache.add(key, 1, timeout=_DEDUP_TTL_SECONDS):
            return False
    except Exception as exc:  # noqa: BLE001 — потеря кэша: молчание, а не шквал
        logger.warning("nutrition.food_scan.provider_signal_dedup_unavailable err=%s", type(exc).__name__)
        return False

    try:
        from django.db import transaction

        from appointments.infrastructure.outbox.envelope import emit_outbox_event

        # Savepoint: у системного сигнала нет доменного изменения, и его отказ
        # не должен ломать внешнюю транзакцию (как у сигнала бюджета).
        with transaction.atomic():
            emit_outbox_event(
                topic=_SIGNAL_TOPIC,
                data={
                    "module_name": _SIGNAL_MODULE,
                    "severity": "error",
                    "metric": {"provider": provider, "reason": reason, "hour": hour},
                },
                actor="system",
                user_id=None,
                tenant_id=None,
            )
    except Exception as exc:  # noqa: BLE001 — сигнал не роняет скан
        logger.warning(
            "nutrition.food_scan.provider_signal_failed provider=%s reason=%s err=%s",
            provider, reason, type(exc).__name__,
        )
        # Час не занят молчанием: следующий стойкий отказ попробует снова (ревью #549).
        try:
            cache.delete(key)
        except Exception:  # noqa: BLE001
            pass
        return False
    logger.warning("nutrition.food_scan.provider_down provider=%s reason=%s", provider, reason)
    return True


__all__ = ["signal_provider_down"]
