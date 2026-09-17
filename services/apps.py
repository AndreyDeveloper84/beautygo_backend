from django.apps import AppConfig


class ServicesConfig(AppConfig):
    name = 'services'

    def ready(self) -> None:
        # Системные проверки геокодирования. Импорт ради побочного эффекта
        # @register — намеренно здесь, у единственного потребителя
        # (services/pricing.py); у `core` нет AppConfig, и заводить его ради
        # одной проверки значило бы править загрузку приложений всего проекта.
        #
        # ВНИМАНИЕ: этим импортом регистрируются ДВЕ проверки, не одна —
        # DRF-1685 (обратное геокодирование, geocoding.W001/E001) и DRF-2028
        # (геокодирование мест, geocoding.W002). Снятие импорта снимает обе;
        # ловит это узел `core/tests/test_places_geocoding_check_2028.py::
        # test_both_checks_are_wired_into_manage_py_check` — он идёт через
        # подпроцесс `manage.py check`, а не импортирует модуль сам.
        from core.geocoding import checks  # noqa: F401
