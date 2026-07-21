"""Test settings — inherits dev, swaps Redis-backed pieces for in-memory.

Tests run on plain ``pytest`` without spinning up a Redis container.
The throttle / cache / Celery layers each have an in-memory equivalent
that gives the same behaviour for unit-test purposes:

- ``LocMemCache`` instead of ``RedisCache`` so cache.set/get round-trips
  inside the process (throttle tests rely on this exact contract).
- ``CELERY_TASK_ALWAYS_EAGER = True`` runs every @shared_task call
  synchronously in the caller's process — no broker, no worker.

DATABASES is inherited from dev.py — Postgres since #422. Pytest-django
creates ``test_<POSTGRES_DB>`` on first run from the same credentials.
The CI workflow (.github/workflows/ci.yml) provisions a Postgres 16
service container and passes the matching POSTGRES_* env vars. Locally,
the developer must boot the compose `db` service first
(`docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d db`)
or run a sibling Postgres on localhost:5432.

Integration tests that need real Redis can override via env or use the
``@pytest.mark.integration`` marker (skipped by default).
"""
from .dev import *  # noqa: F401,F403


# DRF-242.5: dev.py turns strict tenant mode on by default. Unit tests
# use APIClient without an X-Tenant header — middleware would 400 every
# /api/v1/* call before the view runs, breaking ~350 pre-existing tests
# wholesale. Strict-mode-specific tests in tenants/tests/test_middleware
# _and_permission.py flip the flag per-test via the ``settings`` fixture
# so they keep covering the strict path.
MULTI_TENANT_STRICT = False


# DRF-288 — open the cross-domain (Track E) rollout gate for tests by
# default. Per-test overrides via ``override_settings(...)`` or pytest's
# ``settings`` fixture exercise the gate explicitly in TestRolloutGate.
CROSS_DOMAIN_ENABLED = True


# Webhook tests post from the test client (127.0.0.1). Allow ONLY loopback
# — never an empty list (empty = allow-all, amendment J). Security tests
# override this per-test with their own ranges.
YOOKASSA_WEBHOOK_ALLOWED_IPS = ["127.0.0.1/32"]


CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "ayla-test-cache",
    },
}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# dev.py points STORAGES at S3Boto3Storage backed by Minio on localhost:9000.
# That's right for runserver (Minio sibling container) but wrong for plain
# pytest — there's no Minio running and any ImageField save() would attempt
# a real S3 PUT and fail. Tests use the in-process FileSystemStorage instead
# so portfolio / avatar uploads round-trip locally.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}
