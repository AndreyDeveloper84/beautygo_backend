"""Audit Track E cross-domain supply (DRF-268).

Read-only command. For each ``CrossDomainRule`` (active or not), counts
specialists offering the rule's ``service_category_slug``. Surfaces
rules that would silently fail the engine's supply check at evaluate
time.

The threshold is 3 active specialists in the pilot city. Below that,
the engine has nowhere to route users and the rule should not be
activated. The PO uses this output before flipping ``is_active=True``.

Usage:
    python manage.py audit_cross_domain_supply
    python manage.py audit_cross_domain_supply --threshold 5
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from nutrition.models import CrossDomainRule
from services.models import Service


SUPPLY_THRESHOLD = 3


class Command(BaseCommand):
    help = "Audit specialist supply for each cross-domain rule."

    def add_arguments(self, parser):
        parser.add_argument(
            "--threshold", type=int, default=SUPPLY_THRESHOLD,
            help="Minimum specialist count for a rule to pass audit.",
        )

    def handle(self, *_args, **options):
        threshold = options["threshold"]
        rules = CrossDomainRule.objects.all().order_by("rule_id")
        if not rules:
            self.stdout.write(self.style.WARNING(
                "No cross-domain rules in DB — run seed_cross_domain_rules first.",
            ))
            return

        warnings = 0
        for rule in rules:
            count = Service.objects.filter(
                category__slug=rule.service_category_slug,
                is_active=True,
            ).count()
            status_label = "active " if rule.is_active else "inactive"
            if count >= threshold:
                self.stdout.write(self.style.SUCCESS(
                    f"OK       {rule.rule_id} → "
                    f"{rule.service_category_slug} "
                    f"({count} specialists, {status_label})",
                ))
            else:
                warnings += 1
                self.stdout.write(self.style.WARNING(
                    f"WARNING  {rule.rule_id} → "
                    f"{rule.service_category_slug} "
                    f"(<{threshold}: only {count} specialists, "
                    f"{status_label}) — недостаточно поставщиков",
                ))

        self.stdout.write(self.style.SUCCESS(
            f"\ndone · {len(rules)} rules audited, {warnings} below threshold",
        ))
