"""Seed Track E cross-domain rules (DRF-268).

Idempotent. Reads ``nutrition.data.cross_domain_rules_seed.TRACK_E_RULES``
and creates one ``CrossDomainRule`` per dict via ``get_or_create`` keyed
on ``rule_id``. Existing rows are NEVER updated — a curator may have
edited the insight text or flipped is_active=True; the seed must not
clobber those changes on a re-run.

Activation gates ship inert: every new row has
``legal_reviewed=False`` and ``is_active=False``. The PO must:

1. Get formulation reviewed by counsel (Закон «О рекламе» ст. 24)
2. Run audit_cross_domain_supply to confirm ≥3 specialists per category
3. Flip both flags via Django admin

Usage:
    python manage.py seed_cross_domain_rules
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from nutrition.data.cross_domain_rules_seed import TRACK_E_RULES
from nutrition.models import CrossDomainRule


class Command(BaseCommand):
    help = "Seed Track E cross-domain rules (idempotent, additive only)."

    @transaction.atomic
    def handle(self, *_args, **_options):
        created_count = 0
        kept_count = 0

        for spec in TRACK_E_RULES:
            # get_or_create — defaults apply only on creation. A
            # curator's hand-tuned insight_text or flipped is_active
            # survives a re-run.
            _obj, created = CrossDomainRule.objects.get_or_create(
                rule_id=spec["rule_id"],
                defaults={
                    "nutrition_trigger": spec["nutrition_trigger"],
                    "service_category_slug": spec["service_category_slug"],
                    "service_modifier": spec.get("service_modifier", ""),
                    "insight_text_template": spec["insight_text_template"],
                    "rationale_text": spec["rationale_text"],
                    "disclaimer_text": spec["disclaimer_text"],
                    "min_data_points": spec.get("min_data_points", 3),
                    "excluded_health_flags": spec.get(
                        "excluded_health_flags", [],
                    ),
                    # Activation gates — both default False. Curator
                    # flips after legal review + supply audit.
                    "legal_reviewed": False,
                    "is_active": False,
                },
            )
            if created:
                created_count += 1
                self.stdout.write(self.style.SUCCESS(
                    f"created  {spec['rule_id']}",
                ))
            else:
                kept_count += 1
                self.stdout.write(self.style.NOTICE(
                    f"kept     {spec['rule_id']}",
                ))

        self.stdout.write(self.style.SUCCESS(
            f"\ndone · {created_count} created, "
            f"{kept_count} kept · {len(TRACK_E_RULES)} total in seed",
        ))
        self.stdout.write(self.style.WARNING(
            "All seeded rules ship is_active=False AND legal_reviewed=False. "
            "Run audit_cross_domain_supply, then flip via Django admin.",
        ))
