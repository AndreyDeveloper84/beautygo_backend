"""Tests for the seed_canonical_catalog management command."""
import json

import pytest
from django.core.management import call_command

from services.models import ServiceCategory, ServiceTemplate

SAMPLE = [
    {  # service directly under a root category (no subcategory)
        "code": "16.0.1", "category_no": 16, "category": "Солярий и загар",
        "subcategory_no": "", "subcategory": "",
        "service": "Вертикальный солярий", "note": "",
        "requires_health_check": "false", "health_check_reason": "",
    },
    {  # ordinary service under a subcategory
        "code": "1.1.1", "category_no": 1, "category": "Массаж тела",
        "subcategory_no": "1.1", "subcategory": "Базовый ручной массаж",
        "service": "Классический массаж всего тела", "note": "",
        "requires_health_check": "false", "health_check_reason": "",
    },
    {  # health-check-gated service with a contraindication note
        "code": "6.1.1", "category_no": 6, "category": "Инъекционная косметология",
        "subcategory_no": "6.1", "subcategory": "Ботулинотерапия",
        "service": "Ботулотоксин лба", "note": "только медицинский профиль",
        "requires_health_check": "true", "health_check_reason": "category-medical",
    },
]


@pytest.fixture
def seed_file(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8")
    return str(path)


@pytest.mark.django_db
def test_seed_creates_taxonomy_and_templates(seed_file):
    call_command("seed_canonical_catalog", "--file", seed_file)

    # 3 roots (16, 1, 6) + 2 subcategories (1.1, 6.1)
    assert ServiceCategory.objects.filter(parent__isnull=True).count() == 3
    assert ServiceCategory.objects.filter(parent__isnull=False).count() == 2
    assert ServiceTemplate.objects.count() == 3


@pytest.mark.django_db
def test_root_only_service_hangs_off_root(seed_file):
    call_command("seed_canonical_catalog", "--file", seed_file)
    tpl = ServiceTemplate.objects.get(name="Вертикальный солярий")
    assert tpl.category.parent_id is None
    assert tpl.category.name == "Солярий и загар"


@pytest.mark.django_db
def test_health_check_and_contraindications_seeded(seed_file):
    call_command("seed_canonical_catalog", "--file", seed_file)
    tpl = ServiceTemplate.objects.get(name="Ботулотоксин лба")
    assert tpl.requires_health_check is True
    assert tpl.contraindications == "только медицинский профиль"
    # durations left null for later curation
    assert tpl.duration_default is None
    assert tpl.duration_min is None
    assert tpl.duration_max is None


@pytest.mark.django_db
def test_categories_are_global_tenant_null(seed_file):
    call_command("seed_canonical_catalog", "--file", seed_file)
    assert ServiceCategory.objects.filter(tenant__isnull=False).count() == 0


@pytest.mark.django_db
def test_seed_is_idempotent(seed_file):
    call_command("seed_canonical_catalog", "--file", seed_file)
    call_command("seed_canonical_catalog", "--file", seed_file)
    assert ServiceCategory.objects.count() == 5
    assert ServiceTemplate.objects.count() == 3
