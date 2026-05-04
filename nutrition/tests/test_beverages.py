"""Tests for the Phase 3 beverage catalog (DRF-301).

Covers:
- Beverage model: __str__, slug uniqueness, alias lowercasing on save
- seed_beverages command: idempotency, --only-new, --dry-run
- GET /api/v1/nutrition/internal/beverages/: auth, response shape,
  Cache-Control header, only-active filter
"""
from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.db import IntegrityError
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.data.beverages_seed import BEVERAGES
from nutrition.models import Beverage
from users.models import User


pytestmark = pytest.mark.django_db


SERVICE_TOKEN = "test-token-DRF-301"
URL = "/api/v1/nutrition/internal/beverages/"


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def proxy_user(db):
    return User.objects.create(
        username="bot:301", role="client", is_proxy=True,
    )


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class TestBeverageModel:
    def test_str_includes_category(self):
        b = Beverage.objects.create(
            slug="t1", name_ru="Тест", category="water", water_coefficient=1.0,
        )
        assert "Тест" in str(b)
        assert "water" in str(b)

    def test_slug_unique(self):
        Beverage.objects.create(
            slug="dup", name_ru="A", category="water", water_coefficient=1.0,
        )
        with pytest.raises(IntegrityError):
            Beverage.objects.create(
                slug="dup", name_ru="B", category="water", water_coefficient=1.0,
            )

    def test_aliases_lowercased_on_save(self):
        b = Beverage.objects.create(
            slug="case", name_ru="X", category="water", water_coefficient=1.0,
            aliases=["FooBar", "  Кофе  ", "MIXED Case"],
        )
        b.refresh_from_db()
        assert b.aliases == ["foobar", "кофе", "mixed case"]

    def test_aliases_filtered_empties(self):
        b = Beverage.objects.create(
            slug="empties", name_ru="X", category="water", water_coefficient=1.0,
            aliases=["", "   ", "x"],
        )
        b.refresh_from_db()
        assert b.aliases == ["x"]


# ---------------------------------------------------------------------------
# seed_beverages command
# ---------------------------------------------------------------------------


class TestSeedBeveragesCommand:
    def test_first_run_creates_all_rows(self):
        out = StringIO()
        call_command("seed_beverages", stdout=out)
        assert Beverage.objects.count() == len(BEVERAGES)
        assert "created=" in out.getvalue()

    def test_second_run_is_idempotent(self):
        call_command("seed_beverages")
        first_count = Beverage.objects.count()
        call_command("seed_beverages")
        assert Beverage.objects.count() == first_count
        assert first_count == len(BEVERAGES)

    def test_only_new_skips_existing(self):
        Beverage.objects.create(
            slug="voda", name_ru="Override Name", category="water",
            water_coefficient=0.5,  # deliberately wrong — confirms skip
        )
        call_command("seed_beverages", "--only-new")
        b = Beverage.objects.get(slug="voda")
        # admin-edited row preserved
        assert b.name_ru == "Override Name"
        assert b.water_coefficient == 0.5
        # other rows still seeded
        assert Beverage.objects.count() == len(BEVERAGES)

    def test_dry_run_does_not_write(self):
        call_command("seed_beverages", "--dry-run")
        assert Beverage.objects.count() == 0

    def test_seed_covers_all_required_categories(self):
        call_command("seed_beverages")
        cats = set(Beverage.objects.values_list("category", flat=True))
        # Spec: water, tea, coffee, juice, soda, milk, alcohol, broth, sport
        required = {
            "water", "tea", "coffee", "juice", "soda",
            "milk", "alcohol", "broth", "sport",
        }
        missing = required - cats
        assert not missing, f"missing categories: {missing}"

    def test_seed_meets_minimum_size(self):
        call_command("seed_beverages")
        # Spec acceptance: ≥ 50 beverages.
        assert Beverage.objects.count() >= 50


# ---------------------------------------------------------------------------
# GET /internal/beverages/
# ---------------------------------------------------------------------------


class TestInternalBeveragesEndpoint:
    def test_missing_service_token_returns_401(self, proxy_user):
        # Seed something so 200-vs-empty is unambiguous if perms regress.
        call_command("seed_beverages")
        c = APIClient()
        resp = c.get(URL, HTTP_X_EXTERNAL_USER_ID="bot:301")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_invalid_external_id_returns_400(self):
        c = APIClient()
        resp = c.get(
            URL,
            HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
            HTTP_X_EXTERNAL_USER_ID="no-colon-here",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_returns_active_catalog(self, proxy_user):
        call_command("seed_beverages")
        c = APIClient()
        resp = c.get(
            URL,
            HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
            HTTP_X_EXTERNAL_USER_ID="bot:301",
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        assert "beverages" in body
        assert len(body["beverages"]) == Beverage.objects.filter(
            is_active=True,
        ).count()

    def test_response_shape_matches_spec(self, proxy_user):
        call_command("seed_beverages")
        c = APIClient()
        resp = c.get(
            URL,
            HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
            HTTP_X_EXTERNAL_USER_ID="bot:301",
        )
        first = resp.json()["data"]["beverages"][0]
        # Spec §2.5: only UI metadata exposed on the wire — no macros.
        assert set(first.keys()) == {
            "slug", "name_ru", "category", "aliases",
            "default_serving_ml", "default_serving_label",
        }

    def test_cache_control_header_present(self, proxy_user):
        call_command("seed_beverages")
        c = APIClient()
        resp = c.get(
            URL,
            HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
            HTTP_X_EXTERNAL_USER_ID="bot:301",
        )
        assert resp["Cache-Control"] == "max-age=3600"

    def test_inactive_rows_filtered(self, proxy_user):
        call_command("seed_beverages")
        Beverage.objects.filter(slug="vodka").update(is_active=False)
        c = APIClient()
        resp = c.get(
            URL,
            HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
            HTTP_X_EXTERNAL_USER_ID="bot:301",
        )
        slugs = {b["slug"] for b in resp.json()["data"]["beverages"]}
        assert "vodka" not in slugs

    def test_kofe_chernyi_has_expected_aliases(self, proxy_user):
        """Free-text parser smoke check — the bot relies on these aliases."""
        call_command("seed_beverages")
        c = APIClient()
        resp = c.get(
            URL,
            HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
            HTTP_X_EXTERNAL_USER_ID="bot:301",
        )
        kofe = next(
            b for b in resp.json()["data"]["beverages"]
            if b["slug"] == "kofe_chernyi"
        )
        # Lowercased on save; the spec's example aliases should be findable.
        for must_have in ("кофе", "coffee", "americano"):
            assert must_have in kofe["aliases"]
