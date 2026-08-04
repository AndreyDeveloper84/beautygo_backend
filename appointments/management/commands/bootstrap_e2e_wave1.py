"""Create the deterministic backend half of the Wave 1 E2E fixture."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from appointments.models import Appointment, SpecialistTimeOff, SpecialistWorkingHours
from services.models import Service, ServiceCategory, SalonService, SpecialistService
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User


IDS = {
    "tenant": UUID("10000000-0000-4000-8000-000000000001"),
    "customer": UUID("10000000-0000-4000-8000-000000000002"),
    "specialist_user": UUID("10000000-0000-4000-8000-000000000003"),
    "specialist": UUID("10000000-0000-4000-8000-000000000004"),
    "category": UUID("10000000-0000-4000-8000-000000000005"),
    "service": UUID("10000000-0000-4000-8000-000000000006"),
    "offering": UUID("10000000-0000-4000-8000-000000000007"),
    "assignment": UUID("10000000-0000-4000-8000-000000000008"),
    "happy": UUID("10000000-0000-4000-8000-000000000011"),
    "disambiguation": UUID("10000000-0000-4000-8000-000000000012"),
    "terminal": UUID("10000000-0000-4000-8000-000000000013"),
    "conflicting": UUID("10000000-0000-4000-8000-000000000014"),
}

MOSCOW = ZoneInfo("Europe/Moscow")


def _parse_anchor(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CommandError("--anchor must be ISO-8601, for example 2026-08-05T12:00:00+03:00") from exc
    if parsed.tzinfo is None:
        raise CommandError("--anchor must include a UTC offset")
    return parsed.astimezone(MOSCOW).replace(second=0, microsecond=0)


class Command(BaseCommand):
    help = "Create/reset the deterministic e2e-wave1 tenant fixture and emit a JSON manifest."

    def add_arguments(self, parser):
        parser.add_argument("--anchor", required=True, help="A1 start in ISO-8601 with offset (normally T+48h).")
        parser.add_argument("--output", help="Optional path for the backend JSON manifest.")
        parser.add_argument(
            "--bind-external",
            metavar="EXTERNAL_USER_ID",
            help=(
                "Optionally bind a bot external identity (e.g. bot:max:e2e-wave1) to the fixture "
                "customer via the E2E-BOT-02B linked_user mechanism. When omitted the fixture "
                "stays UNBOUND — the regression path (controlled empty records result) is what "
                "the harness exercises until it performs the bind step."
            ),
        )

    @transaction.atomic
    def handle(self, *args, **options):
        anchor = _parse_anchor(options["anchor"])
        duration = timedelta(hours=1)

        tenant, _ = Tenant.all_objects.update_or_create(
            id=IDS["tenant"], defaults={"slug": "e2e-wave1", "name": "E2E Wave 1", "is_active": True}
        )
        customer, _ = User.objects.update_or_create(
            id=IDS["customer"],
            defaults={
                "username": "e2e-wave1-customer", "role": "client", "phone": "+79990001001",
                "tenant": tenant, "is_active": True, "is_verified": True, "deleted_at": None,
            },
        )
        specialist_user, _ = User.objects.update_or_create(
            id=IDS["specialist_user"],
            defaults={
                "username": "e2e-wave1-specialist", "role": "specialist", "phone": "+79990001002",
                "tenant": tenant, "is_active": True, "is_verified": True, "deleted_at": None,
            },
        )
        for user, role in (
            (customer, TenantUserRelationship.Role.CUSTOMER),
            (specialist_user, TenantUserRelationship.Role.STAFF),
        ):
            TenantUserRelationship.objects.update_or_create(
                user=user, tenant=tenant, is_active=True,
                defaults={"role": role, "granted_by": TenantUserRelationship.GrantedBy.SYSTEM, "revoked_at": None},
            )

        # A post-save signal creates a draft profile with a random UUID for
        # every new specialist User. Replace only this fixture user's empty
        # auto-row so the cross-run specialist id remains deterministic.
        SpecialistProfile.objects.filter(user=specialist_user).exclude(id=IDS["specialist"]).delete()
        specialist, _ = SpecialistProfile.objects.update_or_create(
            id=IDS["specialist"],
            defaults={
                "user": specialist_user, "tenant": tenant, "display_name": "E2E Master",
                "timezone": "Europe/Moscow", "status": SpecialistProfile.ProfileStatus.ACTIVE,
                "is_booking_enabled": True, "is_available": True,
                "booking_source": SpecialistProfile.BookingSource.AYLA_LOCAL,
            },
        )
        category, _ = ServiceCategory.objects.update_or_create(
            id=IDS["category"],
            defaults={"tenant": tenant, "name": "E2E Wave 1 Service", "slug": "e2e-wave1-service", "is_active": True},
        )
        service, _ = Service.objects.update_or_create(
            id=IDS["service"],
            defaults={
                "specialist": specialist, "tenant": tenant, "category": category, "name": "E2E Massage",
                "price": Decimal("2500.00"), "duration_minutes": 60, "is_active": True,
            },
        )
        offering, _ = SalonService.objects.update_or_create(
            id=IDS["offering"],
            defaults={
                "tenant": tenant, "category": category, "template": None, "name": "E2E Massage Offering",
                "duration_minutes": 60, "base_price": Decimal("2500.00"), "is_active": True,
                "source": SalonService.Source.SEED,
            },
        )
        assignment, _ = SpecialistService.objects.update_or_create(
            id=IDS["assignment"],
            defaults={
                "tenant": tenant, "salon_service": offering, "specialist": specialist,
                "duration_minutes": 60, "price": Decimal("2500.00"), "is_active": True,
            },
        )
        for weekday in range(7):
            SpecialistWorkingHours.objects.update_or_create(
                specialist=specialist, day_of_week=weekday,
                defaults={
                    "is_working_day": True, "start_time": time(0, 0), "end_time": time(23, 59),
                    "break_start": None, "break_end": None,
                },
            )
        SpecialistTimeOff.objects.filter(
            specialist=specialist, start_at__lt=anchor + timedelta(days=6), end_at__gt=anchor - timedelta(days=1)
        ).delete()

        appointment_specs = {
            "happy_path": (IDS["happy"], anchor, Appointment.Status.CONFIRMED),
            "disambiguation": (IDS["disambiguation"], anchor + timedelta(hours=24), Appointment.Status.CONFIRMED),
            "terminal": (IDS["terminal"], anchor - timedelta(hours=24), Appointment.Status.CANCELLED),
            "conflicting": (IDS["conflicting"], anchor + timedelta(hours=48), Appointment.Status.CONFIRMED),
        }
        appointments = {}
        for key, (appointment_id, starts_at, status) in appointment_specs.items():
            appointment, _ = Appointment.objects.update_or_create(
                id=appointment_id,
                defaults={
                    "client": customer, "specialist": specialist, "tenant": tenant, "service": service,
                    "salon_service": None, "start_datetime": starts_at, "end_datetime": starts_at + duration,
                    "status": status, "version": 1, "price": Decimal("2500.00"),
                    "snapshot_service_name": service.name, "snapshot_duration_minutes": 60,
                    "snapshot_price": Decimal("2500.00"), "snapshot_timezone": "Europe/Moscow",
                    "idempotency_key": f"e2e-wave1-{key}",
                    "cancellation_reason": "E2E terminal fixture" if key == "terminal" else "",
                },
            )
            appointments[key] = {
                "appointment_id": str(appointment.id), "version": appointment.version,
                "status": appointment.status, "starts_at": appointment.start_datetime.isoformat(),
                "ends_at": appointment.end_datetime.isoformat(),
            }

        manifest = {
            "schema_version": 1, "fixture": "e2e-wave1", "anchor": anchor.isoformat(),
            "tenant_id": str(tenant.id), "tenant_code": tenant.slug, "timezone": "Europe/Moscow",
            "booking_via_ayla_rest": True, "backend_customer_id": str(customer.id),
            "specialist_id": str(specialist.id), "service_id": str(service.id),
            "offering_id": str(offering.id), "assignment_id": str(assignment.id),
            "appointments": appointments,
            "candidate_slots": {
                "available": (anchor + timedelta(hours=72)).isoformat(),
                "taken": appointments["conflicting"]["starts_at"],
            },
        }
        external_user_id = options.get("bind_external")
        if external_user_id:
            # E2E-BOT-02B: deterministic identity binding for the harness.
            # Goes through the same service the bind endpoint uses, so the
            # fixture can never drift from the runtime contract.
            from users.services import bind_external_identity
            proxy, _ = bind_external_identity(
                external_user_id, customer.id,
                initiator="e2e_fixture_bootstrap",
            )
            manifest["external_user_id"] = external_user_id
            manifest["proxy_user_id"] = str(proxy.id)
        payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
        if options.get("output"):
            Path(options["output"]).write_text(payload + "\n", encoding="utf-8")
        self.stdout.write(payload)
