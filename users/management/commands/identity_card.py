"""Catalog half of the read-only identity card (owner 11.09 §12, DRF-1693).

    python manage.py identity_card --account max:83146139

Reads only. Prints the proxy ``bot:max:<id>`` and — if bound — the real
account, each with name and phone MASKED (first letter + length; presence +
length, never a digit — DRF-1039), plus what the bot's card cannot know:
personal context, goals, questionnaire runs, food logs, appointments by
status with the §16.3 «держат сброс» split. The pair of this command lives
in ``ai-bot-platform``; together they are the card §12 asks for before any
account is freed.

**Журнал §96, fail-closed по §107 (DRF-2655).** Это «просмотр сотрудником
чувствительных профилей» — оператор видит контекст, цели, дневник. Каждый
человек, чья строка попадает в карточку, получает строку
``PersonalDataAccessLog`` (``operator_card_read``) ДО печати. Журнал
недоступен — карточка не печатается. ``actor`` пуст честно
(аутентифицированного актора у команды нет); кто назвался — ``--operator``
(форма DRF-2653: роль или метка, не имя, не проверено).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from privacy_audit.models import PersonalDataAccessLog
from privacy_audit.retention import OPERATOR_MAX_LENGTH
from privacy_audit.services import AuditUnavailable, record_or_lose
from users.identity_card import build_card, parse_account, render_for_operator

_Log = PersonalDataAccessLog


class Command(BaseCommand):
    help = "Print the catalog half of a masked, read-only identity card for channel:channel_user_id."

    def add_arguments(self, parser):
        parser.add_argument(
            "--account", action="append", required=True, metavar="CHANNEL:ID", help="max:83146139"
        )
        parser.add_argument(
            "--operator", required=True,
            help="Кто смотрит: роль или метка, НЕ имя; не проверяется (DRF-2653).",
        )

    def handle(self, *args, **options):
        operator = options["operator"].strip()
        if not operator:
            raise CommandError("--operator пуст: без него карточка не печатается")
        if len(operator) > OPERATOR_MAX_LENGTH:
            raise CommandError(f"--operator длиннее {OPERATOR_MAX_LENGTH} символов: не обрезаю")
        for spec in options["account"]:
            try:
                channel, channel_user_id = parse_account(spec)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            card = build_card(channel, channel_user_id)
            try:
                for row in card.rows:
                    record_or_lose(
                        caller_purpose=_Log.CallerPurpose.OPERATOR_COMMAND,
                        actor=None,
                        object_id=row.user_id,
                        operation=_Log.Operation.OPERATOR_CARD_READ,
                        object_category=_Log.ObjectCategory.PERSONAL_DATA,
                        result=_Log.Result.ALLOWED,
                        actor_named=False,
                        operator=operator,
                    )
            except AuditUnavailable as exc:
                raise CommandError(
                    "журнал доступа недоступен — карточка не печатается (§107)"
                ) from exc
            self.stdout.write(render_for_operator(card))
            self.stdout.write("")
