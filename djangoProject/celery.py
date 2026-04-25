"""Celery app instance.

Loaded eagerly at Django startup via ``djangoProject/__init__.py`` so
``@shared_task`` decorators across all installed apps register their
tasks against this app instance, not the default-but-disconnected one.

Uses ``django:`` namespace for settings — every Celery setting must be
prefixed ``CELERY_`` in Django settings (see ``settings/base.py``).
"""
from __future__ import annotations

import os

from celery import Celery


# Default to dev settings module — production deploys override via env.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "djangoProject.settings.dev")

app = Celery("ayla")

# All Celery settings live in Django settings under the CELERY_ namespace
# (CELERY_BROKER_URL, CELERY_TASK_ALWAYS_EAGER, etc). Single source of
# truth, env-driven via ``settings/base.py``.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Tell Celery to scan every installed app for ``tasks.py``. The outbox
# dispatcher in ``appointments/tasks.py`` is the only task today; new
# tasks just need a tasks.py in their own app.
app.autodiscover_tasks()
