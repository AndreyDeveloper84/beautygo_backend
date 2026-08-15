"""Who may act on a booking, and in what capacity (DRF-1064).

Until now every operational action on a booking was bound to one actor:
the assigned specialist. ``complete`` and ``no_show`` checked
``request.user.is_specialist`` *and* ``appointment.specialist.user_id ==
request.user.id``. That is not a permission setting — it is the actor
model itself, which is why a salon administrator could not close a visit
even after being given every role in the system.

This module answers one question — *in what capacity is this caller
acting on THIS row?* — and returns a value from the single
:class:`~appointments.domain.value_objects.OperationalActor` vocabulary,
so the answer can be stamped on the row, put in the event payload and
mapped to the envelope actor without three separate translations.

Two rules the surface must not lose:

* **The tenant comes from middleware, never from the body.** A salon
  administrator has to name the salon they are acting in exactly as
  ``IsTenantAdmin`` requires (``tenants/relationships_admin_api.py`` set
  this precedent; DRF-1062's schedule surface follows it). Without
  ``request.tenant`` there is nothing to authorise against, and we fail
  closed.
* **A row in another tenant is "not found", not "forbidden".** Answering
  403 would confirm that the id exists — same info-hiding rationale the
  existing cross-specialist check already uses.

A specialist who ALSO holds an admin grant is resolved as
``SPECIALIST`` for their own bookings and as ``SALON`` for a colleague's.
That is deliberate: the capacity is a property of the row, not of the
person.
"""
from __future__ import annotations

from typing import Any

from .domain.value_objects import OperationalActor


def has_tenant_admin_grant(request: Any) -> bool:
    """True when the caller is an active admin of the addressed tenant.

    Mirrors ``users.permissions.IsTenantAdmin`` exactly. It is duplicated
    as a function rather than reused as a permission class because the
    role gate here runs *inside* an action (alongside the specialist
    branch), where DRF's permission machinery is already past.
    """
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return False
    request_tenant = getattr(request, "tenant", None)
    if request_tenant is None:
        return False

    from users.models import TenantUserRelationship
    return TenantUserRelationship.objects.filter(
        user=user,
        tenant=request_tenant,
        role=TenantUserRelationship.Role.ADMIN,
        is_active=True,
    ).exists()


def may_operate_on_bookings(request: Any) -> bool:
    """Cheap pre-lock gate: could this caller act on *some* booking?

    Deliberately row-independent — it runs before the row is fetched, so
    a caller with no operational standing at all (a client, a guest) is
    turned away with 403 before we take a row lock. The authoritative,
    row-aware answer is :func:`resolve_booking_operator`.
    """
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return bool(getattr(user, "is_specialist", False)) or has_tenant_admin_grant(request)


def resolve_booking_operator(request: Any, appointment: Any) -> str | None:
    """Return the capacity this caller acts in on this booking, or None.

    ``None`` means "no standing on this row" and MUST be rendered as 404
    by the caller (info-hiding — see module docstring).

    Call this AFTER the row is locked: it reads ``appointment.tenant_id``,
    and deciding capacity against an unlocked read would let a concurrent
    change move the row out from under the decision.
    """
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return None

    if (
        getattr(user, "is_specialist", False)
        and appointment.specialist.user_id == user.id
    ):
        return OperationalActor.SPECIALIST.value

    request_tenant = getattr(request, "tenant", None)
    if (
        request_tenant is not None
        and appointment.tenant_id == request_tenant.id
        and has_tenant_admin_grant(request)
    ):
        return OperationalActor.SALON.value

    return None
