"""Phase 3 smoke suite — mirrors docs/SMOKE_TESTS_PHASE_3.md 1:1.

Test names match the checklist IDs verbatim. Each test's docstring quotes
the row from the markdown so a failure points straight at the spec.

Run:
    .venv\\Scripts\\python.exe -m pytest nutrition/tests/smoke -v

Filter:
    pytest nutrition/tests/smoke -v -k "test_3_"   # DRF-302 only
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from rest_framework import status

from nutrition.models import (
    Beverage,
    FoodLog,
    NutritionOutboxEvent,
    NutritionProfile,
    WaterEntry,
)
from nutrition.tasks import (
    deliver_outbox_events_task,
    purge_deleted_water_entries_task,
)
from nutrition.webhook_delivery import (
    BACKOFF_SECONDS,
    MAX_RETRIES,
    compute_signature,
)
from users.models import User


pytestmark = pytest.mark.django_db


# Quick aliases
URL_BEVERAGES = "/api/v1/nutrition/internal/beverages/"
URL_PROFILE = "/api/v1/nutrition/internal/profile/"
URL_WATER = "/api/v1/nutrition/internal/water/"
URL_WATER_TODAY = "/api/v1/nutrition/internal/water/today/"
URL_SCAN = "/api/v1/nutrition/internal/scan/"
URL_SUMMARY = "/api/v1/nutrition/internal/summary/"
URL_PATTERNS = "/api/v1/nutrition/internal/patterns/"
URL_RETURNING_SUCCESS = "/api/v1/nutrition/internal/insights/returning_success/"


# ===========================================================================
# §0 — Pre-flight (subset that runs locally)
# ===========================================================================


class TestSection0Preflight:
    """Pre-flight § — only tests that can run off the VPS.

    P1/P4/P5 are deployment-state checks (covered by the manual checklist).
    """

    def test_p2_migrations_applied_for_phase_3_tables(self):
        """P2 — `showmigrations nutrition` must list the four Phase 3 migrations."""
        from django.db import connection
        tables = set(connection.introspection.table_names())
        for needed in (
            "nutrition_beverage",
            "nutrition_waterentry",
            "nutrition_nutritionprofile",
            "nutrition_nutritionoutboxevent",
        ):
            assert needed in tables, f"missing table {needed}"

    def test_p3_seed_command_is_idempotent(self):
        """P3 — `seed_beverages` runs without errors and is repeatable."""
        call_command("seed_beverages")
        first = Beverage.objects.count()
        assert first >= 50
        call_command("seed_beverages")
        assert Beverage.objects.count() == first

    def test_p6_internal_endpoint_rejects_anonymous(self, client_api):
        """P6 — service-token-protected endpoint refuses unauth'd."""
        resp = client_api.get(URL_BEVERAGES)
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED


# ===========================================================================
# §1 — DRF-301 Beverage catalog
# ===========================================================================


class TestSection1Beverages:
    def test_1_1_list_returns_min_50_across_9_categories(
        self, client_api, headers, seed_beverages, proxy_user,
    ):
        """1.1 — список ≥ 50 строк, 9 категорий."""
        resp = client_api.get(URL_BEVERAGES, **headers)
        assert resp.status_code == status.HTTP_200_OK
        beverages = resp.json()["data"]["beverages"]
        assert len(beverages) >= 50
        cats = {b["category"] for b in beverages}
        assert cats >= {
            "water", "tea", "coffee", "juice", "soda",
            "milk", "alcohol", "broth", "sport",
        }

    def test_1_2_cache_control_max_age_3600(
        self, client_api, headers, seed_beverages, proxy_user,
    ):
        """1.2 — Cache-Control: max-age=3600."""
        resp = client_api.get(URL_BEVERAGES, **headers)
        assert resp["Cache-Control"] == "max-age=3600"

    def test_1_3_kofe_chernyi_aliases_for_free_text_parser(
        self, client_api, headers, seed_beverages, proxy_user,
    ):
        """1.3 — kofe_chernyi алиасы покрывают кофе/coffee/americano."""
        resp = client_api.get(URL_BEVERAGES, **headers)
        kofe = next(
            b for b in resp.json()["data"]["beverages"]
            if b["slug"] == "kofe_chernyi"
        )
        for alias in ("кофе", "coffee", "americano"):
            assert alias in kofe["aliases"]

    def test_1_4_inactive_rows_filtered(
        self, client_api, headers, seed_beverages, proxy_user,
    ):
        """1.4 — is_active=False скрывает строку из списка."""
        Beverage.objects.filter(slug="vodka").update(is_active=False)
        resp = client_api.get(URL_BEVERAGES, **headers)
        slugs = {b["slug"] for b in resp.json()["data"]["beverages"]}
        assert "vodka" not in slugs

    def test_1_5_no_token_returns_401(self, client_api, seed_beverages):
        """1.5 — отсутствие service token = 401."""
        resp = client_api.get(URL_BEVERAGES, HTTP_X_EXTERNAL_USER_ID="bot:1")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED


# ===========================================================================
# §2 — DRF-300 NutritionProfile
# ===========================================================================


