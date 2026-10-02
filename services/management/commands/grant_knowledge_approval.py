"""Выдать право подтверждать знание о процедурах — без оболочки (DRF-2726).

Право подтверждения (``services.approve_procedurecapability`` и
``services.approve_capabilitygoallink``) отдельно от права изменения. Раздаёт
его владелец — группами и правами в Django-admin. Но раздать право в админке
может только тот, кто вправе управлять правами, а на стенде нет оболочки: если
такой учётки под рукой нет, первое право выдать нечем. Эта команда — тот самый
первый шаг.

Кому
----
* ``python manage.py grant_knowledge_approval <username> [<username> …]`` —
  названным учёткам;
* без аргументов — учёткам из переменной окружения
  ``KNOWLEDGE_APPROVER_USERNAMES`` (через запятую). Так команду зовёт
  ``entrypoint.sh`` при старте web. Переменная пуста или не задана — команда
  печатает, что выдавать некому, и завершается успехом.

Что команда делает и чего не делает
-----------------------------------
* Выдаёт ровно два права подтверждения. Права изменения, входа в админку,
  суперпользователя она не даёт и учёток не заводит.
* Выдаёт только действующему сотруднику (``is_active`` и ``is_staff``):
  остальным право подтверждения в админке всё равно ничего не откроет.
  Неизвестное имя или неподходящая учётка — предупреждение, не сбой.
* Идемпотентна: повторный запуск ничего не меняет.
* **Не отзывает.** Убрать имя из переменной — не значит отозвать право: оно
  остаётся в базе и снимается в админке.
* **Пока имя в переменной, право возвращается при каждом старте.** Право,
  снятое в админке у учётки из переменной, вернётся со следующим запуском.
  Отзыв = убрать имя из переменной И снять право.

Имён в коде нет: кто подтверждает — решение владельца.
"""

from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand, CommandError

ENV_NAME = "KNOWLEDGE_APPROVER_USERNAMES"
APPROVAL_CODENAMES = ("approve_procedurecapability", "approve_capabilitygoallink")


class Command(BaseCommand):
    help = "Grant the right to approve procedure knowledge to the named staff accounts."

    def add_arguments(self, parser) -> None:
        parser.add_argument("usernames", nargs="*")

    def handle(self, *args, **options) -> None:
        usernames = list(options["usernames"]) or [
            name.strip() for name in os.environ.get(ENV_NAME, "").split(",") if name.strip()
        ]
        if not usernames:
            self.stdout.write(f"No usernames given and {ENV_NAME} is empty — nothing to grant.")
            return

        permissions = list(
            Permission.objects.filter(
                content_type__app_label="services", codename__in=APPROVAL_CODENAMES,
            )
        )
        if len(permissions) != len(APPROVAL_CODENAMES):
            # Права заводит post_migrate; их отсутствие — не «некому выдавать».
            raise CommandError("Approval permissions are not in the database yet — run migrate first.")

        user_model = get_user_model()
        granted = unchanged = skipped = 0
        for username in usernames:
            user = user_model.objects.filter(username=username).first()
            if user is None or not (user.is_active and user.is_staff):
                # Имя учётки — не ПДн клиента, но и оно в журнал не пишется.
                self.stderr.write(
                    "WARNING: a named account does not exist or is not an active staff account — skipped."
                )
                skipped += 1
                continue
            missing = [p for p in permissions if not user.user_permissions.filter(pk=p.pk).exists()]
            if missing:
                user.user_permissions.add(*missing)
                granted += 1
            else:
                unchanged += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"Knowledge approval: granted={granted} already_had={unchanged} skipped={skipped}."
            )
        )
