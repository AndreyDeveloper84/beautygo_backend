"""DRF-2421 — who stands behind ``IsBotServiceWithVerifiedClient`` and what second factor each has.

The permission resolves the acting person from ``X-External-User-ID`` and
proves only that the call carries the shared internal token. Its docstring
used to promise that every view cross-checks a ``client_id`` from the body;
on 05.10.2026 six of twenty-seven did. The docstring now states the census;
this file holds it.

Every concrete view guarded by the permission must sit in exactly one group:

* **A** — the view compares a user id it was sent with ``request.user.id``;
* **B** — ``IsTenantAdmin`` (or, since DRF-2826, the booking desk's
  ``IsTenantBookingDesk``) stands beside the permission;
* **C** — the header alone. Listed by name: a new view that lands here
  without being added to this list turns the build red, so «header only»
  is always a decision someone wrote down, never a default.

The groups are literals, not derived from the code under test: a census
built from the code would agree with any change to it.

Pure AST over the tree — no database, no requests.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PERMISSION = "IsBotServiceWithVerifiedClient"
HANDLERS = {"get", "post", "put", "patch", "delete"}

A_CROSS_CHECKED = {
    "appointments/internal_api.py::InternalBookingCreateView",
    "payments/views.py::InternalPaymentCreateView",
    "payments/views.py::InternalPaymentRetryView",
    "payments/views.py::InternalCardSetupView",
    "payments/views.py::InternalCardListView",
    "payments/views.py::InternalCardDeleteView",
}

B_TENANT_ADMIN = {
    "tenants/appointments_api.py::SalonBookingCreateView",
    "tenants/appointments_api.py::SalonBookingRescheduleView",
    "tenants/appointments_api.py::SalonBookingCancelView",
    "tenants/appointments_api.py::SalonBookingCompleteView",
    "tenants/appointments_api.py::SalonBookingNoShowView",
    "tenants/appointments_api.py::SalonCustomerLookupView",
}

C_HEADER_ONLY = {
    "appointments/internal_api.py::InternalBookingCancelView",
    "appointments/internal_api.py::InternalBookingRescheduleView",
    "appointments/internal_api.py::InternalAppointmentReadView",
    "appointments/records_api.py::MeBookingsListView",
    "appointments/records_api.py::MeBookingDetailView",
    "appointments/records_api.py::MeBookingRepeatIntentView",
    "goals/api.py::DecisionContextView",
    "goals/api.py::GoalSelectView",
    "goals/api.py::GoalStateView",
    "recommendation/views.py::RecommendationResolveView",
    "users/catalog_recommendations_api.py::CatalogRecommendationsView",
    "users/internal_identity_api.py::InternalMeIdentityView",
    "wellness/api.py::WellnessContextView",
    "wellness/plan_lite_api.py::PlanLiteView",
    "wellness/plan_lite_api.py::PlanLiteProposalView",
    # DRF-2857 — durable Plan: как у Plan Lite, человек берётся только из
    # заголовка; `goal_ref` и `plan_id` из тела — не user id, а строки,
    # которые писатель ищет строго среди принадлежащих `request.user`
    # (чужое → 404).
    "wellness/plan_engine_api.py::PlanEngineView",
    "wellness/plan_engine_api.py::PlanEngineStateView",
    # DRF-2857 — замена плана: `plan_id` и `replaces_plan_id` из тела — не user
    # id; оба плана ищутся строго среди планов `request.user` (чужое → 404).
    "wellness/plan_engine_api.py::PlanReplaceView",
    # DRF-2868 — шаг плана: `plan_id` и `appointment_id` из тела — не user id;
    # и план, и запись ищутся строго среди строк `request.user` (чужое → 404).
    "wellness/plan_engine_api.py::PlanStepResolutionView",
    "wellness/plan_engine_api.py::PlanStepBookingView",
    # DRF-2877 — ограничения плана: `plan_id` и `restriction_id` из тела — не
    # user id; план ищется строго среди планов `request.user` (чужое → 404),
    # ограничение — строго среди ограничений этого плана.
    "wellness/plan_engine_api.py::PlanRestrictionView",
    "wellness/plan_engine_api.py::PlanRestrictionLiftView",
    # DRF-2871 — сборка плана: в теле нет ни одного идентификатора человека
    # или его строк — цель берётся у `request.user`.
    "wellness/plan_engine_api.py::PlanDecisionView",
    # DRF-2871 — подписи способностей: справочное чтение, о человеке ничего.
    "wellness/plan_engine_api.py::PlanCapabilityLabelsView",
}


def _tracked_python() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [
        f
        for f in out.split()
        if "/tests/" not in f and not f.endswith("tests.py") and "/migrations/" not in f
    ]


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
    }


def _guarded_views() -> dict[str, dict]:
    """``"path::Class"`` → facts, for every concrete view behind the permission.

    A class is guarded when its ``permission_classes`` (a literal list or a
    module-level list it names) contains the permission, or when it inherits
    from a guarded class. Concrete = defines an HTTP handler itself.
    """
    views: dict[str, dict] = {}
    for rel in _tracked_python():
        source = (ROOT / rel).read_text(encoding="utf-8")
        if PERMISSION not in source:
            continue
        tree = ast.parse(source)
        module_lists = {
            target.id: _names(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, (ast.List, ast.Tuple))
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        classes = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        perms: dict[str, set[str]] = {}
        for name, cls in classes.items():
            for stmt in cls.body:
                if isinstance(stmt, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "permission_classes" for t in stmt.targets
                ):
                    value = stmt.value
                    perms[name] = (
                        module_lists.get(value.id, set()) | {value.id}
                        if isinstance(value, ast.Name)
                        else _names(value)
                    )
        changed = True
        while changed:  # inheritance inside the module, to a fixpoint
            changed = False
            for name, cls in classes.items():
                if name in perms:
                    continue
                inherited = [perms[b.id] for b in cls.bases if isinstance(b, ast.Name) and b.id in perms]
                if inherited:
                    perms[name] = set().union(*inherited)
                    changed = True
        for name, cls in classes.items():
            if PERMISSION not in perms.get(name, set()):
                continue
            if not any(isinstance(m, ast.FunctionDef) and m.name in HANDLERS for m in cls.body):
                continue
            views[f"{rel}::{name}"] = {"node": cls, "source": source, "perms": perms[name]}
    return views


def _cross_checks_the_actor(view: dict) -> bool:
    """The handler compares a user id the CALLER SENT with ``request.user.id`` — inline or via a helper.

    «The row belongs to ``request.user``» (``appointment.client_id ==
    request.user.id``, read from the database) is ownership scoping, not a
    second factor: the caller sent nothing to compare. Only a value from the
    request counts — the validated body (``validated_data`` / ``claimed``) or
    the path (``_check_user_scope``).
    """
    node, source = view["node"], view["source"]
    for n in ast.walk(node):
        if isinstance(n, ast.Compare):
            segment = ast.get_source_segment(source, n) or ""
            if "request.user.id" in segment and ("validated_data" in segment or "claimed" in segment):
                return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_check_user_scope":
            return True
    return False


VIEWS = _guarded_views()


def test_the_census_sees_the_surface() -> None:
    """Presence first: the scan finds the permission's views, including the one DRF-2421 is about."""
    assert len(VIEWS) >= 20
    assert "users/catalog_recommendations_api.py::CatalogRecommendationsView" in VIEWS