class TestSection2Profile:
    def test_2_1_get_before_create_returns_exists_false(
        self, client_api, headers, proxy_user,
    ):
        """2.1 — GET профиля до создания → exists=false."""
        resp = client_api.get(URL_PROFILE, **headers)
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()["data"]
        assert body["exists"] is False
        assert body["external_user_id"] == "bot:smoke"

    def test_2_2_minimal_post_computes_norms(
        self, client_api, headers, proxy_user,
    ):
        """2.2 — POST минимального профиля рассчитывает BMR=1370 и нормы."""
        resp = client_api.post(URL_PROFILE, {
            "gender": "female", "age": 40, "height_cm": 165,
            "weight_kg": 70.0, "goal": "maintain",
        }, format="json", **headers)
        body = resp.json()["data"]
        assert resp.status_code == status.HTTP_200_OK
        assert body["norms"]["bmr"] == 1370
        assert body["norms"]["daily_kcal"] > 0

    def test_2_3_patch_semantics_only_provided_fields_change(
        self, client_api, headers, proxy_user,
    ):
        """2.3 — посылаем только weight_kg → age/height сохраняются."""
        client_api.post(URL_PROFILE, {
            "gender": "female", "age": 40, "height_cm": 165,
            "weight_kg": 70.0, "goal": "maintain",
        }, format="json", **headers)
        resp = client_api.post(
            URL_PROFILE, {"weight_kg": 72.0}, format="json", **headers,
        )
        body = resp.json()["data"]
        assert body["weight_kg"] == 72.0
        assert body["age"] == 40
        assert body["height_cm"] == 165

    @pytest.mark.parametrize("payload", [
        {"age": 14},                # below 16
        {"age": 105},               # above 99
    ])
    def test_2_4_age_validation(self, client_api, headers, proxy_user, payload):
        """2.4 — age вне 16-99 → 400."""
        resp = client_api.post(URL_PROFILE, payload, format="json", **headers)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_2_5_unknown_health_flag_rejected(
        self, client_api, headers, proxy_user,
    ):
        """2.5 — health_flags whitelist."""
        resp = client_api.post(
            URL_PROFILE,
            {"health_flags": {"made_up_flag": True}},
            format="json", **headers,
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_2_6_pregnancy_override_lose_to_maintain_with_bonuses(
        self, client_api, headers, proxy_user,
    ):
        """2.6 — pregnancy: goal lose→maintain, +200 ккал, +25г белка."""
        baseline_resp = client_api.post(URL_PROFILE, {
            "gender": "female", "age": 30, "height_cm": 165, "weight_kg": 65.0,
            "goal": "maintain",
        }, format="json", **headers)
        baseline = baseline_resp.json()["data"]["norms"]
        # Reset profile by deleting and re-creating with pregnancy
        NutritionProfile.objects.all().delete()
        resp = client_api.post(URL_PROFILE, {
            "gender": "female", "age": 30, "height_cm": 165, "weight_kg": 65.0,
            "goal": "lose", "pace": "moderate",
            "health_flags": {"pregnant": True},
        }, format="json", **headers)
        body = resp.json()["data"]
        assert body["goal"] == "maintain"
        assert body["goal_overridden_by"] == "pregnancy"
        assert body["norms"]["daily_kcal"] == baseline["daily_kcal"] + 200
        assert body["norms"]["daily_protein_g"] == baseline["daily_protein_g"] + 25
        reasons = {o["reason"] for o in body["overrides_applied"]}
        assert "pregnancy" in reasons

    def test_2_7_eating_disorder_pins_goal_to_maintain(
        self, client_api, headers, proxy_user,
    ):
        """2.7 — eating_disorder=true → goal=maintain unconditionally."""
        resp = client_api.post(URL_PROFILE, {
            "gender": "female", "age": 30, "height_cm": 165, "weight_kg": 60.0,
            "goal": "lose", "pace": "moderate",
            "health_flags": {"eating_disorder": True},
        }, format="json", **headers)
        body = resp.json()["data"]
        assert body["goal"] == "maintain"
        assert body["goal_overridden_by"] == "eating_disorder"

    def test_2_8_bmr_floor_ladder_softens_pace_or_flips_goal(
        self, client_api, headers, proxy_user,
    ):
        """2.8 — экстремальный профиль → pace=gentle ИЛИ goal=maintain."""
        resp = client_api.post(URL_PROFILE, {
            "gender": "female", "age": 50, "height_cm": 160, "weight_kg": 55.0,
            "activity_coefficient": 1.2,
            "goal": "lose", "pace": "moderate",
        }, format="json", **headers)
        body = resp.json()["data"]
        # At least one of: pace softened OR goal flipped to maintain via BMR floor.
        if body["goal"] == "lose":
            assert body["pace"] == "gentle"
            assert any(
                o["reason"] == "bmr_floor"
                for o in body["overrides_applied"]
            )
        else:
            assert body["goal"] == "maintain"
            assert any(
                o["reason"] == "bmr_floor" and o["to"].get("goal") == "maintain"
                for o in body["overrides_applied"]
            )

    def test_2_9_idempotency_key_replay_returns_cached_no_double_apply(
        self, client_api, headers, proxy_user,
    ):
        """2.9 — replay возвращает cached, между ними другой POST не отменяется."""
        h_idem = {**headers, "HTTP_IDEMPOTENCY_KEY": "smoke-2-9"}
        body_a = {
            "gender": "female", "age": 40, "height_cm": 165,
            "weight_kg": 70.0, "goal": "maintain",
        }
        client_api.post(URL_PROFILE, body_a, format="json", **h_idem)
        client_api.post(URL_PROFILE, {"weight_kg": 80.0}, format="json", **headers)
        replay = client_api.post(URL_PROFILE, body_a, format="json", **h_idem)
        # Cached response has weight_kg=70, but DB row keeps the in-between 80.
        assert replay.json()["data"]["weight_kg"] == 70.0
        assert NutritionProfile.objects.get(user=proxy_user).weight_kg == 80.0

    def test_2_10_complete_true_first_flips_onboarded_at_then_no_op(
        self, client_api, headers, proxy_user,
    ):
        """2.10 — complete=true ставит onboarded_at один раз."""
        r1 = client_api.post(URL_PROFILE, {
            "gender": "female", "age": 40, "height_cm": 165, "weight_kg": 70.0,
            "complete": True,
        }, format="json", **headers)
        first_ts = r1.json()["data"]["onboarded_at"]
        assert first_ts is not None
        r2 = client_api.post(
            URL_PROFILE, {"complete": True}, format="json", **headers,
        )
        assert r2.json()["data"]["onboarded_at"] == first_ts

    def test_2_11_skipped_fields_set_skipped_flags(
        self, client_api, headers, proxy_user,
    ):
        """2.11 — _skipped_fields → *_skipped boolean flags."""
        resp = client_api.post(URL_PROFILE, {
            "gender": "female",
            "_skipped_fields": ["weight", "age"],
        }, format="json", **headers)
        flags = resp.json()["data"]["health_flags"]
        assert flags.get("weight_skipped") is True
        assert flags.get("age_skipped") is True


# ===========================================================================
# §3 — DRF-302 Water tracker
# ===========================================================================


@pytest.fixture
def profile_default(make_profile):
    """Provide the implicit profile §3 expects (norm 2000)."""
    return make_profile()


class TestSection3Water:
    def test_3_1_pure_water_no_beverage(
        self, client_api, headers, profile_default, seed_beverages,
    ):
        """3.1 — POST {ml:250} без beverage → water_ml=250, kcal=0, pct=12."""
        resp = client_api.post(
            URL_WATER, {"ml": 250}, format="json", **headers,
        )
        assert resp.status_code == status.HTTP_201_CREATED
        body = resp.json()["data"]
        assert body["water_ml"] == 250
        assert body["kcal"] == 0
        assert body["today_progress_pct"] == 12

    def test_3_2_coffee_beverage_macros_and_caffeine(
        self, client_api, headers, profile_default, seed_beverages,
    ):
        """3.2 — кофе ≈ 80 мг кофеина, today_caffeine суммируется."""
        client_api.post(
            URL_WATER,
            {"ml": 200, "beverage_slug": "kofe_chernyi"},
            format="json", **headers,
        )
        today_resp = client_api.get(URL_WATER_TODAY, **headers)
        body = today_resp.json()["data"]
        assert body["today_caffeine_mg"] == pytest.approx(80.0, abs=0.5)

    def test_3_3_latte_creates_food_log_mirror(
        self, client_api, headers, profile_default, seed_beverages,
    ):
        """3.3 — latte (kcal>0) добавляет FoodLog для дневной суммы."""
        before = FoodLog.objects.count()
        resp = client_api.post(
            URL_WATER,
            {"ml": 200, "beverage_slug": "latte"},
            format="json", **headers,
        )
        entry = WaterEntry.objects.get(id=resp.json()["data"]["entry_id"])
        assert entry.food_log_id is not None
        assert FoodLog.objects.count() == before + 1

    @pytest.mark.parametrize("ml", [5, 5000])
    def test_3_4_invalid_ml_rejected(
        self, client_api, headers, profile_default, seed_beverages, ml,
    ):
        """3.4 — ml вне [10, 3000] → 400."""
        resp = client_api.post(URL_WATER, {"ml": ml}, format="json", **headers)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_3_5_unknown_beverage_slug_rejected(
        self, client_api, headers, profile_default, seed_beverages,
    ):
        """3.5 — несуществующий slug → 400."""
        resp = client_api.post(
            URL_WATER,
            {"ml": 200, "beverage_slug": "кефирчик"},
            format="json", **headers,
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_3_6_idempotency_key_dedupes(
        self, client_api, headers, profile_default, seed_beverages,
    ):
        """3.6 — Idempotency-Key: один entry_id, одна строка в БД."""
        h_idem = {**headers, "HTTP_IDEMPOTENCY_KEY": "smoke-3-6"}
        r1 = client_api.post(URL_WATER, {"ml": 250}, format="json", **h_idem)
        r2 = client_api.post(URL_WATER, {"ml": 250}, format="json", **h_idem)
        assert r1.json()["data"]["entry_id"] == r2.json()["data"]["entry_id"]
        assert WaterEntry.objects.count() == 1

    def test_3_7_milestone_50_fires_once_per_day(
        self, client_api, headers, profile_default, seed_beverages,
    ):
        """3.7 — 50% триггерится один раз; третья запись уже без milestone."""
        client_api.post(URL_WATER, {"ml": 500}, format="json", **headers)
        r2 = client_api.post(URL_WATER, {"ml": 500}, format="json", **headers)
        assert "Половина" in (r2.json()["data"]["milestone_text"] or "")
        r3 = client_api.post(URL_WATER, {"ml": 200}, format="json", **headers)
        text = r3.json()["data"]["milestone_text"] or ""
        assert "Половина" not in text

    def test_3_8_milestone_100(
        self, client_api, headers, profile_default, seed_beverages,
    ):
        """3.8 — норма выполнена."""
        client_api.post(URL_WATER, {"ml": 1100}, format="json", **headers)
        r = client_api.post(URL_WATER, {"ml": 950}, format="json", **headers)
        assert "норма выполнена" in (r.json()["data"]["milestone_text"] or "").lower()

    def test_3_9_milestone_150(
        self, client_api, headers, profile_default, seed_beverages,
    ):
        """3.9 — 150% — «запасом»."""
        client_api.post(URL_WATER, {"ml": 1100}, format="json", **headers)
        client_api.post(URL_WATER, {"ml": 950}, format="json", **headers)
        r = client_api.post(URL_WATER, {"ml": 1000}, format="json", **headers)
        assert "запасом" in (r.json()["data"]["milestone_text"] or "")

    def test_3_10_undo_does_not_unlock_milestone(
        self, client_api, headers, profile_default, seed_beverages,
    ):
        """3.10 — после DELETE milestone-записи 50% повторно не сработает."""
        client_api.post(URL_WATER, {"ml": 500}, format="json", **headers)
        r2 = client_api.post(URL_WATER, {"ml": 500}, format="json", **headers)
        assert "Половина" in (r2.json()["data"]["milestone_text"] or "")
        client_api.delete(
            f"{URL_WATER}{r2.json()['data']['entry_id']}/", **headers,
        )
        r3 = client_api.post(URL_WATER, {"ml": 500}, format="json", **headers)
        assert r3.json()["data"]["milestone_text"] is None

    def test_3_11_alcohol_recovery_hint(
        self, client_api, headers, profile_default, seed_beverages,
    ):
        """3.11 — alcohol_recovery_hint=true для категории alcohol."""
        resp = client_api.post(
            URL_WATER,
            {"ml": 150, "beverage_slug": "vino_krasnoe"},
            format="json", **headers,
        )
        assert resp.json()["data"]["alcohol_recovery_hint"] is True

    def test_3_12_caffeine_warning_when_pregnant(
        self, client_api, headers, proxy_user, seed_beverages,
    ):
        """3.12 — pregnant + 600мл espresso (~1080 мг кофеина) → caffeine_warning."""
        NutritionProfile.objects.create(
            user=proxy_user, daily_kcal=2000, daily_water_ml=2000,
            daily_protein_g=100, health_flags={"pregnant": True},
        )
        # Faithful to checklist 3.12: 600 ml espresso × 180 mg/100ml = 1080 mg
        # — well over the 200 mg pregnancy threshold.
        resp = client_api.post(
            URL_WATER,
            {"ml": 600, "beverage_slug": "espresso"},
            format="json", **headers,
        )
        assert resp.json()["data"]["caffeine_warning"] is not None

    def test_3_13_eating_disorder_strips_kcal_milestone_alcohol_caffeine(
        self, client_api, headers, proxy_user, seed_beverages,
    ):
        """3.13 — ED режим: на проводе 0/null, но в БД реальные макро."""
        NutritionProfile.objects.create(
            user=proxy_user, daily_kcal=2000, daily_water_ml=2000,
            daily_protein_g=100, health_flags={"eating_disorder": True},
        )
        resp = client_api.post(
            URL_WATER,
            {"ml": 1500, "beverage_slug": "vino_krasnoe"},
            format="json", **headers,
        )
        body = resp.json()["data"]
        assert body["kcal"] == 0
        assert body["milestone_text"] is None
        assert body["alcohol_recovery_hint"] is False
        assert body["caffeine_warning"] is None
        # DB still has the truth.
        entry = WaterEntry.objects.get(id=body["entry_id"])
        assert entry.kcal > 0

    def test_3_14_soft_delete_updates_today_total_with_restore_window(
        self, client_api, headers, profile_default, seed_beverages,
    ):
        """3.14 — soft-delete уменьшает today_total и возвращает restore_window."""
        r = client_api.post(URL_WATER, {"ml": 500}, format="json", **headers)
        entry_id = r.json()["data"]["entry_id"]
        del_resp = client_api.delete(f"{URL_WATER}{entry_id}/", **headers)
        body = del_resp.json()["data"]
        assert body["deleted"] is True
        assert body["today_total_water_ml"] == 0
        assert "restore_window_expires_at" in body

    def test_3_15_restore_within_window(
        self, client_api, headers, profile_default, seed_beverages,
    ):
        """3.15 — restore сразу после DELETE возвращает запись."""
        r = client_api.post(URL_WATER, {"ml": 250}, format="json", **headers)
        entry_id = r.json()["data"]["entry_id"]
        client_api.delete(f"{URL_WATER}{entry_id}/", **headers)
        restore = client_api.post(f"{URL_WATER}{entry_id}/restore/", **headers)
        assert restore.status_code == status.HTTP_200_OK
        assert restore.json()["data"]["restored"] is True

    def test_3_16_restore_after_window_returns_410(
        self, client_api, headers, profile_default, seed_beverages,
    ):
        """3.16 — после 15 минут restore = 410."""
        r = client_api.post(URL_WATER, {"ml": 250}, format="json", **headers)
        entry_id = r.json()["data"]["entry_id"]
        client_api.delete(f"{URL_WATER}{entry_id}/", **headers)
        WaterEntry.objects.filter(id=entry_id).update(
            deleted_at=datetime.now(dt_tz.utc) - timedelta(minutes=20),
        )
        restore = client_api.post(f"{URL_WATER}{entry_id}/restore/", **headers)
        assert restore.status_code == status.HTTP_410_GONE

    def test_3_17_cross_user_delete_returns_404(
        self, client_api, headers, profile_default, seed_beverages,
    ):
        """3.17 — DELETE чужой строки → 404."""
        r = client_api.post(URL_WATER, {"ml": 250}, format="json", **headers)
        entry_id = r.json()["data"]["entry_id"]
        # Switch to a different external user.
        User.objects.create(username="bot:other", role="client", is_proxy=True)
        other_headers = {
            "HTTP_X_SERVICE_TOKEN": headers["HTTP_X_SERVICE_TOKEN"],
            "HTTP_X_EXTERNAL_USER_ID": "bot:other",
        }
        resp = client_api.delete(f"{URL_WATER}{entry_id}/", **other_headers)
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_3_18_today_per_category_cup_counts(
        self, client_api, headers, profile_default, seed_beverages,
    ):
        """3.18 — coffee/tea cup counters в /today/."""
        client_api.post(
            URL_WATER, {"ml": 200, "beverage_slug": "kofe_chernyi"},
            format="json", **headers,
        )
        client_api.post(
            URL_WATER, {"ml": 200, "beverage_slug": "latte"},
            format="json", **headers,
        )
        client_api.post(
            URL_WATER, {"ml": 200, "beverage_slug": "chai_zelenyi"},
            format="json", **headers,
        )
        resp = client_api.get(URL_WATER_TODAY, **headers)
        body = resp.json()["data"]
        assert body["today_total_coffee_cups"] == 2
        assert body["today_total_tea_cups"] == 1

    def test_3_19_soft_deleted_excluded_from_today(
        self, client_api, headers, profile_default, seed_beverages,
    ):
        """3.19 — soft-deleted записи не возвращаются в /today/."""
        r = client_api.post(URL_WATER, {"ml": 500}, format="json", **headers)
        client_api.post(URL_WATER, {"ml": 250}, format="json", **headers)
        client_api.delete(
            f"{URL_WATER}{r.json()['data']['entry_id']}/", **headers,
        )
        resp = client_api.get(URL_WATER_TODAY, **headers)
        body = resp.json()["data"]
        assert len(body["entries"]) == 1
        assert body["today_total_water_ml"] == 250


# ===========================================================================
# §4 — DRF-303 caption + with_comment
# ===========================================================================


def _png_bytes() -> bytes:
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, format="PNG")
    return buf.getvalue()


class TestSection4ScanAndSummary:
    """§4 splits caption tests at provider level (deterministic, no
    multipart parser ambiguity) and keeps the HTTP-layer summary tests
    via the actual endpoint."""

    def test_4_1_scan_provider_works_without_caption(
        self, settings, patch_openai_client,
    ):
        """4.1 — provider.scan() без caption отрабатывает."""
        from nutrition.providers.openai_vision import OpenAIVisionProvider
        settings.OPENAI_API_KEY = "test"
        msg = MagicMock()
        msg.content = (
            '{"dish_name":"x","confidence":0.9,"portion_g":100,"ingredients":[]}'
        )
        choice = MagicMock()
        choice.message = msg
        patch_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[choice],
        )
        result = OpenAIVisionProvider().scan(b"\xff\xd8\xff")
        assert result.dish_name == "x"
        sent = patch_openai_client.chat.completions.create.call_args.kwargs
        text_part = next(
            p for p in sent["messages"][1]["content"] if p["type"] == "text"
        )
        assert "Подпись пользователя" not in text_part["text"]

    def test_4_2_caption_lands_in_user_prompt(
        self, settings, patch_openai_client,
    ):
        """4.2 — caption попадает в user-prompt OpenAI."""
        from nutrition.providers.openai_vision import OpenAIVisionProvider
        settings.OPENAI_API_KEY = "test"
        msg = MagicMock()
        msg.content = (
            '{"dish_name":"x","confidence":0.9,"portion_g":100,"ingredients":[]}'
        )
        choice = MagicMock()
        choice.message = msg
        patch_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[choice],
        )
        OpenAIVisionProvider().scan(
            b"\xff\xd8\xff", caption="это половина порции у мамы",
        )
        sent = patch_openai_client.chat.completions.create.call_args.kwargs
        text_part = next(
            p for p in sent["messages"][1]["content"] if p["type"] == "text"
        )
        assert "половина порции" in text_part["text"]
        assert "Подпись пользователя" in text_part["text"]

    def test_4_3_caption_truncated_at_280_chars(
        self, settings, patch_openai_client,
    ):
        """4.3 — caption длиной 500 → ≤280 в prompt."""
        from nutrition.providers.openai_vision import OpenAIVisionProvider
        settings.OPENAI_API_KEY = "test"
        msg = MagicMock()
        msg.content = (
            '{"dish_name":"x","confidence":0.9,"portion_g":100,"ingredients":[]}'
        )
        choice = MagicMock()
        choice.message = msg
        patch_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[choice],
        )
        OpenAIVisionProvider().scan(b"\xff\xd8\xff", caption="x" * 500)
        sent = patch_openai_client.chat.completions.create.call_args.kwargs
        text_part = next(
            p for p in sent["messages"][1]["content"] if p["type"] == "text"
        )
        assert text_part["text"].count("x") <= 280

    def test_4_3b_endpoint_accepts_caption_field(
        self, client_api, headers, proxy_user, settings,
    ):
        """4.3b — HTTP endpoint принимает поле caption (не VALIDATION_ERROR)."""
        from django.core.files.uploadedfile import SimpleUploadedFile
        from nutrition.services.food_scanner_router import RouterResult
        from nutrition.providers.base import ScanResult
        settings.FOOD_SCANNER_PRIMARY = "openai"
        settings.FOOD_SCANNER_FALLBACK = ""
        router_mock = MagicMock()
        router_mock.scan.return_value = RouterResult(
            result=ScanResult(
                dish_name="x", confidence=0.9, portion_g=100,
                ingredients=[], provider="openai",
            ),
            primary_provider_name="openai",
        )
        image = SimpleUploadedFile("test.png", _png_bytes(), content_type="image/png")
        with patch(
            "nutrition.views.FoodScannerRouter", return_value=router_mock,
        ), patch(
            "nutrition.views.NutritionLookup",
            return_value=MagicMock(lookup=lambda *a, **kw: None),
        ):
            resp = client_api.post(
                URL_SCAN,
                {"image": image, "caption": "это половина"},
                format="multipart", **headers,
            )
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        # Verify caption forwarded to router.
        assert router_mock.scan.call_args.kwargs.get("caption") == "это половина"

    def test_4_4_summary_default_no_ai_comment(
        self, client_api, headers, proxy_user,
    ):
        """4.4 — summary без with_comment → ai_comment=null."""
        resp = client_api.get(URL_SUMMARY, **headers)
        assert resp.json()["data"]["ai_comment"] is None

    def test_4_5_summary_with_comment_returns_short_text(
        self, client_api, headers, proxy_user, make_profile,
    ):
        """4.5 — with_comment=true → ai_comment ≤220 chars, ≤3 предложений."""
        make_profile(goal="lose")
        client_factory = MagicMock()
        msg = MagicMock()
        msg.content = "Хороший день. Завтра попробуй больше белка с утра."
        choice = MagicMock()
        choice.message = msg
        client_factory.return_value.chat.completions.create.return_value = MagicMock(
            choices=[choice],
        )
        with patch(
            "ai.services.llm_client.get_openai_client", client_factory,
        ):
            resp = client_api.get(
                URL_SUMMARY, {"with_comment": "true"}, **headers,
            )
        comment = resp.json()["data"]["ai_comment"]
        assert comment is not None
        assert len(comment) <= 220

    def test_4_6_ai_comment_cached_for_6h(
        self, client_api, headers, proxy_user, make_profile,
    ):
        """4.6 — повторный GET с with_comment не вызывает LLM."""
        make_profile(goal="lose")
        client_factory = MagicMock()
        msg = MagicMock()
        msg.content = "Стабильный день, держи темп."
        choice = MagicMock()
        choice.message = msg
        client_factory.return_value.chat.completions.create.return_value = MagicMock(
            choices=[choice],
        )
        with patch(
            "ai.services.llm_client.get_openai_client", client_factory,
        ):
            r1 = client_api.get(
                URL_SUMMARY, {"with_comment": "true"}, **headers,
            )
            client_factory.reset_mock()
            r2 = client_api.get(
                URL_SUMMARY, {"with_comment": "true"}, **headers,
            )
        assert r1.json()["data"]["ai_comment"] == r2.json()["data"]["ai_comment"]
        assert client_factory.call_count == 0

    def test_4_7_eating_disorder_returns_supportive_template_no_digits(
        self, client_api, headers, proxy_user, make_profile,
    ):
        """4.7 — ED template без цифр; LLM не вызывается."""
        make_profile(health_flags={"eating_disorder": True})
        # Patch must NOT be called — fail loudly if it is.
        forbid = MagicMock(side_effect=AssertionError("LLM must not run"))
        with patch("ai.services.llm_client.get_openai_client", forbid):
            resp = client_api.get(
                URL_SUMMARY, {"with_comment": "true"}, **headers,
            )
        comment = resp.json()["data"]["ai_comment"]
        assert comment is not None
        assert not any(ch.isdigit() for ch in comment)

    def test_4_8_cost_tripwire_logs_warning_at_limit_plus_one(
        self, settings, caplog,
    ):
        """4.8 — после превышения NUTRITION_AI_COMMENT_DAILY_LIMIT в логах warning."""
        settings.NUTRITION_AI_COMMENT_DAILY_LIMIT = 2
        from nutrition.services.ai_comment_service import _bump_cost_counter
        with caplog.at_level("WARNING", logger="nutrition.services.ai_comment_service"):
            for _ in range(4):
                _bump_cost_counter()
        warnings = [
            r for r in caplog.records
            if "daily_cost_warning" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert "limit=2" in warnings[0].getMessage()


# ===========================================================================
# §5 — DRF-304 Pattern detection
# ===========================================================================


class TestSection5Patterns:
    def test_5_1_empty_returns_zero_active_days(
        self, client_api, headers, profile_default,
    ):
        """5.1 — никаких записей → active_days=0, patterns=[]."""
        resp = client_api.get(URL_PATTERNS, **headers)
        body = resp.json()["data"]
        assert body["active_days"] == 0
        assert body["patterns"] == []

    def test_5_2_evening_sweets_fires_on_4_weekday_evenings(
        self, client_api, headers, proxy_user, profile_default, add_food_at,
    ):
        """5.2 — 4 будних вечера со «сладким» → evening_sweets."""
        # Walk back to find 4 weekdays.
        today = datetime.now(dt_tz.utc).date()
        days_added = 0
        d = today
        while days_added < 4:
            d -= timedelta(days=1)
            if d.weekday() < 5:
                add_food_at(
                    proxy_user,
                    days_ago=(today - d).days,
                    hour=20,
                    dish_name="Шоколад молочный",
                    kcal=200, protein_g=2, fat_g=10, carbs_g=20,
                )
                days_added += 1
        resp = client_api.get(URL_PATTERNS, **headers)
        slugs = {p["slug"] for p in resp.json()["data"]["patterns"]}
        assert "evening_sweets" in slugs

    def test_5_3_low_protein_fires_with_5_low_days_of_7(
        self, client_api, headers, proxy_user, profile_default, add_food_at,
    ):
        """5.3 — 5 дней <70% белка → low_protein."""
        for i in range(5):
            add_food_at(proxy_user, days_ago=i, kcal=400, protein_g=30)
        resp = client_api.get(URL_PATTERNS, **headers)
        slugs = {p["slug"] for p in resp.json()["data"]["patterns"]}
        assert "low_protein" in slugs

    def test_5_4_low_water_fires_with_4_dry_days_of_7(
        self, client_api, headers, proxy_user, profile_default, add_water_at,
    ):
        """5.4 — 4 дня <70% воды → low_water."""
        for i in range(4):
            add_water_at(proxy_user, days_ago=i, ml=500)
        resp = client_api.get(URL_PATTERNS, **headers)
        slugs = {p["slug"] for p in resp.json()["data"]["patterns"]}
        assert "low_water" in slugs

    def test_5_5_late_dinner_fires(
        self, client_api, headers, proxy_user, profile_default, add_food_at,
    ):
        """5.5 — 3 дня last meal >21:00 → late_dinner."""
        for i in range(3):
            add_food_at(proxy_user, days_ago=i, hour=22, kcal=500)
        resp = client_api.get(URL_PATTERNS, **headers)
        slugs = {p["slug"] for p in resp.json()["data"]["patterns"]}
        assert "late_dinner" in slugs

    def test_5_6_meal_skips_three_day_streak(
        self, client_api, headers, proxy_user, profile_default, add_food_at,
    ):
        """5.6 — 3 дня подряд kcal <30% → meal_skips."""
        for i in range(3):
            add_food_at(proxy_user, days_ago=i, kcal=300)
        resp = client_api.get(URL_PATTERNS, **headers)
        slugs = {p["slug"] for p in resp.json()["data"]["patterns"]}
        assert "meal_skips" in slugs

    def test_5_7_late_caffeine_fires(
        self, client_api, headers, proxy_user, profile_default,
        seed_beverages, add_water_at,
    ):
        """5.7 — 3 дня кофеин после 17:00 → late_caffeine."""
        coffee = Beverage.objects.get(slug="kofe_chernyi")
        for i in range(3):
            add_water_at(
                proxy_user, days_ago=i, hour=18,
                ml=200, beverage=coffee, caffeine_mg=80,
            )
        resp = client_api.get(URL_PATTERNS, **headers)
        slugs = {p["slug"] for p in resp.json()["data"]["patterns"]}
        assert "late_caffeine" in slugs

    def test_5_8_frequent_alcohol_silent_when_history_short(
        self, client_api, headers, proxy_user, profile_default,
        seed_beverages, add_water_at,
    ):
        """5.8 — frequent_alcohol требует ≥14 дней истории."""
        wine = Beverage.objects.get(slug="vino_krasnoe")
        for i in (0, 2):
            add_water_at(
                proxy_user, days_ago=i, hour=20,
                ml=150, beverage=wine,
            )
        resp = client_api.get(URL_PATTERNS, **headers)
        slugs = {p["slug"] for p in resp.json()["data"]["patterns"]}
        assert "frequent_alcohol" not in slugs

    def test_5_9_frequent_alcohol_fires_when_history_sufficient(
        self, client_api, headers, proxy_user, profile_default,
        seed_beverages, add_water_at,
    ):
        """5.9 — 2 дня alcohol при ≥14 днях истории → frequent_alcohol."""
        wine = Beverage.objects.get(slug="vino_krasnoe")
        # Anchor inside the 14-day collection window.
        add_water_at(proxy_user, days_ago=13, hour=12, ml=250)
        for i in (0, 2):
            add_water_at(
                proxy_user, days_ago=i, hour=20,
                ml=150, beverage=wine,
            )
        resp = client_api.get(URL_PATTERNS, **headers)
        slugs = {p["slug"] for p in resp.json()["data"]["patterns"]}
        assert "frequent_alcohol" in slugs

    def test_5_10_eating_disorder_suppresses_alcohol_protein_skips(
        self, client_api, headers, proxy_user, seed_beverages,
        make_profile, add_food_at, add_water_at,
    ):
        """5.10 — ED скрывает frequent_alcohol/low_protein/meal_skips."""
        make_profile(health_flags={"eating_disorder": True})
        wine = Beverage.objects.get(slug="vino_krasnoe")
        add_water_at(proxy_user, days_ago=13, hour=12, ml=250)
        for i in (0, 2):
            add_water_at(
                proxy_user, days_ago=i, hour=20,
                ml=150, beverage=wine,
            )
        for i in range(5):
            add_food_at(proxy_user, days_ago=i, kcal=400, protein_g=30)
        for i in range(3):
            add_food_at(proxy_user, days_ago=i, kcal=300)
        resp = client_api.get(URL_PATTERNS, **headers)
        slugs = {p["slug"] for p in resp.json()["data"]["patterns"]}
        assert "frequent_alcohol" not in slugs
        assert "low_protein" not in slugs
        assert "meal_skips" not in slugs

    def test_5_11_pregnant_suppresses_frequent_alcohol(
        self, client_api, headers, proxy_user, seed_beverages,
        make_profile, add_water_at,
    ):
        """5.11 — pregnant скрывает frequent_alcohol."""
        make_profile(health_flags={"pregnant": True})
        wine = Beverage.objects.get(slug="vino_krasnoe")
        add_water_at(proxy_user, days_ago=13, hour=12, ml=250)
        for i in (0, 2):
            add_water_at(
                proxy_user, days_ago=i, hour=20,
                ml=150, beverage=wine,
            )
        resp = client_api.get(URL_PATTERNS, **headers)
        slugs = {p["slug"] for p in resp.json()["data"]["patterns"]}
        assert "frequent_alcohol" not in slugs

    def test_5_12_cache_persists_for_12h(
        self, client_api, headers, proxy_user, profile_default, add_water_at,
    ):
        """5.12 — повторный GET возвращает кешированное значение.

        Two-pronged check: (a) cache key is populated after first call,
        (b) the cached snapshot is what the second call returns even
        though new data has been written that would change a fresh
        recompute.
        """
        from django.core.cache import cache
        from nutrition.services.pattern_detection_service import detect_patterns

        client_api.get(URL_PATTERNS, **headers)
        cache_key = f"nutrition.patterns:{proxy_user.id}"
        cached_snapshot = cache.get(cache_key)
        assert cached_snapshot is not None, "first call did not populate cache"
        assert cached_snapshot.active_days == 0

        # Add data that *would* bump active_days if the second call recomputed.
        add_water_at(proxy_user, days_ago=0, ml=250)
        # Sanity: a forced recompute would now see the new row.
        fresh = detect_patterns(user_id=proxy_user.id, force=True)
        assert fresh.active_days >= 1
        # Restore cached snapshot (force=True overwrote it) and confirm
        # the endpoint still returns the stale value.
        cache.set(cache_key, cached_snapshot, 12 * 60 * 60)
        resp = client_api.get(URL_PATTERNS, **headers)
        assert resp.json()["data"]["active_days"] == 0


# ===========================================================================
# §6 — DRF-305 Returning success
# ===========================================================================


class TestSection6ReturningSuccess:
    def test_6_1_empty_returns_false(
        self, client_api, headers, profile_default,
    ):
        """6.1 — пустые данные → detected=false."""
        resp = client_api.get(URL_RETURNING_SUCCESS, **headers)
        assert resp.json()["data"]["detected"] is False

    def test_6_2_happy_path_3_fail_2_recovery(
        self, client_api, headers, proxy_user, profile_default, add_food_at,
    ):
        """6.2 — 3 fail → 2 recovery → detected=true."""
        for i in (4, 3, 2):
            add_food_at(proxy_user, days_ago=i, kcal=800)
        for i in (1, 0):
            add_food_at(proxy_user, days_ago=i, kcal=1900)
        body = client_api.get(URL_RETURNING_SUCCESS, **headers).json()["data"]
        assert body["detected"] is True
        assert body["failure_streak_days"] == 3
        assert body["recovery_days"] == 2
        assert body["since_recovery_started"] is not None

    def test_6_3_recovery_too_short(
        self, client_api, headers, proxy_user, profile_default, add_food_at,
    ):
        """6.3 — 3 fail + 1 recovery → detected=false."""
        for i in (4, 3, 2):
            add_food_at(proxy_user, days_ago=i, kcal=800)
        add_food_at(proxy_user, days_ago=0, kcal=1900)
        body = client_api.get(URL_RETURNING_SUCCESS, **headers).json()["data"]
        assert body["detected"] is False

    def test_6_4_failure_too_short(
        self, client_api, headers, proxy_user, profile_default, add_food_at,
    ):
        """6.4 — 2 fail + 2 recovery → detected=false."""
        for i in (3, 2):
            add_food_at(proxy_user, days_ago=i, kcal=800)
        for i in (1, 0):
            add_food_at(proxy_user, days_ago=i, kcal=1900)
        body = client_api.get(URL_RETURNING_SUCCESS, **headers).json()["data"]
        assert body["detected"] is False

    def test_6_5_interrupted_recovery(
        self, client_api, headers, proxy_user, profile_default, add_food_at,
    ):
        """6.5 — 3 fail + день >110% + 1 recovery → detected=false."""
        for i in (4, 3, 2):
            add_food_at(proxy_user, days_ago=i, kcal=800)
        add_food_at(proxy_user, days_ago=1, kcal=2500)  # above 110% — "other"
        add_food_at(proxy_user, days_ago=0, kcal=1900)
        body = client_api.get(URL_RETURNING_SUCCESS, **headers).json()["data"]
        assert body["detected"] is False

    def test_6_6_eating_disorder_short_circuits(
        self, client_api, headers, proxy_user, make_profile, add_food_at,
    ):
        """6.6 — ED → detected=false вне зависимости от данных."""
        make_profile(health_flags={"eating_disorder": True})
        for i in (4, 3, 2):
            add_food_at(proxy_user, days_ago=i, kcal=800)
        for i in (1, 0):
            add_food_at(proxy_user, days_ago=i, kcal=1900)
        body = client_api.get(URL_RETURNING_SUCCESS, **headers).json()["data"]
        assert body["detected"] is False
        assert body["failure_streak_days"] == 0
        assert body["recovery_days"] == 0

    def test_6_7_cache_persists(
        self, client_api, headers, proxy_user, profile_default, add_food_at,
    ):
        """6.7 — повтор GET возвращает кешированный результат.

        Two-pronged check (mirrors §5.12 fix): confirm the cache key is
        populated, then write data that *would* flip detected=True on a
        fresh recompute, and assert the cached snapshot still wins.
        """
        from django.core.cache import cache
        from nutrition.services.returning_success_service import (
            detect_returning_success,
        )

        client_api.get(URL_RETURNING_SUCCESS, **headers)
        cache_key = f"nutrition.returning_success:{proxy_user.id}"
        cached_snapshot = cache.get(cache_key)
        assert cached_snapshot is not None, "first call did not populate cache"
        assert cached_snapshot.detected is False

        # Inject a real returning-success signal.
        for i in (4, 3, 2):
            add_food_at(proxy_user, days_ago=i, kcal=800)
        for i in (1, 0):
            add_food_at(proxy_user, days_ago=i, kcal=1900)
        # Sanity: forced recompute would now detect.
        fresh = detect_returning_success(user_id=proxy_user.id, force=True)
        assert fresh.detected is True
        cache.set(cache_key, cached_snapshot, 12 * 60 * 60)
        body = client_api.get(URL_RETURNING_SUCCESS, **headers).json()["data"]
        assert body["detected"] is False  # cached "no signal" wins


# ===========================================================================
# §7 — DRF-306 Webhook outbox + delivery
# ===========================================================================


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    def __init__(self, responder):
        self._responder = responder
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, content=None, headers=None):
        self.calls.append({"url": url, "content": content, "headers": headers})
        return self._responder(url, content, headers)


