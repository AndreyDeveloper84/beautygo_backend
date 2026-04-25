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
