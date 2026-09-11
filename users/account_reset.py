"""Reset a TEST account so it can be used for a fresh pilot check-through — catalog half.

B-R (DRF-1617). The bot has its own ``reset_test_account`` with the same
discipline (``ai-bot-platform/apps/identity/services/account_reset.py``);
this is the catalog side of the same operation, and the reason there ARE two
sides is the measurement of 11.09: a bot-only reset leaves the catalog proxy
``bot:max:<id>`` in place, and the next ``/start`` resolves through
``resolve_external_user`` to the SAME catalog user — the bot thinks the
person is new, the catalog knows better. Run THIS half first: if it fails,
the bot is still whole and the next ``/start`` is safe.

    erasure   protects a PERSON      deliberately KEEPS what must survive
    reset     frees an ACCOUNT       has to remove exactly that

``erase_personal_context`` (C5.2) erases ``UserPersonalContext`` only; the
seven ``PROTECT`` relations on ``users.User`` are untouched by any existing
path. A reset has to take some of them apart — the questionnaire's own
products — and refuse on the rest by name.

### Two subjects, both named on the allowlist

A bot identity is a proxy row (``is_proxy=True``, username ``bot:max:<id>``).
Once the person registers with a phone, ``linked_user`` points the proxy at
the REAL account, and every internal surface resolves to that account. A
reset of the proxy alone would leave the real account — and its phone — so
the next registration finds it. So the plan follows the binding, and the
real account must be on the allowlist BY ITS OWN UUID: a proxy being listed
does not list what it points at. Getting onto the list is a separate act,
never a side effect (§5 of the measurement, owner 11.09).

### Read-only plan, ORM only, hidden relations included

Same construction as the bot half: every incoming relation of ``User`` from
``get_fields(include_hidden=True)``; ``Collector.collect`` for the
transitive picture (it writes nothing); every PROTECT either dismantled by
the chosen mode or a named blocker. The database has no cascades of its own
— every FK is ``NO ACTION`` — so the ORM is the only thing that will ever
delete a child row here, and reading its plan is reading the truth.

### What survives, by name

``privacy_audit.PersonalDataAccessLog.object_id`` (PR #318) is the subject
of an access-log row as a bare UUID — the journal must outlive the person
it is about (owner §96). It is listed in :data:`KEPT_BY_DESIGN` with the
reason; while #318 is not merged the model does not exist and the plan says
so out loud rather than silently having nothing to keep.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field

from django.apps import apps
from django.conf import settings
from django.db import models, transaction
from django.db.models.deletion import Collector, ProtectedError, RestrictedError

from users.models import User

# --- modes -------------------------------------------------------------------


@dataclass(frozen=True)
class Mode:
    """Named by what it FREES. ``dismantles`` is the closed set of PROTECT
    relations the mode takes apart by design; every other PROTECT refuses."""

    name: str
    frees: str
    dismantles: frozenset[str]


#: The onboarding questionnaire's own products. «готов к проверке анкеты»
#: is not true while a previous questionnaire's goals and plan are there.
_QUESTIONNAIRE = frozenset(
    {
        "goals.ClientGoal.client",
        "goals.GoalAnketaRun.client",
        "wellness.DesiredOutcome.user",
        "wellness.PersonalPlan.user",
        "wellness.ProgressObservation.user",
    }
)

MODES: dict[str, Mode] = {
    "client-onboarding": Mode(
        name="client-onboarding",
        frees="готов к проверке анкеты онбординга клиента",
        dismantles=_QUESTIONNAIRE,
    ),
    "master-registration": Mode(
        name="master-registration",
        frees="готов к проверке регистрации мастера",
        # Same set on this side: the master's registration lives in the bot
        # (tenant, card, role); here she is a user like any other. Her
        # subscription (billing.SpecialistSubscription) and her appointments
        # stay PROTECT — both are owner questions, not code.
        dismantles=_QUESTIONNAIRE,
    ),
}


# --- what survives, by name and with a reason --------------------------------

#: label → (column, reason). Columns that hold a user id WITHOUT an FK and
#: must survive the person. Reported by the plan, never asserted on.
KEPT_BY_DESIGN: dict[str, tuple[str, str]] = {
    "privacy_audit.PersonalDataAccessLog.object_id": (
        "object_id",
        "журнал доступа к персданным обязан пережить субъекта (владелец §96, PR #318)",
    ),
}


# --- the plan ----------------------------------------------------------------


@dataclass(frozen=True)
class Relation:
    label: str
    on_delete: str
    model: type[models.Model]
    column: str
    hidden: bool


def incoming_relations(model: type[models.Model] = User) -> list[Relation]:
    """Every relation pointing AT ``model``, hidden ones included."""
    out: list[Relation] = []
    for f in model._meta.get_fields(include_hidden=True):
        fld = getattr(f, "field", None)
        if fld is None or not f.is_relation or f.concrete:
            continue
        on_delete = getattr(getattr(fld.remote_field, "on_delete", None), "__name__", "M2M")
        out.append(
            Relation(
                label=f"{fld.model._meta.label}.{fld.name}",
                on_delete=on_delete,
                model=fld.model,
                column=fld.name,
                hidden=bool(getattr(f, "hidden", False)),
            )
        )
    return sorted(out, key=lambda r: (r.on_delete, r.label))


@dataclass
class Line:
    label: str
    on_delete: str
    disposition: str  # cascade | set_null | dismantled | blocks | protect_empty
    rows: int
    removes: Counter = field(default_factory=Counter)


@dataclass
class Kept:
    label: str
    rows: int | None  # None: the model is not installed
    reason: str


@dataclass
class Subject:
    user: User
    kind: str  # proxy | real | proxy-sibling
    listed_as: str  # what would have to be on the allowlist for this row


@dataclass
class Plan:
    account: str
    mode: Mode
    subjects: list[Subject]
    lines: list[Line]
    kept: list[Kept]
    unlisted: list[Subject]

    @property
    def user_ids(self) -> list[uuid.UUID]:
        return [s.user.id for s in self.subjects]

    @property
    def blockers(self) -> list[Line]:
        return [ln for ln in self.lines if ln.disposition == "blocks"]

    @property
    def ok(self) -> bool:
        return bool(self.subjects) and not self.blockers and not self.unlisted


class NotAllowed(Exception):
    """Something the reset would touch is not on the allowlist. A wall."""

    def __init__(self, subjects: list[Subject]) -> None:
        self.subjects = subjects
        super().__init__(", ".join(f"{s.kind}={s.listed_as}" for s in subjects))


class Blocked(Exception):
    def __init__(self, lines: list[Line]) -> None:
        self.lines = lines
        super().__init__(", ".join(f"{ln.label}={ln.rows}" for ln in lines))


def parse_account(spec: str) -> tuple[str, str]:
    """``max:83146139`` → ``("max", "83146139")`` — the same spelling the bot
    half takes, so one line in a runbook names the account on both sides."""
    channel, sep, channel_user_id = spec.partition(":")
    if not sep or not channel or not channel_user_id:
        raise ValueError(f"account must look like channel:channel_user_id, got {spec!r}")
    return channel, channel_user_id


def proxy_username(spec: str) -> str:
    channel, channel_user_id = parse_account(spec)
    return f"bot:{channel}:{channel_user_id}"


def allowlist() -> frozenset[str]:
    """``ACCOUNT_RESET_ALLOWLIST``: ``channel:channel_user_id`` entries name a
    proxy, bare UUIDs name a real account. Empty means nobody."""
    raw = getattr(settings, "ACCOUNT_RESET_ALLOWLIST", ())
    return frozenset(item.strip() for item in raw if item and item.strip())


def _subjects(spec: str) -> list[Subject]:
    """The proxy, then — if bound — the real account, then the real account's
    other proxies. Everything the reset would remove, each with the allowlist
    spelling that has to cover it."""
    out: list[Subject] = []
    proxy = User.objects.filter(username=proxy_username(spec)).select_related("linked_user").first()
    if proxy is None:
        return out
    out.append(Subject(proxy, "proxy", spec))
    real = proxy.linked_user
    if real is not None:
        out.append(Subject(real, "real", str(real.id)))
        for sibling in User.objects.filter(linked_user=real).exclude(id=proxy.id).order_by("username"):
            # Another channel's proxy for the same person. It would be SET_NULL'd
            # by the real account's delete and left behind as an unbound proxy —
            # which is not «freed». So it goes too, and it has to be listed.
            out.append(Subject(sibling, "proxy-sibling", _spec_of(sibling)))
    return out


def _spec_of(proxy: User) -> str:
    # bot:max:123 → max:123; anything else is listed by its own username.
    parts = proxy.username.split(":", 2)
    if len(parts) == 3 and parts[0] == "bot":
        return f"{parts[1]}:{parts[2]}"
    return proxy.username


def _collect(objs: list[models.Model]) -> Counter:
    if not objs:
        return Counter()
    collector = Collector(using="default")
    collector.collect(objs)
    removes: Counter = Counter()
    for model, instances in collector.data.items():
        removes[model._meta.label] += len(instances)
    for qs in collector.fast_deletes:
        removes[qs.model._meta.label] += qs.count()
    return removes


def _line(rel: Relation, mode: Mode, ids: list[uuid.UUID]) -> Line:
    qs = rel.model._base_manager.filter(**{f"{rel.column}__in": ids})
    rows = qs.count()
    if rel.on_delete in ("PROTECT", "RESTRICT"):
        if rel.label in mode.dismantles:
            disposition = "dismantled"
        elif rows:
            disposition = "blocks"
        else:
            disposition = "protect_empty"
    elif rel.on_delete in ("SET_NULL", "SET_DEFAULT", "SET"):
        disposition = "set_null"
    else:
        disposition = "cascade"

    removes: Counter = Counter()
    if disposition in ("cascade", "dismantled") and rows:
        try:
            removes = _collect(list(qs))
        except (ProtectedError, RestrictedError) as exc:
            protected: set = set(getattr(exc, "protected_objects", ()) or ())
            protected |= set(getattr(exc, "restricted_objects", ()) or ())
            disposition = "blocks"
            removes = Counter(o._meta.label for o in protected)
    return Line(rel.label, rel.on_delete, disposition, rows, removes)


def _kept(ids: list[uuid.UUID]) -> list[Kept]:
    out: list[Kept] = []
    for label, (column, reason) in KEPT_BY_DESIGN.items():
        app_label, model_name, _ = label.split(".", 2)
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            out.append(Kept(label, None, reason))
            continue
        rows = model._base_manager.filter(**{f"{column}__in": ids}).count() if ids else 0
        out.append(Kept(label, rows, reason))
    return out


def plan(spec: str, mode_name: str) -> Plan:
    """Read-only. Does not consult the allowlist for the PROXY (asking «what
    would this take?» is legitimate) but reports which subjects are not
    listed, so the refusal that :func:`apply` will give is visible here."""
    mode = MODES[mode_name]
    subjects = _subjects(spec)
    ids = [s.user.id for s in subjects]
    listed = allowlist()
    lines = [_line(rel, mode, ids) for rel in incoming_relations()] if subjects else []
    # users.User.linked_user is the binding itself: the real account's delete
    # would SET_NULL it on the proxy, but the proxy is being deleted too, so
    # the pointer is part of the subject set, not a leftover.
    return Plan(
        account=spec,
        mode=mode,
        subjects=subjects,
        lines=lines,
        kept=_kept(ids),
        unlisted=[s for s in subjects if s.listed_as not in listed],
    )


# --- applying it -------------------------------------------------------------


@dataclass
class Leftover:
    label: str
    rows: int


def verify(ids: list[uuid.UUID]) -> list[Leftover]:
    """Re-read every relation from the database; anything non-zero is a leftover."""
    left: list[Leftover] = []
    if not ids:
        return left
    n = User.objects.filter(id__in=ids).count()
    if n:
        left.append(Leftover("users.User", n))
    for rel in incoming_relations():
        rows = rel.model._base_manager.filter(**{f"{rel.column}__in": ids}).count()
        if rows:
            left.append(Leftover(rel.label, rows))
    return left


@dataclass
class Report:
    plan: Plan
    removed: Counter
    leftovers: list[Leftover]


def apply(spec: str, mode_name: str) -> Report:
    """Free the account. One transaction; refuses before the first write."""
    with transaction.atomic():
        p = plan(spec, mode_name)
        if not p.subjects:
            return Report(p, Counter(), [])
        if p.unlisted:
            raise NotAllowed(p.unlisted)
        if p.blockers:
            raise Blocked(p.blockers)

        removed: Counter = Counter()
        rels = {r.label: r for r in incoming_relations()}
        for ln in p.lines:
            if ln.disposition != "dismantled" or not ln.rows:
                continue
            rel = rels[ln.label]
            _, per_model = rel.model._base_manager.filter(**{f"{rel.column}__in": p.user_ids}).delete()
            removed.update(per_model)

        # Real account first, then proxies: deleting a proxy first would
        # fire SET_NULL on nothing, deleting the real one first fires
        # SET_NULL on proxies we are about to delete anyway. Either order
        # works; this one keeps the plan's SET_NULL count honest.
        ordered = sorted(p.subjects, key=lambda s: 0 if s.kind == "real" else 1)
        for subject in ordered:
            _, per_model = User.objects.filter(id=subject.user.id).delete()
            removed.update(per_model)

        leftovers = verify(p.user_ids)
        if leftovers:
            transaction.set_rollback(True)
            return Report(p, Counter(), leftovers)
    return Report(p, removed, [])


__all__ = [
    "KEPT_BY_DESIGN",
    "MODES",
    "Blocked",
    "Kept",
    "Leftover",
    "Line",
    "Mode",
    "NotAllowed",
    "Plan",
    "Relation",
    "Report",
    "Subject",
    "allowlist",
    "apply",
    "incoming_relations",
    "parse_account",
    "plan",
    "proxy_username",
    "verify",
]
