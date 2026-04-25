"""djangoProject package.

Eagerly imports the Celery app so ``@shared_task`` decorators register
against the right app at module import time. Without this, tasks
defined in installed apps end up on a phantom default app and never
fire.
"""
from .celery import app as celery_app


__all__ = ("celery_app",)
