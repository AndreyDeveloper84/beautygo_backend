from django.apps import AppConfig


class UsersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'users'

    def ready(self):
        import users.checks  # noqa: F401
        import users.signals  # noqa: F401

        # Handoff A9 — log resolved ayla-ai-core version once at boot
        # so the operator can confirm Ayla and bot-platform stay on
        # the same SHA. Wrapped in try/except so a missing dep does
        # not abort boot (the probe itself decides what to log).
        try:
            from core.ai_core import log_ai_core_version
            log_ai_core_version()
        except Exception:  # noqa: BLE001 — boot probe must never crash startup
            import logging
            logging.getLogger("ayla.bootstrap").exception(
                "ayla-ai-core version probe failed during AppConfig.ready"
            )
