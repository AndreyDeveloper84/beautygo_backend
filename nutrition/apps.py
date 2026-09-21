from django.apps import AppConfig


class NutritionConfig(AppConfig):
    name = "nutrition"
    verbose_name = "Nutrition / Food Scanner"

    def ready(self) -> None:
        # DRF-2227 — сброс AI-комментария к дню при изменении дневника.
        from nutrition import signals  # noqa: F401
