"""Free a TEST account for another pilot check-through — catalog half (B-R, DRF-1617).

    python manage.py reset_test_account --account max:83146139 --mode client-onboarding
    python manage.py reset_test_account --account max:83146139 --mode client-onboarding --apply

Without ``--apply`` the command only READS. ``--apply`` refuses unless every
row it would remove — the proxy ``bot:max:<id>`` AND the real account the
proxy is bound to, by its own UUID — is on ``ACCOUNT_RESET_ALLOWLIST``. On
the pilot that list is empty.

Run this half BEFORE the bot half. See ``users/account_reset.py``.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from users import account_reset as reset


class Command(BaseCommand):
    help = "Free a test account for another check-through (catalog half). Reads unless --apply."

    def add_arguments(self, parser):
        parser.add_argument(
            "--account",
            action="append",
            required=True,
            metavar="CHANNEL:ID",
            help="channel:channel_user_id of the bot identity, e.g. max:83146139. Repeatable.",
        )
        parser.add_argument(
            "--mode",
            required=True,
            choices=sorted(reset.MODES),
            help=" | ".join(f"{m.name} — {m.frees}" for m in reset.MODES.values()),
        )
        parser.add_argument("--apply", action="store_true", help="Actually free the account.")

    def handle(self, *args, **options):
        mode = reset.MODES[options["mode"]]
        specs = options["account"]
        for spec in specs:
            try:
                reset.parse_account(spec)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc

        allowlist = reset.allowlist()
        self.stdout.write(f"режим        {mode.name} — {mode.frees}")
        self.stdout.write(
            f"allowlist    {len(allowlist)} запис(ей)" + ("" if allowlist else " — ПУСТ: --apply откажет всем")
        )
        self.stdout.write(f"действие     {'ПРИМЕНЕНИЕ' if options['apply'] else 'сухой прогон, только чтение'}")

        refused = []
        for spec in specs:
            self.stdout.write("")
            self.stdout.write(f"=== {spec}  (username {reset.proxy_username(spec)})")
            p = reset.plan(spec, mode.name)
            self._print_plan(p)
            if not p.subjects:
                continue
            if p.blockers or (options["apply"] and p.unlisted):
                refused.append(spec)
                continue
            if not options["apply"]:
                continue

            report = reset.apply(spec, mode.name)
            if report.leftovers:
                self.stdout.write("ОТКАЗ  после удаления в базе осталось — транзакция откачена:")
                for lo in report.leftovers:
                    self.stdout.write(f"       {lo.label:60} {lo.rows}")
                refused.append(spec)
                continue
            self.stdout.write("удалено (по моделям):")
            for label, n in sorted(report.removed.items()):
                self.stdout.write(f"       {label:60} {n}")
            self.stdout.write(self.style.SUCCESS(f"ГОТОВО {spec}: {mode.frees}"))

        if refused:
            raise CommandError("отказано: " + ", ".join(refused))

    def _print_plan(self, p):
        if not p.subjects:
            self.stdout.write("аккаунт не найден — 0 строк users.User; освобождать нечего")
            return
        self.stdout.write("субъекты (каждый обязан быть в allowlist под своим именем):")
        for s in p.subjects:
            mark = "  " if s not in p.unlisted else "НЕ В СПИСКЕ"
            self.stdout.write(
                f"  {s.kind:14} {str(s.user.id):38} username={s.user.username!r:32} "
                f"role={s.user.role:11} listed_as={s.listed_as}  {mark}"
            )
        self.stdout.write("")
        self.stdout.write("связи на users.User (все, включая нулевые):")
        titles = {
            "cascade": "уходит",
            "set_null": "остаётся, указатель снимается",
            "dismantled": "разбирается по замыслу режима",
            "protect_empty": "PROTECT, 0 строк — не блокирует",
            "blocks": "БЛОКИРУЕТ",
        }
        for ln in p.lines:
            self.stdout.write(f"  {ln.on_delete:9} {ln.label:52} {ln.rows:6}  {titles[ln.disposition]}")
            for model_label, n in sorted(ln.removes.items()):
                if ln.disposition == "blocks":
                    self.stdout.write(f"            держит: {model_label} {n}")
                elif model_label != ln.label.rsplit(".", 1)[0]:
                    self.stdout.write(f"            + {model_label} {n}")

        self.stdout.write("")
        self.stdout.write("остаётся по замыслу (не FK, в проверку полноты не входит — причина рядом):")
        for k in p.kept:
            rows = "модели нет — исключение пока ничего не исключает" if k.rows is None else str(k.rows)
            self.stdout.write(f"  {k.label:62} {rows:>6}  {k.reason}")

        self.stdout.write("")
        if p.unlisted:
            self.stdout.write(
                "ОТКАЗ  не в ACCOUNT_RESET_ALLOWLIST — попадание в список отдельное действие, "
                "подтверждением не заменяется:"
            )
            for s in p.unlisted:
                self.stdout.write(f"       {s.kind:14} {s.listed_as}")
        if p.blockers:
            self.stdout.write(
                "ОТКАЗ  PROTECT-связи с данными, которые режим не разбирает — не обход, решение владельца:"
            )
            for ln in p.blockers:
                self.stdout.write(f"       {ln.label:52} {ln.rows}")
        if not p.unlisted and not p.blockers:
            self.stdout.write("блокеров нет")
