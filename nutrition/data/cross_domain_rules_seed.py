"""DRF-268 — Track E cross-domain rules seed data.

Five draft rules covering the five micronutrient detectors from
DRF-262. Each rule maps a deficit pattern to a beauty-service
category whose service modifier addresses (cosmetically, not
medically) a known correlated symptom.

EVERY rule ships ``legal_reviewed=False`` and ``is_active=False``.
A curator must:

1. Get formulation reviewed by counsel (Закон «О рекламе» ст. 24)
2. Confirm ≥3 specialists in pilot city carry the category
   (run `python manage.py audit_cross_domain_supply`)
3. Flip both flags via Django admin

The five drafts are intentionally conservative:

- "часто отражается на коже" — observed correlation, not causation
- Modifier hints (argan_oil, lymph_drainage) — tie service variant
  to deficit; supply audit confirms availability
- Disclaimer is stricter than spec demands — "Это не медицинская
  рекомендация" is the floor, with a follow-up to "обратитесь к
  врачу" for chronic deficits

Add a rule:
1. Append a dict to ``TRACK_E_RULES`` with all required keys
2. Verify the trigger is one of: low_vitamin_d, low_iron,
   low_omega3, low_calcium, low_b12 (DRF-262 detectors)
3. Verify the service_category_slug matches an existing
   ServiceCategory.slug (cross-ref ``services/fixtures/categories.json``
   + DRF-259 Track E child slugs)
4. Run `python manage.py seed_cross_domain_rules`
5. Add the rule to legal review queue
"""
from __future__ import annotations


TRACK_E_RULES = [
    {
        "rule_id": "vitamin_d_deficit_to_argan_massage",
        "nutrition_trigger": "low_vitamin_d",
        "service_category_slug": "massage-argan-oil",
        "service_modifier": "argan_oil",
        "insight_text_template": (
            "За последние дни витамин D ниже нормы — это часто "
            "отражается на коже"
        ),
        "rationale_text": (
            "Массаж с аргановым маслом помогает коже получить "
            "витамин E и F"
        ),
        "disclaimer_text": (
            "Это не медицинская рекомендация. При стабильном "
            "дефиците обратитесь к врачу."
        ),
        "min_data_points": 5,
        "excluded_health_flags": ["eating_disorder"],
    },
    {
        "rule_id": "iron_deficit_to_face_glow",
        "nutrition_trigger": "low_iron",
        "service_category_slug": "cosmetology-brightening",
        "service_modifier": "vitamin_c_brightening",
        "insight_text_template": (
            "Железо ниже нормы 7+ дней — кожа может выглядеть тусклее"
        ),
        "rationale_text": (
            "Процедура vitamin C brightening может временно "
            "улучшить тон"
        ),
        "disclaimer_text": (
            "Это не лечение анемии. Нужен анализ крови — обсуди с "
            "врачом."
        ),
        "min_data_points": 7,
        "excluded_health_flags": ["eating_disorder", "pregnant"],
    },
    {
        "rule_id": "omega3_deficit_to_skin_hydration",
        "nutrition_trigger": "low_omega3",
        "service_category_slug": "cosmetology-deep-hydration",
        "service_modifier": "deep_hydration",
        "insight_text_template": (
            "Омега-3 ниже нормы 2+ недели — кожа теряет упругость "
            "быстрее"
        ),
        "rationale_text": (
            "Процедура глубокого увлажнения работает с симптомом, "
            "не причиной"
        ),
        "disclaimer_text": (
            "Долгосрочный эффект даёт диета. Это поддержка, не "
            "замена питания."
        ),
        "min_data_points": 14,
        "excluded_health_flags": ["eating_disorder"],
    },
    {
        "rule_id": "calcium_deficit_to_lymph_drainage",
        "nutrition_trigger": "low_calcium",
        "service_category_slug": "massage-lymph-drainage",
        "service_modifier": "lymph_drainage",
        "insight_text_template": (
            "Кальций низкий 2+ недели — это часто связано с "
            "отёчностью"
        ),
        "rationale_text": (
            "Лимфодренаж помогает уменьшить отёки"
        ),
        "disclaimer_text": (
            "Не заменяет коррекцию питания. При стойких отёках "
            "обратитесь к врачу."
        ),
        "min_data_points": 14,
        "excluded_health_flags": ["eating_disorder", "pregnant"],
    },
    {
        "rule_id": "b12_deficit_to_relaxation_spa",
        "nutrition_trigger": "low_b12",
        "service_category_slug": "spa-relaxation",
        "service_modifier": "relaxation",
        "insight_text_template": (
            "B12 ниже нормы 1+ неделю — может ощущаться как "
            "усталость"
        ),
        "rationale_text": (
            "Спа-процедуры помогают восстановиться, но не заменят "
            "витамин"
        ),
        "disclaimer_text": (
            "При хронической усталости обратитесь к врачу."
        ),
        "min_data_points": 7,
        "excluded_health_flags": ["eating_disorder", "pregnant"],
    },
]
