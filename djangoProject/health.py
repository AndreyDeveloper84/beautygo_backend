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
from django.http import JsonResponse


VERSION = getattr(settings, "APP_VERSION", "1.0.0")


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


def _envelope(checks: dict[str, dict[str, Any]], healthy: bool):
    return JsonResponse(
        {
            "status": "ok" if healthy else "unhealthy",
            "version": VERSION,
            "timestamp": int(time.time()),
            "checks": checks,
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
    )
