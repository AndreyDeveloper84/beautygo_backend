"""Catalog half of the read-only identity card (owner 11.09 §12, DRF-1693).

The bot's card (``ai-bot-platform``, ``identity_card``) carries what the bot
knows: tenants, roles, master cards, dates, dialogues. It says out loud what
it does NOT carry — goals, questionnaire runs, food logs, appointments — and
this is that half. Same discipline: reads only, every personal value masked
(name: first letter and length; phone: presence and length, never a digit —
DRF-1039), counts for everything else.

### Who the card is about

A bot identity lives here as a proxy row ``bot:<channel>:<id>``. When the
person registered with a phone, ``linked_user`` points at the real account;
the card follows that pointer once — the same hop the resolver takes — and
shows both rows, each masked. The measurement of 11.09 found no proxy bound
to a real account among the six real ids; the slot is kept so that the day
one is bound, it shows up here rather than being missed.

### Appointments — two numbers, not one

§16.3 keeps ``Appointment.client`` PROTECT for *состоявшиеся* records. The
card counts appointments by status and shows the ones that would hold a
reset (completed, confirmed, pending, awaiting payment) apart from the ones
that would not have held anything in product terms (cancelled, no-show) —
while saying that the ORM holds on ALL of them. Two numbers, so the operator
does not read «3 записи» as «3 состоявшихся».
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Count

from appointments.models import Appointment
from goals.models import ClientGoal, GoalAnketaRun
from nutrition.models import FoodLog
from users.models import User, UserPersonalContext

BLOCKED_BY_IDENTITY = "BLOCKED_BY_IDENTITY"

#: Statuses §16.3 means by «состоявшиеся» — the ones a reset must not touch
#: in product terms. The ORM PROTECT holds every status; this split is for
#: the reader, and it is named as such on the card.
HELD_IN_PRODUCT = (
    Appointment.Status.COMPLETED,
    Appointment.Status.CONFIRMED,
    Appointment.Status.PENDING,
    Appointment.Status.AWAITING_PAYMENT,
)


def mask_name(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        return "нет"
    return f"{value[0]}… ({len(value)})"


def mask_phone(value: str | None) -> str:
    """Presence and length only — never a digit (DRF-1039)."""
    value = (value or "").strip()
    if not value:
        return "нет"
    return f"есть, длина {len(value)}"


def parse_account(spec: str) -> tuple[str, str]:
    channel, sep, channel_user_id = spec.partition(":")
    if not sep or not channel or not channel_user_id:
        raise ValueError(f"account must look like channel:channel_user_id, got {spec!r}")
    return channel, channel_user_id


@dataclass(frozen=True)
class Row:
    """One ``users.User`` — the proxy or the real account it is bound to."""

    kind: str  # proxy | real
    user_id: uuid.UUID
    username_masked: str
    first_name: str
    phone: str
    role: str
    is_proxy: bool
    onboarding_completed: bool
    date_joined: datetime
    last_login: datetime | None
    personal_context: bool
    goals: int
    anketa_runs: int
    food_logs: int
    appointments_by_status: dict[str, int]

    @property
    def appointments_held(self) -> int:
        return sum(self.appointments_by_status.get(s, 0) for s in HELD_IN_PRODUCT)

    @property
    def appointments_total(self) -> int:
        return sum(self.appointments_by_status.values())


@dataclass(frozen=True)
class CatalogCard:
    channel: str
    channel_user_id: str
    rows: tuple[Row, ...]
    status: str = BLOCKED_BY_IDENTITY

    @property
    def found(self) -> bool:
        return bool(self.rows)


def _row(kind: str, user: User) -> Row:
    by_status = dict(
        Appointment.objects.filter(client_id=user.id)
        .values_list("status")
        .annotate(n=Count("id"))
        .values_list("status", "n")
    )
    # A proxy username is `bot:max:<id>` — the id is what the operator typed,
    # so it stays; a real account's username is a personal value and is masked.
    username_masked = user.username if user.is_proxy else mask_name(user.username)
    return Row(
        kind=kind,
        user_id=user.id,
        username_masked=username_masked,
        first_name=user.first_name or "",
        phone=user.phone or "",
        role=user.role,
        is_proxy=user.is_proxy,
        onboarding_completed=user.onboarding_completed,
        date_joined=user.date_joined,
        last_login=user.last_login,
        personal_context=UserPersonalContext.objects.filter(user_id=user.id).exists(),
        goals=ClientGoal.objects.filter(client_id=user.id).count(),
        anketa_runs=GoalAnketaRun.objects.filter(client_id=user.id).count(),
        food_logs=FoodLog.objects.filter(user_id=user.id).count(),
        appointments_by_status=by_status,
    )


def build_card(channel: str, channel_user_id: str) -> CatalogCard:
    """Read-only. The proxy and — if bound — the real account, one hop."""
    proxy = (
        User.objects.filter(username=f"bot:{channel}:{channel_user_id}")
        .select_related("linked_user")
        .first()
    )
    if proxy is None:
        return CatalogCard(channel, channel_user_id, ())
    rows = [_row("proxy", proxy)]
    if proxy.linked_user is not None:
        rows.append(_row("real", proxy.linked_user))
    return CatalogCard(channel, channel_user_id, tuple(rows))


def _d(value: datetime | None) -> str:
    return value.date().isoformat() if value else "—"


def render_for_operator(card: CatalogCard) -> str:
    if not card.found:
        proxy = f"bot:{card.channel}:{card.channel_user_id}"
        return f"{card.channel}:{card.channel_user_id} — proxy {proxy} в каталоге нет; карточки нет"
    lines = [
        f"{card.channel}:{card.channel_user_id}    статус: {card.status}",
        f"строк users.User: {len(card.rows)}"
        + ("    (proxy не привязан к реальному аккаунту)" if len(card.rows) == 1 else ""),
        "",
    ]
    for r in card.rows:
        lines.append(
            f"[{r.kind}] {r.username_masked}  роль={r.role}  proxy={'да' if r.is_proxy else 'нет'}"
            f"  онбординг={'да' if r.onboarding_completed else 'нет'}"
        )
        lines.append(f"    имя: {mask_name(r.first_name)}    телефон: {mask_phone(r.phone)}")
        lines.append(f"    создан: {_d(r.date_joined)}    последний вход: {_d(r.last_login)}")
        lines.append(
            f"    личный контекст: {'есть' if r.personal_context else 'нет'}"
            f"    целей: {r.goals}    прогонов анкеты: {r.anketa_runs}    записей еды: {r.food_logs}"
        )
        lines.append(
            f"    записей на приём: всего {r.appointments_total}, "
            f"из них держат сброс по §16.3: {r.appointments_held}"
            + (
                "  (" + ", ".join(f"{k}={v}" for k, v in sorted(r.appointments_by_status.items())) + ")"
                if r.appointments_by_status
                else ""
            )
        )
        lines.append("")
    lines.append(
        "PROTECT в ORM держит записи ЛЮБОГО статуса; «держат сброс» — прочтение §16.3, не поведение базы."
    )
    return "\n".join(lines)


__all__ = [
    "BLOCKED_BY_IDENTITY",
    "HELD_IN_PRODUCT",
    "CatalogCard",
    "Row",
    "build_card",
    "mask_name",
    "mask_phone",
    "parse_account",
    "render_for_operator",
]
