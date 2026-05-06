"""DRF-259 — Track E category extensions.

Existing seed_service_templates seeds 8 root categories. Track E rules
(massage with argan oil, lymphatic drainage, brightening cosmetology,
deep hydration, spa) require child categories that cross-domain rule
engine maps deficit patterns to.

This test file covers:
- seed_track_e_categories management command — adds 6 child categories
  idempotently without disturbing existing data
- migrate_uncategorized_services — regex matching of free-text Service.name
  to ServiceCategory.slug for backfill of legacy services

Both commands must be idempotent: re-running them is a no-op when the
target state is already reached.
"""
from __future__ import annotations

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from services.models import Service, ServiceCategory


# Track E child slugs we expect to exist after seeding.
# Pinned here so the seed command can never silently drift away from
# what the cross-domain rule engine references.
TRACK_E_CHILD_SLUGS = {
    "massage-argan-oil",
    "massage-lymph-drainage",
    "cosmetology-brightening",
    "cosmetology-deep-hydration",
    "spa",
    "spa-relaxation",
}


@pytest.mark.django_db
class TestSeedTrackECategories:
    """`python manage.py seed_track_e_categories` idempotent seed."""

    def test_creates_six_track_e_categories(self):
        out = StringIO()
        call_command("seed_track_e_categories", stdout=out)

        present = set(
            ServiceCategory.objects.filter(slug__in=TRACK_E_CHILD_SLUGS)
            .values_list("slug", flat=True)
        )
        assert present == TRACK_E_CHILD_SLUGS, (
            f"Missing categories: {TRACK_E_CHILD_SLUGS - present}"
        )

    def test_idempotent_second_run_is_noop(self):
        # First run creates the rows.
        call_command("seed_track_e_categories", stdout=StringIO())
        first_count = ServiceCategory.objects.count()

        # Second run must not duplicate or replace.
        call_command("seed_track_e_categories", stdout=StringIO())
        second_count = ServiceCategory.objects.count()

        assert first_count == second_count

    def test_massage_children_link_to_existing_massage_parent(self):
        # Existing seed_service_templates creates "massage" root.
        call_command("seed_service_templates", stdout=StringIO())
        call_command("seed_track_e_categories", stdout=StringIO())

        massage = ServiceCategory.objects.get(slug="massage")
        argan = ServiceCategory.objects.get(slug="massage-argan-oil")
        lymph = ServiceCategory.objects.get(slug="massage-lymph-drainage")

        assert argan.parent_id == massage.pk
        assert lymph.parent_id == massage.pk

    def test_cosmetology_children_link_to_existing_cosmetology_parent(self):
        call_command("seed_service_templates", stdout=StringIO())
        call_command("seed_track_e_categories", stdout=StringIO())

        cosm = ServiceCategory.objects.get(slug="cosmetology")
        bright = ServiceCategory.objects.get(slug="cosmetology-brightening")
        hydra = ServiceCategory.objects.get(slug="cosmetology-deep-hydration")

        assert bright.parent_id == cosm.pk
        assert hydra.parent_id == cosm.pk

    def test_spa_root_created_when_absent(self):
        # spa is a new root — not in seed_service_templates.
        call_command("seed_track_e_categories", stdout=StringIO())

        spa = ServiceCategory.objects.get(slug="spa")
        assert spa.parent_id is None
        assert spa.is_active is True

    def test_spa_relaxation_links_to_spa(self):
        call_command("seed_track_e_categories", stdout=StringIO())

        spa = ServiceCategory.objects.get(slug="spa")
        relax = ServiceCategory.objects.get(slug="spa-relaxation")
        assert relax.parent_id == spa.pk

    def test_does_not_touch_unrelated_categories(self):
        # Unrelated row created before seed must remain unchanged.
        unrelated = ServiceCategory.objects.create(
            name="Кастомная категория", slug="custom-test", sort_order=999,
        )
        call_command("seed_track_e_categories", stdout=StringIO())
        unrelated.refresh_from_db()
        assert unrelated.name == "Кастомная категория"
        assert unrelated.sort_order == 999

    def test_runs_without_existing_parents(self):
        # If massage / cosmetology parents are missing, command must
        # create them on-the-fly (defensive seeding) rather than fail.
        # This means seed can run on a fresh DB without depending on
        # seed_service_templates having run first.
        ServiceCategory.objects.all().delete()
        call_command("seed_track_e_categories", stdout=StringIO())

        assert ServiceCategory.objects.filter(slug="massage").exists()
        assert ServiceCategory.objects.filter(slug="cosmetology").exists()
        assert ServiceCategory.objects.filter(
            slug="massage-argan-oil"
        ).exists()


