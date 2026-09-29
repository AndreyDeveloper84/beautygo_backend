"""DRF-2606 — возможность процедуры и её связь с целью: носитель и провенанс.

Решение владельца 29.09: «Capability описывает возможность процедуры, связь с
целью — отдельной таблицей». Узлы — на пары, которые обязаны различаться:
«читается» прошло бы и при выводе системы, и при подтверждённом.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from services.capabilities import (
    KnowledgeState,
    client_facing_capabilities,
    client_facing_goal_links,
)
from services.models import (
    CapabilityGoalLink,
    ClaimEvidence,
    GoalOption,
    ProcedureCapability,
    ServiceCategory,
    ServiceTemplate,
)

pytestmark = pytest.mark.django_db

S = ClaimEvidence.Status
C = ClaimEvidence.ClaimScope


@pytest.fixture
def template() -> ServiceTemplate:
    category = ServiceCategory.objects.create(name="Уход за лицом", slug="face-2606")
    return ServiceTemplate.objects.create(
        category=category, name="Пилинг", name_short="Пилинг", duration_default=45
    )


@pytest.fixture
def curator():
    return get_user_model().objects.create_user(username="curator-2606", password="x")


@pytest.fixture
def goal() -> GoalOption:
    return GoalOption.objects.create(key="skin-care-2606", label="Уход за кожей")


def _capability(template, *, key="even-tone", **kw) -> ProcedureCapability:
    return ProcedureCapability.objects.create(
        template=template, key=key, text_client="Выравнивает тон кожи", **kw
    )


def _approve(row, curator, **extra) -> None:
    for name, value in {
        "status": S.APPROVED,
        "claim_scope": C.SUPPORTED,
        "confirmed_by": curator,
        "confirmed_at": timezone.now(),
        "source_ref": "owner-review-2606",
        **extra,
    }.items():
        setattr(row, name, value)
    row.save()


class TestInferenceIsNotSaid:
    """Главный узел листа: вывод системы ≠ подтверждённое человеком."""

    def test_the_same_row_is_said_only_after_a_human_confirms_it(
        self, template, curator
    ) -> None:
        row = _capability(template, claim_scope=C.SUPPORTED)  # status: inference

        before = client_facing_capabilities(template)
        _approve(row, curator)
        after = client_facing_capabilities(template)

        assert before.state is KnowledgeState.UNKNOWN
        assert before.capabilities == ()
        assert after.state is KnowledgeState.KNOWN
        assert [c.key for c in after.capabilities] == ["even-tone"]


class TestUnknownIsNotPermission:
    def test_a_procedure_without_capabilities_is_unknown_not_an_empty_answer(
        self, template, curator
    ) -> None:
        # Положительная сторона впереди: у второй процедуры знание есть.
        known = ServiceTemplate.objects.create(
            category=template.category, name="Массаж лица", name_short="Массаж",
            duration_default=30,
        )
        _approve(_capability(known), curator)

        assert client_facing_capabilities(known).state is KnowledgeState.KNOWN
        readout = client_facing_capabilities(template)
        assert readout.state is KnowledgeState.UNKNOWN


class TestOnlySupportedClaimsAreSaid:
    @pytest.mark.parametrize("scope", [C.NOT_SUPPORTED, C.PROHIBITED_CLAIM])
    def test_confirmed_but_unsupported_or_prohibited_is_never_said(
        self, template, curator, scope
    ) -> None:
        _approve(_capability(template, key="said"), curator)
        _approve(_capability(template, key="not-said"), curator, claim_scope=scope)

        keys = [c.key for c in client_facing_capabilities(template).capabilities]

        assert keys == ["said"]

    def test_an_expired_confirmation_is_not_said(self, template, curator) -> None:
        _approve(_capability(template, key="fresh"), curator)
        _approve(
            _capability(template, key="stale"),
            curator,
            valid_until=timezone.now() - timedelta(days=1),
        )

        keys = [c.key for c in client_facing_capabilities(template).capabilities]

        assert keys == ["fresh"]


class TestProvenanceIsRequired:
    def test_approved_without_author_or_rule_is_refused_by_the_database(
        self, template, curator
    ) -> None:
        # Положительная сторона: с автором — сохраняется.
        _approve(_capability(template, key="ok"), curator)

        row = _capability(template, key="orphan")
        row.status = S.APPROVED
        row.claim_scope = C.SUPPORTED
        row.confirmed_at = timezone.now()
        row.source_ref = "x"
        with pytest.raises(IntegrityError), transaction.atomic():
            row.save()


class TestGoalLinkHasItsOwnEvidence:
    """Два утверждения — два подтверждения: «умеет X» и «X помогает цели Y»."""

    def test_link_is_said_only_when_both_the_capability_and_the_link_are_confirmed(
        self, template, curator, goal
    ) -> None:
        capability = _capability(template)
        link = CapabilityGoalLink.objects.create(
            capability=capability, goal=goal, claim_scope=C.SUPPORTED
        )

        _approve(capability, curator)
        only_capability = client_facing_goal_links(capability)
        _approve(link, curator)
        both = client_facing_goal_links(capability)

        assert only_capability == ()
        assert [lk.goal.key for lk in both] == ["skin-care-2606"]

    def test_a_confirmed_link_of_an_unconfirmed_capability_is_not_said(
        self, template, curator, goal
    ) -> None:
        capability = _capability(template)  # inference
        link = CapabilityGoalLink.objects.create(capability=capability, goal=goal)
        _approve(link, curator)

        assert client_facing_goal_links(capability) == ()
