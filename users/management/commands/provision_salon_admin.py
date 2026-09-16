"""Give a salon an administrator (DRF-1062).

Without this, everything else in DRF-1062 is unreachable. ``IsTenantAdmin``
needs an active ``TenantUserRelationship`` with role ``admin`` for the
addressed tenant, and the pilot salon has none — the audit of 2026-08-14
found zero administrators for it. Shipping the admin surface without a way
to create that row would repeat the mistake that left the bot's Mini App
built, deployed and unusable behind an empty ``TenantStaff`` table.

Idempotent by construction: re-running reuses the user and the grant. The
phone number is never a literal in this repository — it is personal data and
belongs to the operator, not to git.

**It is not an argument either (DRF-2024).** ``--phone`` put the number into
four places at once: the process list on the host, the operator's shell
history, the host audit log, and — because sentry-sdk's ``ArgvIntegration``
ships in the default integrations — ``extra["sys.argv"]`` of every Sentry
event. Only the last of the four is reachable by an application-side
scrubber; the other three live outside the process. So the number arrives on
**stdin**, and the guide's own invocation form already keeps stdin open
(``docker exec -i``), so the operator's move stays a one-liner.

The optional display name follows on the second line for the same reason: it
is a person's name. ``--tenant`` and ``--dry-run`` stay arguments — a salon
slug is not personal data.

Deliberately refuses to act when a *revoked* relationship exists: someone
removed that person's access on purpose, and silently restoring it from a
provisioning script is not a decision a script should make.

    printf '+7 9xx xxx-xx-xx\\n' | docker exec -i dev-web-1 \\
        python manage.py provision_salon_admin --tenant formula-tela

Second line, optional — the display name used only when the account is
created::

    printf '+7 9xx xxx-xx-xx\\nИмя Владельца\\n' | docker exec -i dev-web-1 \\
        python manage.py provision_salon_admin --tenant formula-tela
"""
from __future__ import annotations

import sys

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tenants.models import Tenant
from users.models import TenantUserRelationship, User


class Command(BaseCommand):
    help = "Create or reuse a salon administrator and grant them tenant admin."

    #: ``stdin`` is deliberately NOT a parser option — it must never be able
    #: to appear in a command line. Django's stealth options are the supported
    #: way to hand a stream to ``call_command``, which is what the tests do.
    stealth_options = ("stdin",)

    #: Флаги, снятые в DRF-2024. Оператор с прежней командой в буфере обмена
    #: получил бы от argparse «unrecognized arguments: --phone» — из этого не
    #: следует, что делать, и человек повторяет ход, а каждый повтор снова
    #: кладёт номер в `ps`, историю, аудит и `extra["sys.argv"]` Sentry.
    REMOVED_PERSONAL_FLAGS = ("--phone", "--name")

    def run_from_argv(self, argv) -> None:
        """Отказать понятно, если персональные данные всё же пришли в argv.

        Экспозицию это НЕ уменьшает: к этому моменту значение уже в списке
        процессов и в истории оболочки — утечка произошла при exec, до Python.
        Смысл в другом: назвать верную форму и сказать, что номер засвечен,
        чтобы не было второго и третьего захода.

        Сравниваются только ИМЕНА флагов; значение не читается и не печатается.
        Флаг не возвращается ни в парсер, ни в ``--help`` — иначе он снова
        начал бы ПРИНИМАТЬ значение.
        """
        offending = [
            flag
            for flag in self.REMOVED_PERSONAL_FLAGS
            if flag in argv or any(str(arg).startswith(f"{flag}=") for arg in argv)
        ]
        if offending:
            raise CommandError(
                f"{', '.join(offending)} is no longer an argument (DRF-2024): "
                "personal data is read from stdin, not from the command line. "
                "Retry as: printf '+7 9xx xxx-xx-xx\\n' | docker exec -i "
                "dev-web-1 python manage.py provision_salon_admin "
                "--tenant <slug>. NOTE: the value you just passed is already "
                "in the host process list, your shell history, the host audit "
                "log and the Sentry event's sys.argv — treat it as exposed."
            )
        super().run_from_argv(argv)

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--tenant", required=True,
            help="Tenant slug, e.g. formula-tela.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change and exit without writing.",
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        stream = options.get("stdin") or sys.stdin
        phone = (stream.readline() or "").strip()
        if not phone:
            raise CommandError(
                "stdin gave no number — the phone goes on the FIRST line of "
                "stdin, not into an argument (DRF-2024). Example: "
                "printf '+7 9xx xxx-xx-xx\\n' | docker exec -i dev-web-1 "
                "python manage.py provision_salon_admin --tenant <slug>"
            )
        # The second line is optional and is used only when the account is
        # created. Read it unconditionally: a two-line input must not leave a
        # dangling tail in the caller's stream.
        display_name = (stream.readline() or "").strip()
        slug = options["tenant"].strip()
        dry_run = options["dry_run"]

        try:
            tenant = Tenant.objects.get(slug=slug)
        except Tenant.DoesNotExist:
            raise CommandError(f"Tenant '{slug}' does not exist.")

        user = User.objects.filter(phone=phone).first()
        created_user = user is None

        if created_user:
            if dry_run:
                self.stdout.write(f"would create user for phone in tenant {slug}")
            else:
                user = User.objects.create_user(
                    username=phone,
                    phone=phone,
                    role="admin",
                    first_name=display_name,
                    is_verified=True,
                )
        else:
            # An existing account keeps its role: a client who also
            # administers a salon is a normal situation, and the grant
            # below is what actually authorises them.
            self.stdout.write(f"reusing existing account (id={user.pk})")

        if dry_run and created_user:
            self.stdout.write(self.style.WARNING("dry-run: nothing written"))
            return

        revoked = TenantUserRelationship.objects.filter(
            user=user, tenant=tenant, is_active=False,
        ).exists()
        active = TenantUserRelationship.objects.filter(
            user=user, tenant=tenant, is_active=True,
        ).first()

        if revoked and not active:
            raise CommandError(
                "This user's access to the tenant was revoked. Restoring it "
                "is a deliberate decision — grant it explicitly instead of "
                "through provisioning."
            )

        if active and active.role == TenantUserRelationship.Role.ADMIN:
            self.stdout.write(self.style.SUCCESS(
                f"already an administrator of {slug} — nothing to do"
            ))
            return

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"dry-run: would grant admin on {slug}"
            ))
            return

        if active:
            # Promote in place: the partial unique constraint allows only
            # one active relationship per (user, tenant), so a second row
            # would fail rather than upgrade them.
            active.role = TenantUserRelationship.Role.ADMIN
            active.save(update_fields=["role"])
            self.stdout.write(self.style.SUCCESS(
                f"promoted existing relationship to admin on {slug}"
            ))
        else:
            TenantUserRelationship.objects.create(
                user=user,
                tenant=tenant,
                role=TenantUserRelationship.Role.ADMIN,
                is_active=True,
                granted_by=TenantUserRelationship.GrantedBy.ADMIN,
            )
            self.stdout.write(self.style.SUCCESS(
                f"granted admin on {slug}"
            ))

        self.stdout.write(
            "Login route: OTP to the provided phone. Send X-Tenant: "
            f"{tenant.id} (or rely on the JWT tenant claim) with "
            "X-App-Type: pro when calling the salon-admin endpoints."
        )
