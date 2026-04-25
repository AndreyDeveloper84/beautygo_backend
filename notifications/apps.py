from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = "notifications"
    verbose_name = "Notifications"

    def ready(self) -> None:
        # Late import — appointments must be loaded first so its
        # OutboxEvent.Topic enum + EVENT_HANDLERS dict exist when we
        # register against them. signal-style registration is preferable
        # to a hard import dependency from appointments → notifications.
        from . import outbox_handlers  # noqa: F401
