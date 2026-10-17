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
        templates=[template], key=key, text_client="Выравнивает тон кожи", **kw
    )


def _approve(row, curator, **extra) -> None:
    for name, value in {
        "status": S.APPROVED,
        "evidence_kind": "professional_consensus",  # DRF-2742: вид из закрытого списка
        "claim_type": "product",  # DRF-2726: тип без рецензента
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
        # DRF-2726: у подтверждённого запрета обязателен предмет запрета.
        statement = {"prohibited_statement": "синтетика"} if scope == C.PROHIBITED_CLAIM else {}
        _approve(_capability(template, key="not-said"), curator, claim_scope=scope, **statement)

        keys = [c.key for c in client_facing_capabilities(template).capabilities]

        assert keys == ["said"]

    def test_validity_ends_exactly_at_valid_until(self, template, curator) -> None:
        now = timezone.now()
        _approve(_capability(template, key="edge"), curator, valid_until=now)
        assert client_facing_capabilities(template, now=now - timedelta(seconds=1)).state is (
            KnowledgeState.KNOWN
        )
        assert client_facing_capabilities(template, now=now).state is KnowledgeState.UNKNOWN

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


class TestProvenanceShapes:
    """Ревью: провенанс — все четыре части, и «правило» так же законно, как «человек»."""

    def test_a_named_rule_is_as_good_as_a_named_person(self, template) -> None:
        row = _capability(template)
        _approve(row, None, confirmed_rule="owner_confirmed_list", rule_version="1")
        assert client_facing_capabilities(template).state is KnowledgeState.KNOWN

    @pytest.mark.parametrize("missing", ["confirmed_at", "source_ref"])
    def test_approved_without_date_or_basis_is_refused(self, template, curator, missing) -> None:
        row = _capability(template)
        row.status = S.APPROVED
        row.claim_scope = C.SUPPORTED
        row.confirmed_by = curator
        row.confirmed_at = None if missing == "confirmed_at" else timezone.now()
        row.source_ref = "" if missing == "source_ref" else "x"
        with pytest.raises(IntegrityError), transaction.atomic():
            row.save()

    def test_the_goal_link_carries_the_same_constraint(self, template, goal) -> None:
        link = CapabilityGoalLink(
            capability=_capability(template), goal=goal, status=S.APPROVED, claim_type="product",
            evidence_kind="professional_consensus",
            claim_scope=C.SUPPORTED, confirmed_at=timezone.now(), source_ref="x",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            link.save()


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
        said = _capability(template, key="said")
        _approve(said, curator)
        _approve(CapabilityGoalLink.objects.create(capability=said, goal=goal), curator)
        unsaid = _capability(template, key="unsaid")  # inference
        _approve(CapabilityGoalLink.objects.create(capability=unsaid, goal=goal), curator)

        assert len(client_facing_goal_links(said)) == 1
        assert client_facing_goal_links(unsaid) == ()

    def test_a_stale_in_memory_capability_does_not_open_its_links(
        self, template, curator, goal
    ) -> None:
        """Ревью: связь проверяет возможность ПО БАЗЕ, а не по объекту в руках."""
        capability = _capability(template)
        _approve(capability, curator)
        _approve(CapabilityGoalLink.objects.create(capability=capability, goal=goal), curator)
        assert len(client_facing_goal_links(capability)) == 1

        # DRF-2879: содержание подтверждённой строки меняется только вместе
        # с новым подтверждением — отсюда свежая дата в той же записи.
        ProcedureCapability.objects.filter(pk=capability.pk).update(
            claim_scope=C.PROHIBITED_CLAIM, prohibited_statement="синтетика",  # DRF-2726
            confirmed_at=timezone.now() + timedelta(seconds=1),
        )

        assert client_facing_goal_links(capability) == ()  # capability in memory still says SUPPORTED

    @pytest.mark.parametrize(
        "change",
        [
            {"claim_scope": C.NOT_SUPPORTED},
            {"claim_scope": C.PROHIBITED_CLAIM, "prohibited_statement": "синтетика"},  # DRF-2726
            {"valid_until": "past"},
            {"goal_inactive": True},
        ],
        ids=["not_supported", "prohibited", "expired", "goal_inactive"],
    )
    def test_a_link_is_dropped_by_its_own_evidence_or_an_inactive_goal(
        self, template, curator, goal, change
    ) -> None:
        capability = _capability(template)
        _approve(capability, curator)
        link = CapabilityGoalLink.objects.create(capability=capability, goal=goal)
        _approve(link, curator)
        assert len(client_facing_goal_links(capability)) == 1  # positive side

        if change.get("goal_inactive"):
            GoalOption.objects.filter(pk=goal.pk).update(is_active=False)
        elif change.get("valid_until") == "past":
            CapabilityGoalLink.objects.filter(pk=link.pk).update(
                valid_until=timezone.now() - timedelta(seconds=1)
            )
        else:
            # DRF-2879: правка содержания — с новым подтверждением в той же записи.
            CapabilityGoalLink.objects.filter(pk=link.pk).update(
                **change, confirmed_at=timezone.now() + timedelta(seconds=1),
            )

        assert client_facing_goal_links(capability) == ()


class TestStableIdentity:
    """Приёмка владельца §9.1: ключ — идентичность смысла, не производная от текста."""

    def test_editing_the_wording_does_not_change_the_key(self, template, curator) -> None:
        row = _capability(template, key="temporary_relaxation")
        _approve(row, curator)

        row.text_client = "Помогает расслабиться — формулировка переписана редактором"
        row.text_professional = "Снижение мышечного тонуса"
        # DRF-2879: переписанная формулировка подтверждена заново — без этого
        # база не даст подтверждённой строке нести другое содержание.
        row.confirmed_at = timezone.now() + timedelta(seconds=1)
        row.save()
        row.refresh_from_db()

        assert row.key == "temporary_relaxation"
        assert [c.key for c in client_facing_capabilities(template).capabilities] == [
            "temporary_relaxation"
        ]


class TestTrustBoundaryByName:
    """Приёмка §9.2 тем же словом, что у владельца: system_inference."""

    def test_new_knowledge_starts_as_system_inference_and_is_not_said(self, template) -> None:
        row = _capability(template, claim_scope=C.SUPPORTED)
        assert row.status == S.SYSTEM_INFERENCE == "system_inference"
        assert client_facing_capabilities(template).state is KnowledgeState.UNKNOWN


class TestSafeCourseStorage:
    """Приёмка §9.4: курс не сохраняется голым; min/max не требуется."""

    def _link(self, template, goal, **course) -> CapabilityGoalLink:
        return CapabilityGoalLink(capability=_capability(template), goal=goal, **course)

    def test_a_worded_course_with_a_note_and_evidence_is_stored_without_min_max(
        self, template, goal
    ) -> None:
        link = self._link(
            template,
            goal,
            course_pattern="Обычно рассматривается как курс сеансов",
            variability_note="Длительность зависит от исходного состояния и выбранного результата",
            evidence_source="Протокол салона",
            source_ref="owner-2606",
        )
        link.save()
        link.refresh_from_db()
        assert link.course_pattern.startswith("Обычно")

    @pytest.mark.parametrize(
        "course",
        [
            {"course_pattern": "Курс сеансов", "evidence_source": "x", "source_ref": "x"},
            {"course_pattern": "Курс сеансов", "variability_note": "зависит", "source_ref": "x"},
            {"course_pattern": "Курс сеансов", "variability_note": "зависит", "evidence_source": "x"},
            {
                "course_pattern": " 10 ",
                "variability_note": "зависит",
                "evidence_source": "x",
                "source_ref": "x",
            },
        ],
        ids=["no_variability_note", "no_evidence_source", "no_source_ref", "bare_number"],
    )
    def test_a_course_without_note_or_evidence_or_as_a_bare_number_is_refused(
        self, template, goal, course
    ) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            self._link(template, goal, **course).save()