@pytest.fixture
def webhook_on(settings):
    settings.NUTRITION_WEBHOOK_URL = "https://maxbot.example/api/maxbot/ayla-events/"
    settings.NUTRITION_WEBHOOK_SECRET = "smoke-secret"


class TestSection7WebhookOutbox:
    def test_7_1_url_empty_is_no_op_on_enqueue(
        self, settings, profile_default, client_api, headers,
    ):
        """7.1 — URL пустой → enqueue не создаёт строку."""
        settings.NUTRITION_WEBHOOK_URL = ""
        client_api.post(URL_PROFILE, {
            "gender": "female", "age": 40, "height_cm": 165, "weight_kg": 70.0,
            "goal": "maintain",
        }, format="json", **headers)
        assert NutritionOutboxEvent.objects.count() == 0

    def test_7_2_profile_post_enqueues_event(
        self, webhook_on, profile_default, client_api, headers,
    ):
        """7.2 — POST profile создаёт pending event."""
        client_api.post(URL_PROFILE, {
            "gender": "female", "age": 40, "height_cm": 165, "weight_kg": 70.0,
            "goal": "maintain",
        }, format="json", **headers)
        rows = NutritionOutboxEvent.objects.filter(
            topic=NutritionOutboxEvent.Topic.PROFILE_UPDATED,
        )
        assert rows.count() == 1
        assert rows.first().status == NutritionOutboxEvent.Status.PENDING

    def test_7_3_worker_delivers_2xx_and_marks_delivered(self, webhook_on):
        """7.3 — 2xx → delivered, delivered_at заполнен."""
        ev = NutritionOutboxEvent.objects.create(
            topic=NutritionOutboxEvent.Topic.WATER_LOGGED,
            external_user_id="bot:1", payload={"k": 1},
        )
        client = _FakeClient(lambda *a: _FakeResponse(200))
        with patch("nutrition.webhook_delivery.httpx.Client", return_value=client):
            counters = deliver_outbox_events_task()
        ev.refresh_from_db()
        assert ev.status == NutritionOutboxEvent.Status.DELIVERED
        assert ev.delivered_at is not None
        assert counters["delivered"] == 1

    def test_7_4_hmac_signature_matches_shared_secret(self, webhook_on):
        """7.4 — X-Signature совпадает с hmac.new(SECRET, body)."""
        NutritionOutboxEvent.objects.create(
            topic=NutritionOutboxEvent.Topic.WATER_LOGGED,
            external_user_id="bot:1", payload={"k": 1},
        )
        captured = {}

        def responder(url, content, headers):
            captured["body"] = content
            captured["sig"] = headers["X-Signature"]
            return _FakeResponse(200)
        client = _FakeClient(responder)
        with patch("nutrition.webhook_delivery.httpx.Client", return_value=client):
            deliver_outbox_events_task()
        expected = compute_signature("smoke-secret", captured["body"])
        assert captured["sig"] == expected
        assert captured["sig"].startswith("sha256=")

    def test_7_5_x_event_id_header_stable_across_retries(self, webhook_on):
        """7.5 — X-Event-Id одинаковый при повторе (для receiver-side dedup)."""
        ev = NutritionOutboxEvent.objects.create(
            topic=NutritionOutboxEvent.Topic.WATER_LOGGED,
            external_user_id="bot:1", payload={"k": 1},
        )
        seen_ids: list[str] = []

        def responder(url, content, headers):
            seen_ids.append(headers["X-Event-Id"])
            # First call 5xx (retry), second 200 (delivery).
            return _FakeResponse(500 if len(seen_ids) == 1 else 200)
        client = _FakeClient(responder)
        with patch("nutrition.webhook_delivery.httpx.Client", return_value=client):
            deliver_outbox_events_task()  # 1st attempt → schedules retry
            ev.refresh_from_db()
            ev.next_retry_at = datetime.now(dt_tz.utc) - timedelta(seconds=1)
            ev.save()
            deliver_outbox_events_task()  # 2nd attempt
        assert len(seen_ids) == 2
        assert seen_ids[0] == seen_ids[1] == str(ev.id)

    def test_7_6_5xx_schedules_backoff_retry(self, webhook_on):
        """7.6 — 5xx → status=pending, next_retry_at сдвинут на BACKOFF_SECONDS[0]."""
        ev = NutritionOutboxEvent.objects.create(
            topic=NutritionOutboxEvent.Topic.WATER_LOGGED,
            external_user_id="bot:1", payload={"k": 1},
        )
        client = _FakeClient(lambda *a: _FakeResponse(503, "down"))
        with patch("nutrition.webhook_delivery.httpx.Client", return_value=client):
            deliver_outbox_events_task()
        ev.refresh_from_db()
        assert ev.status == NutritionOutboxEvent.Status.PENDING
        assert ev.retry_count == 1
        delta = (ev.next_retry_at - datetime.now(dt_tz.utc)).total_seconds()
        assert -1 < delta - BACKOFF_SECONDS[0] < 5

    def test_7_7_dlq_after_max_retries(self, webhook_on, caplog):
        """7.7 — после MAX_RETRIES status=dlq, лог nutrition.webhook.dlq."""
        ev = NutritionOutboxEvent.objects.create(
            topic=NutritionOutboxEvent.Topic.WATER_LOGGED,
            external_user_id="bot:1", payload={"k": 1},
            retry_count=MAX_RETRIES - 1,
        )
        client = _FakeClient(lambda *a: _FakeResponse(500))
        with caplog.at_level("WARNING", logger="nutrition.webhook_delivery"):
            with patch("nutrition.webhook_delivery.httpx.Client", return_value=client):
                deliver_outbox_events_task()
        ev.refresh_from_db()
        assert ev.status == NutritionOutboxEvent.Status.DLQ
        assert any("nutrition.webhook.dlq" in r.getMessage() for r in caplog.records)

    def test_7_8_admin_requeue_action_resets_dlq_to_pending(self, webhook_on):
        """7.8 — действие admin requeue сбрасывает status/retry_count."""
        from django.contrib.admin.sites import AdminSite
        from nutrition.admin import NutritionOutboxEventAdmin
        ev = NutritionOutboxEvent.objects.create(
            topic=NutritionOutboxEvent.Topic.WATER_LOGGED,
            external_user_id="bot:1", payload={"k": 1},
            status=NutritionOutboxEvent.Status.DLQ,
            retry_count=MAX_RETRIES,
            last_error="boom",
        )
        admin = NutritionOutboxEventAdmin(NutritionOutboxEvent, AdminSite())
        # message_user just stashes message; mock it to avoid request setup.
        request = MagicMock()
        admin.requeue_dlq(request, NutritionOutboxEvent.objects.filter(id=ev.id))
        ev.refresh_from_db()
        assert ev.status == NutritionOutboxEvent.Status.PENDING
        assert ev.retry_count == 0
        assert ev.last_error == ""


