"""Перепись проверки здоровья по активным предложениям — список последствий (DRF-2877).

Решение владельца 07.10 (пакет S2): неподтверждённое «проверка перед услугой
не нужна» — это «неизвестно»; черновое «нужна» — «требование ещё не
подтверждено». Смотреть надо происхождение **конкретного поля**, а не способ
создания строки услуги: умолчание, копия флага канона из сидера и даже
ручное значение без автора — не ответ человека. Правило записи включается
только после того, как владелец посмотрит список затронутых услуг.

Этот модуль — тот список. **Только чтение**: он ничего не пишет и никакое
правило не включает.

### Что считается

Единица — активное предложение салона. Вердикт гейта записи берётся у
каждого продаваемого ребра мастер × услуга методом модели
(``SpecialistService.resolved_health_check``) — тем же, что решает запись;
своего расчёта «сегодня» здесь нет, чтобы он не разошёлся с гейтом.

Исход сегодня:

``passes``        гейт отвечает «проверка не нужна»;
``client_check``  гейт отвечает «нужна»;
``unknown``       гейт отвечает «неизвестно».

Исход после правила владельца — три значения, и два из них нельзя путать:

``passes``        канон подтверждённо говорит «не нужна», и никто не поднял
                  неподтверждённое требование;
``client_check``  канон подтверждённо говорит «нужна» — проверку проходит
                  **клиент**;
``undefined``     всё остальное: условия услуги в каталоге не определены.
                  Адресат — ответственный за данные услуги, **не клиент**.

### Происхождение ответа салона

У ответа салона сегодня нет ни автора, ни даты — поля происхождения
появятся отдельной миграцией. Пока их нет, каждый ответ салона считается
неподтверждённым; когда появятся, перепись прочтёт их без правки
(``salon_answer_confirmed``).

### Чего перепись не видит

* Что гейт записи в боте делает с вердиктом — она читает каталог.
* Предложение без продаваемого мастера записи не имеет вовсе; оно стоит
  отдельной группой и в «после» не считается.
* Уже созданные записи: правило владельца их не трогает, перепись — тоже.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from services.models import SalonService, ServiceTemplate, SpecialistService
from services.offer_sellable import sellable_offer_q
from users.sellable import sellable_q

PASSES = "passes"
CLIENT_CHECK = "client_check"
UNKNOWN = "unknown"
UNDEFINED = "undefined"

TODAY_OUTCOMES = (PASSES, CLIENT_CHECK, UNKNOWN)
AFTER_OUTCOMES = (PASSES, CLIENT_CHECK, UNDEFINED)

#: Почему после правила условия услуги «не определены».
CANON_MISSING = "canon_missing"
CANON_FLAG_INFERRED = "canon_flag_inferred"
SALON_RAISE_UNCONFIRMED = "salon_raise_unconfirmed"
MASTER_RAISE_UNCONFIRMED = "master_raise_unconfirmed"

NO_CANON = "нет канона"
NO_CODE = "без кода"

_TODAY = {True: CLIENT_CHECK, False: PASSES, None: UNKNOWN}


def salon_answer_confirmed(offer: SalonService) -> bool:
    """Подтверждён ли ответ салона человеком или названным правилом.

    Полей происхождения у ответа салона ещё нет — до их появления ответ
    всегда неподтверждён. Читается через ``getattr``, чтобы перепись увидела
    поле без правки, когда оно появится.
    """
    return getattr(offer, "health_check_origin", None) == "confirmed"


def after_outcome(edge: SpecialistService) -> tuple[str, tuple[str, ...]]:
    """Исход после правила владельца и причины, если условия не определены."""
    offer = edge.salon_service
    canon = offer.template
    confirmed = (
        canon is not None
        and canon.health_check_origin == ServiceTemplate.HealthCheckOrigin.CONFIRMED
    )
    if confirmed and canon.requires_health_check:
        return CLIENT_CHECK, ()

    reasons: list[str] = []
    if canon is None:
        reasons.append(CANON_MISSING)
    elif not confirmed:
        reasons.append(CANON_FLAG_INFERRED)
    # Неподтверждённое «нужна» само опрос клиента не запускает и запись не
    # открывает — в обе стороны требование не подтверждено.
    if offer.requires_health_check is True and not salon_answer_confirmed(offer):
        reasons.append(SALON_RAISE_UNCONFIRMED)
    if edge.requires_health_check:
        reasons.append(MASTER_RAISE_UNCONFIRMED)
    if reasons:
        return UNDEFINED, tuple(reasons)
    return PASSES, ()


@dataclass(frozen=True)
class EdgeVerdict:
    today: str
    basis: str
    after: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class OfferRow:
    pk: object
    tenant_slug: str
    name: str
    canon: str
    #: Ответ салона: ``None`` — не отвечал, ``True`` — нужна, ``False`` — не нужна.
    salon_answer: bool | None
    salon_confirmed: bool
    #: Флаг канона и подтверждён ли он; ``None`` — канона нет.
    canon_flag: bool | None
    canon_confirmed: bool
    edges: tuple[EdgeVerdict, ...]

    @property
    def sellable(self) -> bool:
        return bool(self.edges)

    def passes(self, when: str) -> bool:
        """Есть ли хоть один мастер, запись к которому проходит гейт."""
        return any(getattr(e, when) == PASSES for e in self.edges)

    @property
    def stops(self) -> bool:
        """Сегодня запись проходит — после правила перестанет."""
        return self.passes("today") and not self.passes("after")

    @property
    def master_raises(self) -> int:
        return sum(MASTER_RAISE_UNCONFIRMED in e.reasons for e in self.edges)


@dataclass
class Census:
    rows: list[OfferRow] = field(default_factory=list)

    @property
    def sellable(self) -> list[OfferRow]:
        return [r for r in self.rows if r.sellable]

    @property
    def stopping(self) -> list[OfferRow]:
        return [r for r in self.rows if r.stops]

    def outcomes(self, when: str) -> Counter:
        """Продаваемые рёбра по исходам."""
        return Counter(getattr(e, when) for r in self.rows for e in r.edges)

    def bases(self) -> Counter:
        return Counter(e.basis for r in self.rows for e in r.edges)

    def reasons(self) -> Counter:
        return Counter(reason for r in self.rows for e in r.edges for reason in e.reasons)

    def salon_answers(self) -> Counter:
        """Ответы салона: (значение, подтверждён ли)."""
        return Counter((r.salon_answer, r.salon_confirmed) for r in self.rows)

    def canon_flags(self) -> Counter:
        """Флаги канона у предложений: (значение, подтверждён ли)."""
        return Counter((r.canon_flag, r.canon_confirmed) for r in self.rows)


def health_check_census(tenant_slugs: Iterable[str] | None = None) -> Census:
    """Перепись по активным предложениям; ``None`` — вся база."""
    offers = SalonService.objects.filter(is_active=True).select_related("tenant", "template")
    if tenant_slugs is not None:
        offers = offers.filter(tenant__slug__in=list(tenant_slugs))
    offers = list(offers.order_by("tenant__slug", "name", "pk"))

    edges: dict[object, list[EdgeVerdict]] = {}
    sellable_edges = (
        SpecialistService.objects.filter(sellable_offer_q(), sellable_q("specialist"))
        .filter(salon_service_id__in=[o.pk for o in offers])
        .select_related("salon_service__template")
        .order_by("pk")
    )
    for edge in sellable_edges:
        verdict, basis = edge.resolved_health_check()
        after, reasons = after_outcome(edge)
        edges.setdefault(edge.salon_service_id, []).append(
            EdgeVerdict(today=_TODAY[verdict], basis=basis, after=after, reasons=reasons)
        )

    census = Census()
    for offer in offers:
        canon = offer.template
        census.rows.append(
            OfferRow(
                pk=offer.pk,
                tenant_slug=offer.tenant.slug,
                name=offer.name,
                canon=NO_CANON if canon is None else (canon.canonical_code or NO_CODE),
                salon_answer=offer.requires_health_check,
                salon_confirmed=salon_answer_confirmed(offer),
                canon_flag=None if canon is None else canon.requires_health_check,
                canon_confirmed=(
                    canon is not None
                    and canon.health_check_origin == ServiceTemplate.HealthCheckOrigin.CONFIRMED
                ),
                edges=tuple(edges.get(offer.pk, ())),
            )
        )
    return census
