"""Предложение по старым адресам мастеров — только печать (§9, H4 → а; DRF-1924).

Зачем
-----

На пилоте 15.09 у 27 профилей мастеров есть старый ``SpecialistProfile.address``
и нет ``works_at`` ни к одному ``ServiceLocation`` (docs/MEASURE_L8B). §9
запрещает массовый перенос, H4 (владелец 15.09) — сначала предложение по
каждой строке, решение — у владельца, запись — отдельным листом после его
подтверждения. Эта команда — только первая половина: она **ничего не пишет**
и флага записи у неё нет вовсе.

Что печатает
------------

Строку на каждого мастера со старым адресом и без места, с одним из
четырёх классов:

* **A** — у его тенанта есть место с тем же адресом → привязать к нему;
* **B** — места нет, но адрес совпадает с адресом тенанта → сначала
  ``promote_tenant_location --slug`` (место салона, ``review_required``),
  потом привязать;
* **C** — адрес расходится и с местами тенанта, и с его адресом → оставить,
  решает владелец (личный или тестовый адрес → ``inactive``, иначе новое место);
* **D** — у тенанта нет ни адреса, ни мест (или тенанта нет) → оставить.

Привязка предлагается только к месту **того же** тенанта. Сравнение адресов —
``tenants.service_location.same_address``, то же, что у ``promote_tenant_location``.

Чего не печатает без флага
--------------------------

Адресов. Старый адрес мастера бывает домашним, а замер L8b шёл без адресов.
``--with-address`` добавляет их для экрана владельца и печатает в шапке
предупреждение: вывод с адресами в docs, Linear и чат не переносится.

Ограничение, названное в шапке
------------------------------

``Tenant.kind`` у всего, что заведено до G4, — ``salon`` по умолчанию
(``SOLO`` пишет только provisioning). «Салон» в строке — признак, а не
доказательство; рядом печатается число мастеров в тенанте.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from django.core.management.base import BaseCommand
from django.db.models import Count

from core.measurement_subject import gather_pulse, subject_lines
from tenants.models import ServiceLocation
from tenants.service_location import same_address
from users.models import SpecialistProfile

CLASS_LINK = "A"
CLASS_PROMOTE_THEN_LINK = "B"
CLASS_OWNER_DECIDES = "C"
CLASS_NOTHING_TO_LINK = "D"
CLASSES = (CLASS_LINK, CLASS_PROMOTE_THEN_LINK, CLASS_OWNER_DECIDES, CLASS_NOTHING_TO_LINK)

KIND_LIMIT_LINE = (
    "  ограничение: kind=salon у тенантов, заведённых до G4, — умолчание, а не доказательство салона; "
    "смотрите «мастеров в тенанте»"
)
PII_WARNING_LINE = "  ВНИМАНИЕ: вывод содержит адреса мастеров (ПДн) — в docs, Linear и чат не переносить"


class Command(BaseCommand):
    help = (
        "Предложение по старым адресам мастеров без места (§9, H4): класс и действие на строку. "
        "Только печать — в базу не пишет, флага записи нет."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--with-address", action="store_true",
            help="Печатать адреса (ПДн) — только для экрана владельца, не для docs/Linear/чата.",
        )

    def handle(self, *args, **options) -> None:
        with_address = options["with_address"]
        for line in subject_lines(pulses=gather_pulse()):
            self.stdout.write(line)
        self.stdout.write("")

        self.stdout.write("== ПРЕДЛОЖЕНИЕ ПО СТАРЫМ АДРЕСАМ МАСТЕРОВ (§9, H4) · только печать, в базу не пишет ==")
        self.stdout.write(KIND_LIMIT_LINE)
        if with_address:
            self.stdout.write(self.style.WARNING(PII_WARNING_LINE))

        profiles = list(SpecialistProfile._base_manager.select_related("tenant").order_by("tenant__slug", "pk"))
        legacy = [p for p in profiles if (p.address or "").strip()]
        placed = [p for p in legacy if p.works_at_id is not None]
        unplaced = [p for p in legacy if p.works_at_id is None]

        tenant_ids = {p.tenant_id for p in unplaced if p.tenant_id is not None}
        places_by_tenant: dict = defaultdict(list)
        for place in ServiceLocation._base_manager.filter(tenant_id__in=tenant_ids).order_by("created_at", "pk"):
            places_by_tenant[place.tenant_id].append(place)
        masters_in_tenant = dict(
            SpecialistProfile._base_manager.filter(tenant_id__in=tenant_ids)
            .values("tenant_id").annotate(n=Count("pk")).values_list("tenant_id", "n")
        )
        place_statuses = Counter(place.status for places in places_by_tenant.values() for place in places)

        self.stdout.write(f"  профилей мастеров всего        : {len(profiles)}")
        self.stdout.write(f"  со старым адресом              : {len(legacy)}")
        self.stdout.write(f"  уже с местом (исключены)       : {len(placed)}")
        self.stdout.write(f"  без места (строки ниже)        : {len(unplaced)}")
        self.stdout.write(f"  тенантов у строк               : {len(tenant_ids)}")
        statuses = ", ".join(f"{status}={n}" for status, n in sorted(place_statuses.items())) or "нет"
        self.stdout.write(f"  мест у этих тенантов           : {sum(place_statuses.values())} ({statuses})")
        self.stdout.write("")

        by_class: Counter = Counter()
        for profile in unplaced:
            klass, proposal = self._propose(profile, places_by_tenant.get(profile.tenant_id, []))
            by_class[klass] += 1
            tenant = profile.tenant
            who = (
                f"тенант={tenant.slug} ({tenant.kind}, мастеров в тенанте {masters_in_tenant.get(tenant.pk, 0)})"
                if tenant is not None else "тенант=— (не назначен)"
            )
            self.stdout.write(f"  [{klass}] профиль={profile.pk} · {who} → {proposal}")
            if with_address:
                self.stdout.write(f"      адрес мастера: {profile.address}")
                if tenant is not None and (tenant.address or "").strip():
                    self.stdout.write(f"      адрес тенанта: {tenant.address}")

        self.stdout.write("")
        counts = " ".join(f"{klass}={by_class[klass]}" for klass in CLASSES)
        self.stdout.write(f"  итого по классам: {counts} · сумма {sum(by_class.values())} = без места {len(unplaced)}")

    @staticmethod
    def _propose(profile: SpecialistProfile, places: list) -> tuple[str, str]:
        matches = [place for place in places if same_address(place.address, profile.address)]
        if matches:
            targets = ", ".join(f"{place.pk} [{place.status}]" for place in matches)
            if len(matches) == 1:
                return CLASS_LINK, f"привязать к месту {targets}"
            return CLASS_LINK, f"привязать к одному из мест {targets} — какое, решает владелец"
        tenant = profile.tenant
        tenant_address = (tenant.address or "").strip() if tenant is not None else ""
        if tenant_address and same_address(tenant_address, profile.address):
            return (
                CLASS_PROMOTE_THEN_LINK,
                f"сначала promote_tenant_location --slug {tenant.slug} (review_required; confirm — по слову "
                "владельца), затем привязать к созданному месту",
            )
        if tenant_address or places:
            return (
                CLASS_OWNER_DECIDES,
                "оставить: адрес расходится с местами и адресом тенанта — решает владелец "
                "(личный или тестовый → inactive, иначе новое место)",
            )
        return CLASS_NOTHING_TO_LINK, "оставить: у тенанта нет ни адреса, ни мест — сначала нужен адрес места"
