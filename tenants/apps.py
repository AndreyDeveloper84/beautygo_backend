from django.apps import AppConfig


class TenantsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tenants"
    verbose_name = "Тенанты"

    def ready(self) -> None:
        # DRF-2646: сторож нерешённого признака демо-салона (``tenants.W001``).
        from tenants import checks  # noqa: F401
