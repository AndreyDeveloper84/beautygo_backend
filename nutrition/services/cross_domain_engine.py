"""DRF-263 — Cross-domain rule engine.

Selects a top-1 cross-domain recommendation for a user given their
active patterns (DRF-262). Hybrid cooldown (PO-approved 2026-05-05):

- Per-rule:      30 days
- Per-trigger:   14 days (any rule with same nutrition_trigger)
- Per-category:  7 days  (any rule with same service_category_slug)
- Global cap:    1 in 3 days (any rule whatsoever)
- Skip extends:  +7 days on dismissed
- Double skip:   60-day pause after two dismissals

Auto-confirm-5min: a CrossDomainShownRule with ``seen_at IS NULL`` but
``shown_at + 5min < now`` counts as "seen" — defends cooldowns against
client crashes between GET and POST /seen/.

Eating-disorder mode: ALWAYS returns None. The pattern engine already
suppresses micronutrient deficits for ED users; the cross-domain
engine adds a second guard so a stray pattern can't surface a
recommendation.

The engine creates a ``CrossDomainShownRule`` row at evaluation time
(PO-approved 2026-05-05: inline cooldown bookkeeping starts on GET,
not on /seen/). Auto-confirm covers the case where the surface fails
to confirm.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.db import transaction

from nutrition.models import (
    CrossDomainRule,
    CrossDomainShownRule,
    NutritionProfile,
)
from nutrition.services.pattern_detection_service import (
    DetectedPattern,
    detect_patterns,
)


# Tunables (cooldown defaults live on CrossDomainRule itself; these are
# global to the engine).
GLOBAL_CAP_DAYS = 3
AUTO_CONFIRM_MINUTES = 5

# Score boosts for ranking.
FAVORITE_SPECIALIST_BOOST = 1.5


def _user_in_rollout_allowlist(user) -> bool:
    """DRF-288 — gate access to cross-domain recommendations.

    Returns True when:
    - ``settings.CROSS_DOMAIN_ENABLED`` is True (global rollout, all users), OR
    - ``user.username`` matches an entry in ``settings.CROSS_DOMAIN_INTERNAL_USERNAMES``.

    The latter list usually holds proxy usernames like ``bot:NNN`` —
    matches the maxbot side gate (``CROSS_DOMAIN_INTERNAL_ACCOUNTS``)
    one-for-one. If the global flag is off and the user is not in the
    list, the engine returns None without writing any state.
    """
    if getattr(settings, "CROSS_DOMAIN_ENABLED", False):
        return True
    allowlist = getattr(settings, "CROSS_DOMAIN_INTERNAL_USERNAMES", []) or []
    if not allowlist:
        return False
    return getattr(user, "username", None) in set(allowlist)


@dataclass(frozen=True)
class CrossDomainRecommendation:
    """Engine output. Mirrors what the bot card / mobile UI render."""

    shown_id: str
    rule_id: str
    nutrition_trigger: str
    service_category_slug: str
    insight_text: str
    rationale_text: str
    disclaimer_text: str
    data_points: int
    severity: str


class CrossDomainEngine:
    """Selects top-1 recommendation, writes a shown row, returns DTO."""

    def evaluate(
        self, *, user, surface: str = "bot",
    ) -> CrossDomainRecommendation | None:
        # DRF-288 — internal-only gate. Until the global rollout flag
        # ``CROSS_DOMAIN_ENABLED`` flips True, only users in the
        # internal allowlist see recommendations. Other users short-
        # circuit before any DB writes (no CrossDomainShownRule created,
        # no detect_patterns cache work). Bot's mirror gate also skips
        # the GET, but we fail-closed here for safety.
        if not _user_in_rollout_allowlist(user):
            return None

        # Eating-disorder gate — global, no exceptions.
        profile = NutritionProfile.objects.filter(user=user).first()
        flags = (profile.health_flags or {}) if profile else {}
        if flags.get("eating_disorder"):
            return None

        # Pull active patterns. detect_patterns is cached for 12h.
        result = detect_patterns(user_id=user.id)
        if not result.patterns:
            return None
        active_triggers = {p.slug: p for p in result.patterns}

        # Engine candidates: rules whose trigger matches AND that pass
        # all gates. Score and pick top-1.
        now = datetime.now(timezone.utc)
        history = self._load_history(user, days=60)

        # Global cap (any rule shown in last 3 days, considering
        # auto-confirm). Single check before per-rule iteration.
        if self._global_cap_blocked(history, now=now):
            return None

        candidates: list[tuple[CrossDomainRule, float, DetectedPattern]] = []

        rules = CrossDomainRule.objects.filter(
            is_active=True, legal_reviewed=True,
        )
        for rule in rules:
            pattern = active_triggers.get(rule.nutrition_trigger)
            if pattern is None:
                continue

            # min_data_points gate — protects against single-fluke
            # activations that would burn a 30-day cooldown.
            if pattern.count < rule.min_data_points:
                continue

            if self._violates_health_exclusion(rule, flags):
                continue

            if rule.requires_premium and not getattr(user, "is_premium", False):
                continue

            ok, _reason = self._cooldown_ok(rule, history, now=now)
            if not ok:
                continue

            if not self._has_supply(rule, user):
                continue

            score = self._score(rule, user)
            candidates.append((rule, score, pattern))

        if not candidates:
            return None

        # Top-1 by score (highest first). Ties broken by rule_id for
        # deterministic output across replicas.
        candidates.sort(key=lambda x: (-x[1], x[0].rule_id))
        chosen_rule, _, chosen_pattern = candidates[0]

        # Inline shown bookkeeping (PO-approved: bookkeeping at GET,
        # not at /seen/). The auto-confirm logic in cooldown reads this.
        shown = self._record_shown(
            user=user, rule=chosen_rule, pattern=chosen_pattern,
            surface=surface, now=now,
        )

        return CrossDomainRecommendation(
            shown_id=str(shown.id),
            rule_id=chosen_rule.rule_id,
            nutrition_trigger=chosen_rule.nutrition_trigger,
            service_category_slug=chosen_rule.service_category_slug,
            insight_text=chosen_rule.insight_text_template,
            rationale_text=chosen_rule.rationale_text,
            disclaimer_text=chosen_rule.disclaimer_text,
            data_points=chosen_pattern.count,
            severity=chosen_pattern.severity,
        )

    # ------------------------------------------------------------------
    # Cooldown logic
    # ------------------------------------------------------------------

    def _load_history(self, user, *, days: int) -> list:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return list(
            CrossDomainShownRule.objects
            .filter(user=user, shown_at__gte=cutoff)
            .select_related("rule")
            .order_by("-shown_at")
        )

    @staticmethod
    def _effective_seen_at(row: CrossDomainShownRule) -> datetime:
        """``seen_at`` if present; otherwise auto-confirm 5 min after shown."""
        if row.seen_at is not None:
            return row.seen_at
        return row.shown_at + timedelta(minutes=AUTO_CONFIRM_MINUTES)

    def _global_cap_blocked(self, history: list, *, now: datetime) -> bool:
        for row in history:
            seen = self._effective_seen_at(row)
            if seen > now:
                # Row is still in the auto-confirm window — don't count
                # it as a "showing" yet. The 1-min-old in-flight row
                # case from the test.
                continue
            if (now - seen).days < GLOBAL_CAP_DAYS:
                return True
        return False

    def _cooldown_ok(
        self, rule: CrossDomainRule, history: list, *, now: datetime,
    ) -> tuple[bool, str]:
        """Return (allowed, reason). reason='' when allowed."""
        same_rule = [
            h for h in history if h.rule_id == rule.id
        ]
        same_trigger = [
            h for h in history if h.nutrition_trigger == rule.nutrition_trigger
        ]
        same_category = [
            h for h in history
            if h.service_category_slug == rule.service_category_slug
        ]

        # Per-rule + skip extension.
        if same_rule:
            # DRF-263 LB-7: previously the recency window was hard-coded
            # to 30 days, but the pause itself is 60 — so a second
            # dismissal at day 35 escaped the count and the 60-day pause
            # never triggered. Use double_skip_pause_days as the window
            # so the two values stay coupled.
            window_days = rule.double_skip_pause_days
            recent_dismissals = [
                h for h in same_rule
                if h.user_action == "dismissed"
                and (now - self._effective_seen_at(h)).days < window_days
            ]
            # Double-skip pause: ``double_skip_pause_days`` after two
            # dismissals within the same window.
            if len(recent_dismissals) >= 2:
                last = max(self._effective_seen_at(h) for h in recent_dismissals)
                if (now - last).days < rule.double_skip_pause_days:
                    return False, "double_skip_pause"

            last_seen = max(self._effective_seen_at(h) for h in same_rule)
            cooldown = rule.cooldown_rule_days
            # Skip extends rule cooldown by skip_extend_days.
            if same_rule[0].user_action == "dismissed":
                cooldown += rule.skip_extend_days
            if (now - last_seen).days < cooldown:
                return False, "rule_cooldown"

        # Per-trigger.
        if same_trigger:
            last = max(self._effective_seen_at(h) for h in same_trigger)
            if (now - last).days < rule.cooldown_trigger_days:
                return False, "trigger_cooldown"

        # Per-category.
        if same_category:
            last = max(self._effective_seen_at(h) for h in same_category)
            if (now - last).days < rule.cooldown_category_days:
                return False, "category_cooldown"

        return True, ""

    # ------------------------------------------------------------------
    # Other gates
    # ------------------------------------------------------------------

    @staticmethod
    def _violates_health_exclusion(
        rule: CrossDomainRule, flags: dict,
    ) -> bool:
        for excluded in rule.excluded_health_flags or []:
            if flags.get(excluded):
                return True
        return False

    def _has_supply(self, rule: CrossDomainRule, user) -> bool:
        """At least one Service in ``service_category_slug`` exists.

        For Penza pilot we accept any active service (no city filter on
        the queryset until tenants are tied to the user). When we have
        UserPersonalContext.city, scope by location here.
        """
        from services.models import Service
        return Service.objects.filter(
            category__slug=rule.service_category_slug,
            is_active=True,
        ).exists()

    def _score(self, rule: CrossDomainRule, user) -> float:
        """Priority + favorite-specialist boost.

        Higher = better. Base priority is 1.0; favorite-specialist
        match (when supply check finds the user's favorite) bumps to
        1.5. Ties broken by rule_id for determinism.
        """
        score = 1.0

        # Favorite-specialist boost: if any Service in the rule's
        # category is owned by a specialist the user has favorited,
        # that's a strong relevance signal.
        try:
            from services.models import Service
            from users.models import FavoriteSpecialist
            fav_ids = FavoriteSpecialist.objects.filter(
                client=user,
            ).values_list("specialist_id", flat=True)
            if fav_ids and Service.objects.filter(
                category__slug=rule.service_category_slug,
                specialist_id__in=fav_ids,
                is_active=True,
            ).exists():
                score *= FAVORITE_SPECIALIST_BOOST
        except Exception:
            # Defensive — never let scoring crash the engine.
            pass

        return score

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------

    @transaction.atomic
    def _record_shown(
        self, *, user, rule, pattern, surface: str, now: datetime,
    ) -> CrossDomainShownRule:
        return CrossDomainShownRule.objects.create(
            user=user, rule=rule,
            nutrition_trigger=rule.nutrition_trigger,
            service_category_slug=rule.service_category_slug,
            shown_at=now,
            surface=surface,
        )
