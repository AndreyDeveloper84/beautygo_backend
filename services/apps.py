from django.apps import AppConfig


class ServicesConfig(AppConfig):
    name = 'services'

    def ready(self) -> None:
        # Системная проверка DRF-1685: обратное геокодирование не притворяется
        # живым. Импорт ради побочного эффекта @register — намеренно здесь,
        # у единственного потребителя (services/pricing.py).
        from core.geocoding import checks  # noqa: F401