def test_every_guarded_view_is_classified_exactly_once() -> None:
    assert not (A_CROSS_CHECKED & B_TENANT_ADMIN)
    assert not (A_CROSS_CHECKED & C_HEADER_ONLY)
    assert not (B_TENANT_ADMIN & C_HEADER_ONLY)
    classified = A_CROSS_CHECKED | B_TENANT_ADMIN | C_HEADER_ONLY

    unclassified = sorted(set(VIEWS) - classified)
    assert unclassified == [], (
        "a view behind IsBotServiceWithVerifiedClient is not classified: put it in A "
        "(compares a sent user id with request.user.id), B (IsTenantAdmin beside it) "
        "or — as a written decision — C (header only). See the class docstring."
    )
    gone = sorted(classified - set(VIEWS))
    assert gone == [], "listed views no longer behind the permission — remove them from this file"


def test_group_a_really_cross_checks() -> None:
    lying = sorted(v for v in A_CROSS_CHECKED if not _cross_checks_the_actor(VIEWS[v]))
    assert lying == [], "listed as cross-checking the actor, but no such comparison is in the view"


#: The tenant-grant second factors group B accepts. DRF-2826: the booking
#: desk (``IsTenantBookingDesk`` — an active admin OR receptionist grant in
#: ``request.tenant``) is the same kind of factor as ``IsTenantAdmin``: the
#: tenant from middleware, the grant active there; it admits one more role.
B_SECOND_FACTORS = ("IsTenantAdmin", "IsTenantBookingDesk")


def test_group_b_really_has_the_tenant_admin_permission() -> None:
    missing = sorted(
        v for v in B_TENANT_ADMIN
        if not any(name in VIEWS[v]["perms"] for name in B_SECOND_FACTORS)
    )
    assert missing == []


def test_group_c_has_no_hidden_cross_check() -> None:
    """A view that gained a cross-check belongs in A: the census must not undersell it either."""
    promoted = sorted(v for v in C_HEADER_ONLY if _cross_checks_the_actor(VIEWS[v]))
    assert promoted == []


def test_the_census_numbers_of_05_10() -> None:
    """Literally the numbers the docstring and DRF-2421 cite. A change here is a decision."""
    # DRF-2857: C 15 -> 17, the two durable-plan views (same standing as Plan Lite).
    # DRF-2868: C 17 -> 19, the two plan-step views.
    # DRF-2871: C 19 -> 20, the plan-composition view.
    # DRF-2871: C 20 -> 21, the capability-labels view.
    assert (len(A_CROSS_CHECKED), len(B_TENANT_ADMIN), len(C_HEADER_ONLY)) == (6, 6, 24)


def test_the_docstring_states_the_census_not_the_old_promise() -> None:
    from users.permissions import IsBotServiceWithVerifiedClient

    doc = IsBotServiceWithVerifiedClient.__doc__ or ""
    assert "me/identity/" in doc
    assert "the header alone" in doc
    assert "the view MUST\n    cross-check" not in doc
