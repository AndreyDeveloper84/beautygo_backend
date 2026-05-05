"""DRF-262 — five micronutrient pattern rules.

Track E pattern engine extension. Adds five detectors for chronic
micronutrient deficits the cross-domain rule engine reads to drive
beauty-service recommendations.

Detectors (all aggregate over rolling windows ending today, UTC):

| slug              | rule                                  | window | severity                           |
|-------------------|---------------------------------------|--------|------------------------------------|
| low_vitamin_d     | <30% RDA in ≥5 of 7 days              | 7d     | medium                             |
| low_iron          | <50% RDA in ≥7 of 14 days             | 14d    | medium                             |
| low_omega3        | <50% RDA in ≥14 of 21 days            | 21d    | low                                |
| low_calcium       | <50% RDA in ≥14 of 21 days            | 21d    | low                                |
| low_b12           | <30% RDA in ≥7 of 14 days             | 14d    | high if vegan/vegetarian, else med |

Quality gate: each detector skips a window where >40% of FoodLog rows
came from ``micronutrients_source="ai_estimate"``. AI hallucinations
become bogus deficit signals → bogus cross-domain recommendations →
trust loss. Provenance is the firewall.

Eating-disorder mode: all five micronutrient patterns are suppressed.
We never surface "you're deficient" framing to ED users.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model

from nutrition.models import FoodLog, NutritionProfile
from nutrition.services.pattern_detection_service import detect_patterns


User = get_user_model()


@pytest.fixture
def user_with_profile(db, settings):
    # RDA targets come from settings until DRF-265 lands the profile-side
    # daily_*_norm fields. Pin the defaults the tests depend on.
    settings.NUTRITION_DEFAULT_VITAMIN_D_IU = 600
    settings.NUTRITION_DEFAULT_IRON_MG = 18
    settings.NUTRITION_DEFAULT_OMEGA3_G = 1.1
    settings.NUTRITION_DEFAULT_CALCIUM_MG = 1000
    settings.NUTRITION_DEFAULT_B12_MCG = 2.4

    user = User.objects.create_user(
        username="micro_pattern_user", password="x",
        role="client", phone="+79990600001",
    )
    NutritionProfile.objects.create(
        user=user, gender="female", age=40, height_cm=165,
        weight_kg=70.0, goal="maintain", pace="moderate",
        daily_kcal=2000, daily_protein_g=100, daily_water_ml=2000,
    )
    return user


def _seed_log(
    user, *, days_ago: int, hours_ago: int = 12,
    iron_mg: float | None = None, vitamin_d_iu: float | None = None,
    omega3_g: float | None = None, calcium_mg: float | None = None,
    vitamin_b12_mcg: float | None = None,
    source: str = "rospotrebnadzor",
):
    """Create a FoodLog with optional micronutrient values."""
    when = datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)
    return FoodLog.objects.create(
        user=user, dish_name=f"test d{days_ago}",
        calories=600, protein_g=25, fat_g=15, carbs_g=70,
        meal_type="lunch", logged_at=when,
        iron_mg=iron_mg, vitamin_d_iu=vitamin_d_iu,
        omega3_g=omega3_g, calcium_mg=calcium_mg,
        vitamin_b12_mcg=vitamin_b12_mcg,
        micronutrients_source=source,
    )


def _patterns_by_slug(user) -> dict:
    result = detect_patterns(user_id=user.id, force=True)
    return {p.slug: p for p in result.patterns}


@pytest.mark.django_db
class TestLowVitaminD:
    """<30% RDA (180 IU) in ≥5 of 7 days → low_vitamin_d."""

    def test_fires_when_5_of_7_low(self, user_with_profile):
        # 5 days at 100 IU (16% of 600), 2 days at 600 IU (100%).
        for d in range(5):
            _seed_log(user_with_profile, days_ago=d, vitamin_d_iu=100)
        for d in range(5, 7):
            _seed_log(user_with_profile, days_ago=d, vitamin_d_iu=600)

        patterns = _patterns_by_slug(user_with_profile)
        assert "low_vitamin_d" in patterns
        assert patterns["low_vitamin_d"].severity == "medium"

    def test_does_not_fire_when_4_of_7_low(self, user_with_profile):
        for d in range(4):
            _seed_log(user_with_profile, days_ago=d, vitamin_d_iu=100)
        for d in range(4, 7):
            _seed_log(user_with_profile, days_ago=d, vitamin_d_iu=600)

        patterns = _patterns_by_slug(user_with_profile)
        assert "low_vitamin_d" not in patterns


@pytest.mark.django_db
class TestLowIron:
    """<50% RDA (9 mg) in ≥7 of 14 days → low_iron."""

    def test_fires_when_7_of_14_low(self, user_with_profile):
        for d in range(7):
            _seed_log(user_with_profile, days_ago=d, iron_mg=3)
        for d in range(7, 14):
            _seed_log(user_with_profile, days_ago=d, iron_mg=20)

        patterns = _patterns_by_slug(user_with_profile)
        assert "low_iron" in patterns

    def test_does_not_fire_when_6_of_14_low(self, user_with_profile):
        for d in range(6):
            _seed_log(user_with_profile, days_ago=d, iron_mg=3)
        for d in range(6, 14):
            _seed_log(user_with_profile, days_ago=d, iron_mg=20)

        patterns = _patterns_by_slug(user_with_profile)
        assert "low_iron" not in patterns


@pytest.mark.django_db
class TestLowOmega3:
    """<50% RDA (0.55 g) in ≥14 of 21 days → low_omega3 (low severity)."""

    def test_fires_with_14_of_21_low(self, user_with_profile):
        for d in range(14):
            _seed_log(user_with_profile, days_ago=d, omega3_g=0.1)
        for d in range(14, 21):
            _seed_log(user_with_profile, days_ago=d, omega3_g=2.0)

        patterns = _patterns_by_slug(user_with_profile)
        assert "low_omega3" in patterns
        assert patterns["low_omega3"].severity == "low"


@pytest.mark.django_db
class TestLowCalcium:
    """<50% RDA (500 mg) in ≥14 of 21 days → low_calcium (low severity)."""

    def test_fires_with_chronic_deficit(self, user_with_profile):
        for d in range(15):
            _seed_log(user_with_profile, days_ago=d, calcium_mg=200)
        for d in range(15, 21):
            _seed_log(user_with_profile, days_ago=d, calcium_mg=1200)

        patterns = _patterns_by_slug(user_with_profile)
        assert "low_calcium" in patterns


@pytest.mark.django_db
class TestLowB12:
    """<30% RDA (0.72 mcg) in ≥7 of 14 days. Severity high for vegans."""

    def test_omnivore_severity_medium(self, user_with_profile):
        for d in range(8):
            _seed_log(user_with_profile, days_ago=d, vitamin_b12_mcg=0.2)
        for d in range(8, 14):
            _seed_log(user_with_profile, days_ago=d, vitamin_b12_mcg=2.5)

        patterns = _patterns_by_slug(user_with_profile)
        assert "low_b12" in patterns
        assert patterns["low_b12"].severity == "medium"

    def test_vegan_severity_high(self, user_with_profile):
        # Flip vegan flag — same data should escalate severity.
        prof = user_with_profile.nutrition_profile
        prof.health_flags = {"vegan": True}
        prof.save()
        for d in range(8):
            _seed_log(user_with_profile, days_ago=d, vitamin_b12_mcg=0.2)
        for d in range(8, 14):
            _seed_log(user_with_profile, days_ago=d, vitamin_b12_mcg=2.5)

        patterns = _patterns_by_slug(user_with_profile)
        assert "low_b12" in patterns
        assert patterns["low_b12"].severity == "high"


@pytest.mark.django_db
class TestQualityGate:
    """Detector skips windows where >40% of rows are AI-estimated."""

    def test_skips_when_majority_ai_estimated(self, user_with_profile):
        # 5 ai-estimate days + 2 rospotrebnadzor → 71% ai → above 40%.
        # Even though the deficit is real, source quality is too low.
        for d in range(5):
            _seed_log(
                user_with_profile, days_ago=d, vitamin_d_iu=100,
                source="ai_estimate",
            )
        for d in range(5, 7):
            _seed_log(
                user_with_profile, days_ago=d, vitamin_d_iu=600,
                source="rospotrebnadzor",
            )

        patterns = _patterns_by_slug(user_with_profile)
        assert "low_vitamin_d" not in patterns

    def test_fires_when_ai_below_40_pct(self, user_with_profile):
        # 2 ai-estimate + 5 rospotrebnadzor → 28% ai → below 40%.
        for d in range(2):
            _seed_log(
                user_with_profile, days_ago=d, vitamin_d_iu=100,
                source="ai_estimate",
            )
        for d in range(2, 7):
            _seed_log(
                user_with_profile, days_ago=d, vitamin_d_iu=100,
                source="rospotrebnadzor",
            )

        patterns = _patterns_by_slug(user_with_profile)
        assert "low_vitamin_d" in patterns


@pytest.mark.django_db
class TestEatingDisorderSuppression:
    """All 5 micronutrient patterns suppressed for ED users."""

    def test_low_vitamin_d_suppressed(self, user_with_profile):
        prof = user_with_profile.nutrition_profile
        prof.health_flags = {"eating_disorder": True}
        prof.save()
        for d in range(7):
            _seed_log(user_with_profile, days_ago=d, vitamin_d_iu=50)

        patterns = _patterns_by_slug(user_with_profile)
        assert "low_vitamin_d" not in patterns

    def test_low_iron_suppressed_for_ed(self, user_with_profile):
        prof = user_with_profile.nutrition_profile
        prof.health_flags = {"eating_disorder": True}
        prof.save()
        for d in range(10):
            _seed_log(user_with_profile, days_ago=d, iron_mg=2)

        patterns = _patterns_by_slug(user_with_profile)
        assert "low_iron" not in patterns


@pytest.mark.django_db
class TestNoFalsePositiveOnEmptyData:
    """No micronutrient logs → no patterns. Doesn't crash."""

    def test_no_data_returns_no_patterns(self, user_with_profile):
        # No logs at all.
        patterns = _patterns_by_slug(user_with_profile)
        for slug in ("low_vitamin_d", "low_iron", "low_omega3",
                     "low_calcium", "low_b12"):
            assert slug not in patterns

    def test_logs_with_no_micronutrients_no_patterns(self, user_with_profile):
        # Log with all micros=None (legacy data, source="unknown").
        for d in range(7):
            _seed_log(user_with_profile, days_ago=d, source="unknown")

        patterns = _patterns_by_slug(user_with_profile)
        for slug in ("low_vitamin_d", "low_iron", "low_omega3",
                     "low_calcium", "low_b12"):
            assert slug not in patterns