# ===========================================================================
# §8 — Cross-feature
# ===========================================================================


class TestSection8CrossFeature:
    def test_8_1_profile_timezone_loaded_into_water_context(
        self, client_api, headers, proxy_user, seed_beverages,
    ):
        """8.1 — profile.timezone="Europe/Moscow" пробрасывается в WaterContext."""
        NutritionProfile.objects.create(
            user=proxy_user, timezone="Europe/Moscow",
            daily_water_ml=2000, daily_kcal=2000, daily_protein_g=100,
        )
        from nutrition.services.water_entry_service import _load_nutrition_context
        ctx = _load_nutrition_context(proxy_user.id)
        assert ctx.timezone is not dt_tz.utc
        # ZoneInfo objects expose key under .key attribute.
        key = getattr(ctx.timezone, "key", None)
        assert key == "Europe/Moscow", f"unexpected tz: {ctx.timezone!r}"

    def test_8_2_profile_change_updates_water_norm(
        self, client_api, headers, proxy_user, seed_beverages,
    ):
        """8.2 — после смены профиля POST /water/ использует новую норму.

        Verifies the WaterContext loader actually re-reads the profile
        rather than caching a stale daily_water_ml.
        """
        client_api.post(URL_PROFILE, {
            "gender": "female", "age": 40, "height_cm": 165, "weight_kg": 70.0,
            "goal": "maintain",
        }, format="json", **headers)
        before = NutritionProfile.objects.get(user=proxy_user).daily_water_ml
        assert before > 0  # recompute happened on initial POST

        # Switch goal — recompute must run again.
        client_api.post(URL_PROFILE, {"goal": "lose"}, format="json", **headers)
        after = NutritionProfile.objects.get(user=proxy_user).daily_water_ml
        assert after > 0

        # The endpoint must surface the *current* norm — not a cached one.
        water_resp = client_api.post(
            URL_WATER, {"ml": 250}, format="json", **headers,
        )
        assert water_resp.json()["data"]["today_norm_water_ml"] == after

    def test_8_3_scan_caption_then_summary_with_comment_round_trip(
        self, client_api, headers, proxy_user, settings, patch_openai_client,
        make_profile,
    ):
        """8.3 — caption → scan → summary с AI-комментарием не падает."""
        make_profile(goal="lose")
        settings.OPENAI_API_KEY = "test"
        msg = MagicMock()
        msg.content = (
            '{"dish_name":"x","confidence":0.9,'
            '"portion_g":100,"ingredients":[]}'
        )
        choice = MagicMock()
        choice.message = msg
        patch_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[choice],
        )
        client_api.post(
            URL_SCAN,
            {"image": ("test.png", _png_bytes(), "image/png"),
             "caption": "не наелась"},
            format="multipart", **headers,
        )
        # The summary AI-comment uses a separate LLM client factory.
        comment_client = MagicMock()
        comment_msg = MagicMock()
        comment_msg.content = "Хорошее начало дня."
        comment_choice = MagicMock()
        comment_choice.message = comment_msg
        comment_client.return_value.chat.completions.create.return_value = MagicMock(
            choices=[comment_choice],
        )
        with patch("ai.services.llm_client.get_openai_client", comment_client):
            resp = client_api.get(
                URL_SUMMARY, {"with_comment": "true"}, **headers,
            )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["data"]["ai_comment"]

    def test_8_4_multiple_patterns_simultaneously(
        self, client_api, headers, proxy_user, profile_default,
        add_food_at, add_water_at,
    ):
        """8.4 — несколько детекторов могут сработать в одном response."""
        # low_protein: 5 days low protein
        for i in range(5):
            add_food_at(proxy_user, days_ago=i, kcal=400, protein_g=30)
        # late_dinner: 3 days >21:00
        for i in range(3):
            add_food_at(proxy_user, days_ago=i, hour=22, kcal=500)
        resp = client_api.get(URL_PATTERNS, **headers)
        slugs = {p["slug"] for p in resp.json()["data"]["patterns"]}
        # At least 2 active.
        assert len(slugs) >= 2


