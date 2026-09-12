"""What the authorization check decided, carried to whoever records it.

The permission knows WHY a call was allowed or refused; the view knows WHAT
was being reached. The journal needs both, so the decision travels on the
request as one small frozen object instead of being reconstructed from a
status code — a 403 tells a reader that something was refused and never
which of five different things it was.

``auditable`` is the one field with a policy in it. Owner §96 asks for
"каждая попытка, включая отклонённую", and the composition it asks for names
an actor, a role and a tenant. A request with no credential at all has none
of those: there is nothing to record but the fact that an anonymous someone
knocked. Recording a durable row for each of those hands an unauthenticated
caller a way to grow our storage, so those stay in the log and everything
from an authenticated caller goes in the journal. This reading was carried
to the owner as a reading, not as their words.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Where the outcome is parked on the request. A private-ish name because it
# is an internal contract between the permission and the audit mixin, not
# part of the request API.
REQUEST_ATTR = "_subject_authz_outcome"


@dataclass(frozen=True)
class SubjectAuthzOutcome:
    caller_purpose: str
    actor: Any
    actor_named: bool
    allowed: bool
    auditable: bool
    reason: str = ""


def attach(request, outcome: SubjectAuthzOutcome) -> None:
    setattr(request, REQUEST_ATTR, outcome)
    # DRF wraps the Django request; a permission sees the DRF one and the
    # mixin may hold either. Mirror onto the underlying request so neither
    # side has to know which it is holding.
    underlying = getattr(request, "_request", None)
    if underlying is not None:
        setattr(underlying, REQUEST_ATTR, outcome)


def read(request) -> SubjectAuthzOutcome | None:
    outcome = getattr(request, REQUEST_ATTR, None)
    if outcome is not None:
        return outcome
    underlying = getattr(request, "_request", None)
    if underlying is not None:
        return getattr(underlying, REQUEST_ATTR, None)
    return None
