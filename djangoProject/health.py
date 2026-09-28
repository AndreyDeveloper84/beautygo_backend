"""Health check endpoints for liveness + readiness probes.

Two URLs, two semantics:

- ``GET /api/v1/health/`` — **liveness**. The process is up and can talk
  to its critical dependencies (DB + cache). Loadbalancers hit this every
  few seconds. Failure here means "remove me from rotation".

- ``GET /api/v1/health/ready/`` — **readiness**. Liveness AND every
  unapplied migration is applied. Used by deploy scripts: a freshly
  rolled pod is "live" before migrate runs but only "ready" after. Hit
  this at boot, not on every tick.

Both endpoints return JSON with per-check status so on-call can read at
a glance. They never block longer than ~100ms (DB + cache only); slow
checks belong in dedicated monitoring, not on a request path that's
expected to scale to thousands of probes per minute.
"""
from __future__ import annotations

import time
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Count, Max, Min, Q
from django.http import JsonResponse

from appointments.models import OutboxEvent


VERSION = getattr(settings, "APP_VERSION", "1.0.0")

#: Сколько секунд живёт подсчёт мёртвых писем в readiness (DRF-2525).
OUTBOX_CACHE_TTL_S = 60
OUTBOX_CACHE_KEY = "_health_outbox_delivery"


def _check_db() -> tuple[bool, str]:
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True, "ok"
    except Exception as exc:  # pragma: no cover — disaster path
        return False, f"error: {exc.__class__.__name__}"


def _check_cache() -> tuple[bool, str]:
    probe_key = "_health_probe"
    try:
        cache.set(probe_key, "1", timeout=5)
        ok = cache.get(probe_key) == "1"
        return (True, "ok") if ok else (False, "round-trip failed")
    except Exception as exc:  # pragma: no cover
        return False, f"error: {exc.__class__.__name__}"


def _check_migrations() -> tuple[bool, str]:
    try:
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception as exc:  # pragma: no cover
        return False, f"error: {exc.__class__.__name__}"
    if plan:
        return False, f"{len(plan)} unapplied"
    return True, "applied"


def _envelope(
    checks: dict[str, dict[str, Any]],
    healthy: bool,
    extra: dict[str, Any] | None = None,
):
    return JsonResponse(
        {
            "status": "ok" if healthy else "unhealthy",
            "version": VERSION,
            "timestamp": int(time.time()),
            "checks": checks,
            **(extra or {}),
        },
        status=200 if healthy else 503,
    )


def liveness(request):
    db_ok, db_msg = _check_db()
    cache_ok, cache_msg = _check_cache()
    healthy = db_ok and cache_ok
    return _envelope(
        {
            "db": {"ok": db_ok, "detail": db_msg},
            "cache": {"ok": cache_ok, "detail": cache_msg},
        },
        healthy=healthy,
    )


def _outbox_delivery() -> dict[str, Any]:
    """Мёртвые письма ящика по темам — с охватом рядом с числом (DRF-2525).

    Шесть ``booking.completed`` пролежали мёртвыми две недели, и никто не
    заметил: ни один ответ, доступный без shell, мёртвых писем не показывал.
    Здесь — только количества: ручка открыта без авторизации, поэтому ни id, ни
    полезной нагрузки, ни текста ошибок.

    Охват (``rows_seen`` и окно ``created_at``) стоит рядом с числом нарочно:
    без него ``dead_total = 0`` значило бы и «всё дошло», и «смотрел не туда».

    Информационный ключ, не проверка: на ``status``/503 не влияет. Мёртвое
    письмо — повод человеку посмотреть, а не повод выводить машину из ротации.
    Ошибка подсчёта возвращается в ключе и readiness не роняет.

    Цена. Ручку опрашивают CI и скрипты выкладки до переключения трафика, а
    ``GROUP BY`` по всему ящику — последовательный проход таблицы (индекс по
    ``bot_delivery_status`` есть, но охват требует считать всё). Поэтому успешный
    результат кэшируется на :data:`OUTBOX_CACHE_TTL_S` секунд: не больше одного
    прохода в минуту при любой частоте опроса. ``computed_at`` в ответе говорит,
    насколько число свежее. Ошибка не кэшируется — следующий опрос пробует снова.
    """
    cached = cache.get(OUTBOX_CACHE_KEY)
    if cached is not None:
        return cached
    result = _count_outbox_delivery()
    if "error" not in result:
        cache.set(OUTBOX_CACHE_KEY, result, timeout=OUTBOX_CACHE_TTL_S)
    return result


def _count_outbox_delivery() -> dict[str, Any]:
    try:
        rows = list(
            OutboxEvent.objects.values("topic", "bot_delivery_status").annotate(n=Count("id"))
        )
        dead = OutboxEvent.BotDeliveryStatus.DEAD
        bounds = OutboxEvent.objects.aggregate(
            first=Min("created_at"),
            last=Max("created_at"),
            oldest_dead=Min("bot_dead_lettered_at", filter=Q(bot_delivery_status=dead)),
        )
    except Exception as exc:  # observability must not break readiness
        return {"error": exc.__class__.__name__}

    by_status: dict[str, int] = {}
    dead_by_topic: dict[str, int] = {}
    for row in rows:
        status = row["bot_delivery_status"]
        by_status[status] = by_status.get(status, 0) + row["n"]
        if status == dead:
            dead_by_topic[row["topic"]] = dead_by_topic.get(row["topic"], 0) + row["n"]

    def _iso(value):
        return value.isoformat() if value else None

    return {
        "rows_seen": sum(by_status.values()),
        "window": {"from": _iso(bounds["first"]), "to": _iso(bounds["last"])},
        "by_delivery_status": by_status,
        "dead_total": sum(dead_by_topic.values()),
        "dead_by_topic": dead_by_topic,
        "oldest_dead_at": _iso(bounds["oldest_dead"]),
        "computed_at": int(time.time()),
        "cache_ttl_s": OUTBOX_CACHE_TTL_S,
    }


def readiness(request):
    db_ok, db_msg = _check_db()
    cache_ok, cache_msg = _check_cache()
    mig_ok, mig_msg = _check_migrations()
    healthy = db_ok and cache_ok and mig_ok
    return _envelope(
        {
            "db": {"ok": db_ok, "detail": db_msg},
            "cache": {"ok": cache_ok, "detail": cache_msg},
            "migrations": {"ok": mig_ok, "detail": mig_msg},
        },
        healthy=healthy,
        # Вне ``checks``: от этого ключа статус не зависит (DRF-2525).
        extra={"outbox": _outbox_delivery() if db_ok else {"error": "db unavailable"}},
    )