# ===========================================================================
# §9 — Observability subset (rate-limit + helpers)
# ===========================================================================


class TestSection9Observability:
    def test_9_2_purge_task_returns_count(
        self, proxy_user, profile_default,
    ):
        """9.2 — purge task деудаляет строки старше 90 дней."""
        WaterEntry.objects.create(
            user=proxy_user,
            ts=datetime.now(dt_tz.utc) - timedelta(days=120),
            ml=250, water_ml=250.0,
            deleted_at=datetime.now(dt_tz.utc) - timedelta(days=100),
            deleted_reason=WaterEntry.DeletedReason.USER_UNDO,
        )
        purged = purge_deleted_water_entries_task()
        assert purged == 1

    def test_9_3_deliver_task_no_op_when_url_empty(self, settings):
        """9.3 — deliver task — no-op без NUTRITION_WEBHOOK_URL."""
        settings.NUTRITION_WEBHOOK_URL = ""
        counters = deliver_outbox_events_task()
        assert counters == {"delivered": 0, "retried": 0, "dlq": 0, "skipped": 0}

    def test_9_4_throttle_food_scan_internal_scope_enforced(
        self, override_throttle_rate, client_api, headers,
        profile_default, seed_beverages,
    ):
        """9.4 — после превышения food_scan_internal scope — 429."""
        override_throttle_rate("food_scan_internal", "3/min")
        statuses = []
        for _ in range(5):
            r = client_api.get(URL_BEVERAGES, **headers)
            statuses.append(r.status_code)
        assert status.HTTP_429_TOO_MANY_REQUESTS in statuses

    def test_9_5_profile_endpoint_under_same_throttle_scope(
        self, override_throttle_rate, client_api, headers, proxy_user,
    ):
        """9.5 — /internal/profile/ под тем же throttle scope."""
        override_throttle_rate("food_scan_internal", "3/min")
        statuses = []
        for _ in range(5):
            r = client_api.get(URL_PROFILE, **headers)
            statuses.append(r.status_code)
        assert status.HTTP_429_TOO_MANY_REQUESTS in statuses


@pytest.fixture
def override_throttle_rate(settings):
    """Override a DRF throttle rate with proper teardown.

    Plain ``settings.REST_FRAMEWORK[...][scope] = rate`` mutates a nested
    dict pytest-django doesn't restore — only the top-level REST_FRAMEWORK
    key is snapshotted. We mutate in place (so DRF's ``api_settings`` —
    which caches a reference to the old dict — actually picks up the new
    rate) and explicitly restore the original value in teardown.
    """
    from rest_framework.throttling import SimpleRateThrottle
    rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    saved: dict[str, str] = {}

    def _apply(scope: str, rate: str) -> None:
        if scope not in saved:
            saved[scope] = rates.get(scope)
        rates[scope] = rate
        SimpleRateThrottle.cache.clear()

    yield _apply

    for scope, original in saved.items():
        if original is None:
            rates.pop(scope, None)
        else:
            rates[scope] = original
    SimpleRateThrottle.cache.clear()
