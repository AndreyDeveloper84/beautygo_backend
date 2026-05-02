"""Tenant model tests (DRF-242.1)."""
from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from tenants.models import Tenant


pytestmark = pytest.mark.django_db


class TestTenantBasics:
    def test_default_pk_is_uuid(self):
        t = Tenant.objects.create(slug="formula", name="Формула тела")
        assert t.id is not None
        assert str(t.id).count("-") == 4  # UUID4 shape

    def test_str_repr_includes_name_and_slug(self):
        t = Tenant.objects.create(slug="ayla", name="Ayla Marketplace")
        assert "Ayla Marketplace" in str(t)
        assert "ayla" in str(t)

    def test_timestamps_set_on_create(self):
        t = Tenant.objects.create(slug="x", name="x")
        assert t.created_at is not None
        assert t.updated_at is not None


class TestSlugUniqueness:
    def test_duplicate_slug_rejected_at_db_level(self):
        Tenant.objects.create(slug="dup", name="First")
        # IntegrityError must be raised — wrap in atomic so the test DB
        # transaction stays usable for any follow-up assertions.
        with transaction.atomic(), pytest.raises(IntegrityError):
            Tenant.objects.create(slug="dup", name="Second")


class TestSlugValidation:
    def test_uppercase_rejected(self):
        t = Tenant(slug="UPPERCASE", name="X")
        with pytest.raises(ValidationError) as exc:
            t.full_clean()
        assert "slug" in exc.value.message_dict

    def test_starts_with_hyphen_rejected(self):
        t = Tenant(slug="-foo", name="X")
        with pytest.raises(ValidationError):
            t.full_clean()

    def test_too_short_rejected(self):
        t = Tenant(slug="a", name="X")
        with pytest.raises(ValidationError):
            t.full_clean()

    def test_lowercase_with_hyphens_accepted(self):
        t = Tenant(slug="formula-tela_v2", name="X")
        t.full_clean()  # no exception


class TestActiveManager:
    def test_default_manager_hides_inactive(self):
        Tenant.objects.create(slug="alive", name="A", is_active=True)
        Tenant.objects.create(slug="dead", name="D", is_active=False)
        slugs = list(Tenant.objects.values_list("slug", flat=True))
        assert "alive" in slugs
        assert "dead" not in slugs

    def test_all_objects_returns_inactive_too(self):
        Tenant.objects.create(slug="alive2", name="A", is_active=True)
        Tenant.objects.create(slug="dead2", name="D", is_active=False)
        slugs = set(Tenant.all_objects.values_list("slug", flat=True))
        assert {"alive2", "dead2"}.issubset(slugs)

    def test_inactive_filterable_via_all_objects(self):
        Tenant.objects.create(slug="solo", name="S", is_active=False)
        assert Tenant.all_objects.filter(slug="solo").exists()
        assert not Tenant.objects.filter(slug="solo").exists()
