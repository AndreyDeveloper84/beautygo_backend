"""DRF-2556 — буст избранного мастера ×1.5 применяется, а ошибка кода не глушится.

``CrossDomainEngine._score`` фильтровал ``FavoriteSpecialist.objects.filter(client=user)``;
у модели поля ``client`` нет (живой ``_meta``: ``id, user, specialist,
created_at``), каждый вызов поднимал ``FieldError``, и ``except Exception: pass``
его глушил — буст не применялся ни разу, оценка всегда 1.0. Узлы:

* у человека есть избранный мастер с активной услугой в категории правила →
  оценка = базовая × ``FAVORITE_SPECIALIST_BOOST``;
* положительная пара: избранного нет → базовая 1.0 (иначе «буст есть» проходил
  бы и у движка, который бустит всех);
* сбой базы → базовая оценка и строка в логе, подбор не падает;
* ошибка кода (``FieldError``) больше не глушится — она падает, а не тонет.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import FieldError
from django.db import DatabaseError

from ai.tests.factories import make_specialist
from nutrition.models import CrossDomainRule
from nutrition.services.cross_domain_engine import (
    FAVORITE_SPECIALIST_BOOST,
    CrossDomainEngine,
)
from services.models import Service, ServiceCategory
from users.models import FavoriteSpecialist

User = get_user_model()
CATEGORY_SLUG = "massage-argan-oil-2556"

pytestmark = pytest.mark.django_db


@pytest.fixture
def client_person():
    return User.objects.create_user(
        username="boost_2556", password="x", role="client", phone="+79990825561"
    )


@pytest.fixture
def rule():
    return CrossDomainRule.objects.create(
        rule_id="vitamin_d_to_argan_2556",
        nutrition_trigger="low_vitamin_d",
        service_category_slug=CATEGORY_SLUG,
        insight_text_template="text",
        rationale_text="rationale",
        disclaimer_text="not medical advice",
        is_active=True,
        legal_reviewed=True,
    )


@pytest.fixture
def master_in_category():
    specialist = make_specialist()
    category = ServiceCategory.objects.create(name="Аргановый массаж 2556", slug=CATEGORY_SLUG)
    Service.objects.create(
        specialist=specialist,
        name="Массаж с аргановым маслом",
        price=Decimal("2500"),
        duration_minutes=60,
        is_active=True,
        category=category,
    )
    return specialist


class TestFavoriteBoost:
    def test_favourite_master_in_category_boosts_the_score(
        self, client_person, rule, master_in_category
    ) -> None:
        FavoriteSpecialist.objects.create(user=client_person, specialist=master_in_category)

        score = CrossDomainEngine()._score(rule, client_person)

        assert score == pytest.approx(1.0 * FAVORITE_SPECIALIST_BOOST)
        assert score > 1.0

    def test_no_favourite_keeps_the_base_score(
        self, client_person, rule, master_in_category
    ) -> None:
        # Мастер и услуга есть, но в избранном их нет — буста быть не должно.
        assert CrossDomainEngine()._score(rule, client_person) == pytest.approx(1.0)

    def test_favourite_outside_the_category_does_not_boost(
        self, client_person, rule, master_in_category
    ) -> None:
        other = make_specialist(display_name="Другой мастер")
        FavoriteSpecialist.objects.create(user=client_person, specialist=other)

        assert CrossDomainEngine()._score(rule, client_person) == pytest.approx(1.0)


class TestWhatTheGuardCatches:
    def test_database_outage_degrades_to_base_score_loudly(
        self, client_person, rule, master_in_category, caplog
    ) -> None:
        FavoriteSpecialist.objects.create(user=client_person, specialist=master_in_category)
        with (
            patch.object(
                FavoriteSpecialist.objects, "filter", side_effect=DatabaseError("db gone")
            ),
            caplog.at_level(logging.WARNING, logger="nutrition.services.cross_domain_engine"),
        ):
            score = CrossDomainEngine()._score(rule, client_person)

        assert score == pytest.approx(1.0)
        assert "favorite_boost_unavailable" in caplog.text

    def test_a_code_error_is_not_swallowed(self, client_person, rule) -> None:
        # То, что прежний ``except Exception: pass`` превращал в «избранных нет».
        with (
            patch.object(
                FavoriteSpecialist.objects,
                "filter",
                side_effect=FieldError("Cannot resolve keyword 'client' into field"),
            ),
            pytest.raises(FieldError),
        ):
            CrossDomainEngine()._score(rule, client_person)
