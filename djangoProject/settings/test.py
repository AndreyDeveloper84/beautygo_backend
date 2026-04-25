"""Test settings — inherits dev, swaps Redis-backed pieces for in-memory.

Tests run on plain ``pytest`` without spinning up a Redis container.
The throttle / cache / Celery layers each have an in-memory equivalent
that gives the same behaviour for unit-test purposes:

- ``LocMemCache`` instead of ``RedisCache`` so cache.set/get round-trips
  inside the process (throttle tests rely on this exact contract).
- ``CELERY_TASK_ALWAYS_EAGER = True`` runs every @shared_task call
  synchronously in the caller's process — no broker, no worker.

Integration tests that need real Redis can override via env or use the
``@pytest.mark.integration`` marker (skipped by default).
"""
from .dev import *  # noqa: F401,F403


CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "ayla-test-cache",
    },
}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
