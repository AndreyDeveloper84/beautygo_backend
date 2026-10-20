from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'billing'
    verbose_name = 'Биллинг'

    def ready(self) -> None:
        # DRF-2984: web and celery refuse to start with charges on and
        # accounting off.
        from billing.flags import check_flag_pair

        check_flag_pair()
