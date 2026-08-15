from django.apps import AppConfig


class AppointmentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'appointments'
    verbose_name = 'Записи'

    def ready(self) -> None:
        # DRF-1062 — slot-cache invalidation for per-date schedule
        # overrides and salon closures. Imported for the side effect of
        # registering the receivers.
        from . import signals  # noqa: F401