@pytest.mark.django_db
class TestMigrateUncategorizedServices:
    """`python manage.py migrate_uncategorized_services` regex backfill.

    Existing Service rows with category=None — match Service.name against
    a regex map and assign the closest ServiceCategory. Unmatched names are
    listed in stdout for human cleanup via Django admin.

    Match priority: more specific patterns win (anti-cellulite massage
    before generic "массаж"). Specificity is encoded by ordering in the
    pattern list.
    """

    def _make_service(self, specialist_user, name: str, category=None):
        return Service.objects.create(
            specialist=specialist_user.specialist_profile,
            name=name, price=Decimal("1500"), duration_minutes=60,
            category=category,
        )

    def test_assigns_argan_massage_to_track_e_subcategory(
        self, specialist_user,
    ):
        call_command("seed_track_e_categories", stdout=StringIO())
        svc = self._make_service(
            specialist_user, "Массаж тела с аргановым маслом",
        )

        call_command("migrate_uncategorized_services", stdout=StringIO())

        svc.refresh_from_db()
        assert svc.category is not None
        assert svc.category.slug == "massage-argan-oil"

    def test_assigns_lymph_drainage(self, specialist_user):
        call_command("seed_track_e_categories", stdout=StringIO())
        svc = self._make_service(
            specialist_user, "Лимфодренажный массаж 60 мин",
        )

        call_command("migrate_uncategorized_services", stdout=StringIO())

        svc.refresh_from_db()
        assert svc.category.slug == "massage-lymph-drainage"

    def test_assigns_generic_massage_when_no_specific_match(
        self, specialist_user,
    ):
        # Generic "Массаж спины" has no Track-E child — should fall back
        # to the "massage" root.
        call_command("seed_service_templates", stdout=StringIO())
        call_command("seed_track_e_categories", stdout=StringIO())
        svc = self._make_service(specialist_user, "Массаж спины классический")

        call_command("migrate_uncategorized_services", stdout=StringIO())

        svc.refresh_from_db()
        assert svc.category.slug == "massage"

    def test_assigns_brightening_to_cosmetology_brightening(
        self, specialist_user,
    ):
        call_command("seed_track_e_categories", stdout=StringIO())
        svc = self._make_service(
            specialist_user, "Осветление кожи лица витамин С",
        )

        call_command("migrate_uncategorized_services", stdout=StringIO())

        svc.refresh_from_db()
        assert svc.category.slug == "cosmetology-brightening"

    def test_assigns_deep_hydration(self, specialist_user):
        call_command("seed_track_e_categories", stdout=StringIO())
        svc = self._make_service(
            specialist_user, "Глубокое увлажнение кожи",
        )

        call_command("migrate_uncategorized_services", stdout=StringIO())

        svc.refresh_from_db()
        assert svc.category.slug == "cosmetology-deep-hydration"

    def test_unmatched_service_remains_uncategorized(self, specialist_user):
        call_command("seed_track_e_categories", stdout=StringIO())
        svc = self._make_service(specialist_user, "Что-то совсем другое xyz")

        out = StringIO()
        call_command("migrate_uncategorized_services", stdout=out)

        svc.refresh_from_db()
        assert svc.category is None
        # Command reports unmatched names so a human can curate them.
        assert "Что-то совсем другое xyz" in out.getvalue()

    def test_does_not_overwrite_already_categorized(self, specialist_user):
        # If the service already has a category, migration must leave it.
        call_command("seed_service_templates", stdout=StringIO())
        existing = ServiceCategory.objects.get(slug="manicure")
        svc = self._make_service(
            specialist_user, "Массаж аргановым маслом", category=existing,
        )

        call_command("seed_track_e_categories", stdout=StringIO())
        call_command("migrate_uncategorized_services", stdout=StringIO())

        svc.refresh_from_db()
        assert svc.category_id == existing.pk

    def test_idempotent_second_run(self, specialist_user):
        call_command("seed_track_e_categories", stdout=StringIO())
        self._make_service(
            specialist_user, "Массаж с аргановым маслом для лица",
        )

        call_command("migrate_uncategorized_services", stdout=StringIO())
        call_command("migrate_uncategorized_services", stdout=StringIO())

        # Second run is no-op — categorized rows are skipped.
        svc = Service.objects.get()
        assert svc.category.slug == "massage-argan-oil"

    def test_dry_run_flag_does_not_mutate(self, specialist_user):
        call_command("seed_track_e_categories", stdout=StringIO())
        svc = self._make_service(
            specialist_user, "Массаж с аргановым маслом",
        )

        out = StringIO()
        call_command(
            "migrate_uncategorized_services", "--dry-run", stdout=out,
        )

        svc.refresh_from_db()
        assert svc.category is None
        assert "Массаж с аргановым маслом" in out.getvalue()
