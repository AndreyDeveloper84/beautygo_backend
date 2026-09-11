"""Catalog half of the read-only identity card (owner 11.09 §12, DRF-1693).

    python manage.py identity_card --account max:83146139

Reads only. Prints the proxy ``bot:max:<id>`` and — if bound — the real
account, each with name and phone MASKED (first letter + length; presence +
length, never a digit — DRF-1039), plus what the bot's card cannot know:
personal context, goals, questionnaire runs, food logs, appointments by
status with the §16.3 «держат сброс» split. The pair of this command lives
in ``ai-bot-platform``; together they are the card §12 asks for before any
account is freed.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from users.identity_card import build_card, parse_account, render_for_operator


class Command(BaseCommand):
    help = "Print the catalog half of a masked, read-only identity card for channel:channel_user_id."

    def add_arguments(self, parser):
        parser.add_argument(
            "--account", action="append", required=True, metavar="CHANNEL:ID", help="max:83146139"
        )

    def handle(self, *args, **options):
        for spec in options["account"]:
            try:
                channel, channel_user_id = parse_account(spec)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(render_for_operator(build_card(channel, channel_user_id)))
            self.stdout.write("")
