"""Перепись допуска активных предложений — что закроет включение флага (DRF-2866).

Решение владельца 07.10: «census=0 только по шаблонам недостаточно; нужен
dry-run по активным предложениям». Здесь он считается: по каждому активному
предложению — допущено ли оно к персональной рекомендации **сегодня** и
будет ли допущено **после включения**
``BODY_CARE_UNCLASSIFIED_FAIL_CLOSED``, и по каким причинам нет.

**Только чтение.** Модуль ничего не пишет и флаг на стенде не трогает:
второе положение флага он получает подменой настройки на время чтения.

### Что считается

Единица — активное предложение салона (``SalonService.is_active``), включая
предложения без канона. Причины не сливаются: у предложения названа
**каждая** несошедшаяся проверка, а не первая (на проводе резолвера код
один — первой; перепись читает функции каталога напрямую).

Проверки и их причины:

``link_not_verified``      связь с каноном не подтверждена — рекомендации
                           не подлежит при любом положении флага;
``no_sellable_master``     ни одного продаваемого ребра мастер × услуга;
``unclassified``           область канона неизвестна или канона нет;
``config_not_ready``       подлежит Body Care, конфигурация не готова;
``class_unconfirmed``      юридический класс не подтверждён;
``license_not_verified``,
``license_scope_mismatch`` класс медицинский, лицензия не сошлась;
``no_master_cleared``      мастера есть, но ни один не прошёл адрес и
                           квалификацию.

### Чего перепись не видит

* Что говорит резолвер клиенту и что отвечает бот на пустую выдачу.
* Гейт новой записи: проверки ниже закрывают рекомендацию, не запись.
* Клиентские проверки (скрининг, противопоказания) — это слой клиента.
* Демонстрационные салоны считаются наравне с остальными; область
  сужается слагами.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from django.test.utils import override_settings

from services import body_care_address, body_care_license, body_care_qualification
from services.body_care_validation import NOT_SUBJECT, READY_FOR_SCREENING, UNCLASSIFIED, validation_states
from services.models import SalonService, SpecialistService
from services.offer_sellable import sellable_offer_q
from users.sellable import sellable_q

FLAG = "BODY_CARE_UNCLASSIFIED_FAIL_CLOSED"

LINK_NOT_VERIFIED = "link_not_verified"
NO_SELLABLE_MASTER = "no_sellable_master"
REASON_UNCLASSIFIED = "unclassified"
CONFIG_NOT_READY = "config_not_ready"
CLASS_UNCONFIRMED = "class_unconfirmed"
LICENSE_NOT_VERIFIED = "license_not_verified"
LICENSE_SCOPE_MISMATCH = "license_scope_mismatch"
NO_MASTER_CLEARED = "no_master_cleared"

#: Порядок — как у резолвера в S1: связь → конфигурация → юридические условия.
#: Первая причина предложения в этом порядке — та, что ушла бы на провод.
REASONS = (
    LINK_NOT_VERIFIED,
    NO_SELLABLE_MASTER,
    REASON_UNCLASSIFIED,
    CONFIG_NOT_READY,
    CLASS_UNCONFIRMED,
    LICENSE_NOT_VERIFIED,
    LICENSE_SCOPE_MISMATCH,
    NO_MASTER_CLEARED,
)

_LICENSE_REASON = {
    body_care_license.CLASS_UNCONFIRMED: CLASS_UNCONFIRMED,
    body_care_license.NOT_VERIFIED: LICENSE_NOT_VERIFIED,
    body_care_license.SCOPE_MISMATCH: LICENSE_SCOPE_MISMATCH,
}
_ADDRESS_OPEN = frozenset({body_care_address.NOT_REQUIRED, body_care_address.VERIFIED})
_QUALIFICATION_OPEN = frozenset(
    {body_care_qualification.NOT_REQUIRED, body_care_qualification.VERIFIED}
)

NO_CANON = "нет канона"
NO_CODE = "без кода"


@dataclass(frozen=True)
class OfferVerdict:
    """Допуск одного предложения при одном положении флага."""

    reasons: tuple[str, ...]

    @property
    def available(self) -> bool:
        return not self.reasons

    @property
    def first_reason(self) -> str | None:
        return self.reasons[0] if self.reasons else None


@dataclass(frozen=True)
class OfferRow:
    pk: object
    tenant_slug: str
    name: str
    #: Код эталонного справочника, ``NO_CODE`` или ``NO_CANON``.
    canon: str
    sellable_masters: int
    today: OfferVerdict
    after: OfferVerdict

    @property
    def section(self) -> str:
        """Раздел кода (``1.4``) — группа для счёта; у канона без кода — сама метка."""
        return self.canon.rsplit(".", 1)[0] if self.canon[:1].isdigit() else self.canon


@dataclass
class Census:
    flag_now: bool
    rows: list[OfferRow] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.rows)

    def available(self, when: str) -> list[OfferRow]:
        return [r for r in self.rows if getattr(r, when).available]

    @property
    def closing(self) -> list[OfferRow]:
        """Доступны сегодня — закроются после включения."""
        return [r for r in self.rows if r.today.available and not r.after.available]

    @property
    def opening(self) -> list[OfferRow]:
        """Закрыты сегодня — откроются после включения. Ожидается пусто."""
        return [r for r in self.rows if not r.today.available and r.after.available]

    def reasons(self, when: str) -> Counter:
        """Сколько предложений несут причину (предложение — под каждой своей)."""
        return Counter(reason for r in self.rows for reason in getattr(r, when).reasons)

    def first_reasons(self, when: str) -> Counter:
        return Counter(
            getattr(r, when).first_reason for r in self.rows if not getattr(r, when).available
        )

    @property
    def unclassified(self) -> list[OfferRow]:
        """Активные предложения с неизвестной областью — условие включения флага."""
        return [r for r in self.rows if REASON_UNCLASSIFIED in r.after.reasons]


def _verdicts(rows: list[dict], edges: dict[object, list[tuple[object, object]]]) -> dict[object, OfferVerdict]:
    """Причины по каждому предложению при ТЕКУЩЕМ положении флага."""
    pks = [r["pk"] for r in rows]
    config = validation_states(pks)
    licence = body_care_license.license_states(pks)
    pairs = [(specialist, pk) for pk, masters in edges.items() for specialist, _ in masters]
    address = body_care_address.address_states(pairs)
    qualification = body_care_qualification.qualification_states(
        {(specialist, template) for masters in edges.values() for specialist, template in masters if template}
    )

    out: dict[object, OfferVerdict] = {}
    for row in rows:
        pk = row["pk"]
        reasons: list[str] = []
        if row["mapping_status"] != SalonService.MappingStatus.VERIFIED:
            reasons.append(LINK_NOT_VERIFIED)
        masters = edges.get(pk, [])
        if not masters:
            reasons.append(NO_SELLABLE_MASTER)
        state = config.get(pk)
        if state == UNCLASSIFIED:
            reasons.append(REASON_UNCLASSIFIED)
        elif state not in (NOT_SUBJECT, READY_FOR_SCREENING):
            reasons.append(CONFIG_NOT_READY)
        licence_reason = _LICENSE_REASON.get(licence.get(pk))
        if licence_reason:
            reasons.append(licence_reason)
        if masters and not licence_reason and not any(
            address.get((specialist, pk)) in _ADDRESS_OPEN
            and (
                template is None
                or getattr(qualification.get((specialist, template)), "state", None) in _QUALIFICATION_OPEN
            )
            for specialist, template in masters
        ):
            reasons.append(NO_MASTER_CLEARED)
        out[pk] = OfferVerdict(tuple(r for r in REASONS if r in reasons))
    return out


def admission_census(tenant_slugs: Iterable[str] | None = None) -> Census:
    """Перепись допуска активных предложений при обоих положениях флага.

    ``tenant_slugs`` — сузить область; ``None`` — вся база.
    """
    from django.conf import settings

    offers = SalonService.objects.filter(is_active=True)
    if tenant_slugs is not None:
        offers = offers.filter(tenant__slug__in=list(tenant_slugs))
    rows = list(
        offers.order_by("tenant__slug", "name", "pk").values(
            "pk", "name", "mapping_status", "template_id", "tenant__slug", "template__canonical_code",
        )
    )
    edges: dict[object, list[tuple[object, object]]] = {}
    for offer_pk, specialist, template in (
        SpecialistService.objects.filter(sellable_offer_q(), sellable_q("specialist"))
        .filter(salon_service_id__in=[r["pk"] for r in rows])
        .values_list("salon_service_id", "specialist_id", "salon_service__template_id")
    ):
        edges.setdefault(offer_pk, []).append((specialist, template))

    with override_settings(**{FLAG: False}):
        today = _verdicts(rows, edges)
    with override_settings(**{FLAG: True}):
        after = _verdicts(rows, edges)

    census = Census(flag_now=bool(getattr(settings, FLAG, False)))
    for row in rows:
        canon = NO_CANON if row["template_id"] is None else (row["template__canonical_code"] or NO_CODE)
        census.rows.append(
            OfferRow(
                pk=row["pk"],
                tenant_slug=row["tenant__slug"],
                name=row["name"],
                canon=canon,
                sellable_masters=len(edges.get(row["pk"], [])),
                today=today[row["pk"]],
                after=after[row["pk"]],
            )
        )
    return census
