"""Место работы соло-мастера — один путь записи (DRF-1803, M11; §9, D3, D4).

**Кто пишет.** Мастер сам, из бота, про свой workspace — через
``/internal/specialists/{id}/service-locations/``
(:mod:`users.internal_service_locations_api`). До этого места писали только
admin и ``promote_tenant_location``.

**Статус.** Место, которое назвал сам мастер, — происхождение не подтверждено
человеком → ``REVIEW_REQUIRED`` (§9). Подтверждение — отдельное действие
человека с провенансом; эта запись его не ставит и не сохраняет чужое:
сменился адрес — подтверждение и координаты прежнего адреса снимаются.

**Только соло.** Место салона ведёт владелец салона; профиль без workspace —
отказ по имени. Тенант места — всегда workspace самого мастера (D4 → а): во
вводе его нет и быть не может.

**Одно место.** ``works_at`` — пока один FK (D3 → а; несколько мест —
DRF-1956). Второе своё место — отказ ``place_already_set``, не молчаливая
замена. Выезд — не место, а :class:`~tenants.service_area.ServiceArea`,
поэтому «кабинет + выезд» — две строки уже сейчас.

**Город** — из ``Tenant.city`` соло-workspace, не из ввода.
"""
from __future__ import annotations

from django.db import transaction

from tenants.models import (
    AreaCoverage,
    AreaKind,
    GeocodeStatus,
    LocationKind,
    LocationStatus,
    ServiceArea,
    ServiceLocation,
    Tenant,
)
from tenants.service_location import same_address
from users.models import SpecialistProfile

ADDRESS_MAX = 500
LABEL_MAX = 120
NOTE_MAX = 200

REASON_NO_WORKSPACE = "no_workspace_tenant"
REASON_SALON = "salon_place_owner_managed"
REASON_PLACE_ALREADY_SET = "place_already_set"
REASON_PLACE_OUTSIDE_WORKSPACE = "place_outside_workspace"
REASON_AREA_ALREADY_SET = "area_already_set"

PLACE_KINDS = frozenset({LocationKind.PRIVATE_STUDIO.value, LocationKind.SALON_OR_STUDIO.value})
AREA_KINDS = frozenset({AreaKind.MOBILE.value})
COVERAGES = frozenset({AreaCoverage.WHOLE_CITY.value, AreaCoverage.LATER.value})

#: Подтверждение человеком — о прежнем адресе.
_CONFIRMATION_RESET = {"status": LocationStatus.REVIEW_REQUIRED, "confirmed_by": None,
                       "confirmed_at": None, "confirmed_source_ref": ""}
#: Происхождение координат — тоже о прежнем адресе.
_GEOCODE_RESET = {
    "geocode_source_address": "", "geocode_normalized_address": "", "latitude": None, "longitude": None,
    "geocode_provider": "", "geocode_precision": "", "geocode_status": GeocodeStatus.NOT_ATTEMPTED,
    "geocoded_at": None,
}


class PlaceRefused(Exception):
    """Отказ с машинной причиной — 409 наружу, ничего не записано."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def solo_workspace(profile: SpecialistProfile) -> Tenant:
    tenant = profile.tenant
    if tenant is None:
        raise PlaceRefused(REASON_NO_WORKSPACE)
    if tenant.kind != Tenant.Kind.SOLO:
        raise PlaceRefused(REASON_SALON)
    return tenant


def own_place(profile: SpecialistProfile, tenant: Tenant) -> ServiceLocation | None:
    """Место мастера, если оно в его workspace; иное место — не его место."""
    place = profile.works_at
    return place if place is not None and place.tenant_id == tenant.pk else None


def place_dict(place: ServiceLocation) -> dict:
    return {
        "id": str(place.pk),
        "kind": place.kind,
        "label": place.label,
        "address": place.address,
        "city": place.city,
        "note_for_client": place.note_for_client,
        "status": place.status,
        "geocode_status": place.geocode_status,
        "latitude": None if place.latitude is None else str(place.latitude),
        "longitude": None if place.longitude is None else str(place.longitude),
        # Адрес назовут клиенту (после публикации профиля) только у места,
        # подтверждённого человеком (tenants.distance.offer_address).
        "shown_to_clients_after_publication": place.status == LocationStatus.CONFIRMED,
    }


def area_dict(area: ServiceArea) -> dict:
    return {
        "id": str(area.pk),
        "kind": area.kind,
        "city": area.city,
        "coverage": area.coverage,
        "configured": area.coverage == AreaCoverage.WHOLE_CITY,
    }


def state(profile: SpecialistProfile) -> dict:
    tenant = solo_workspace(profile)
    place = own_place(profile, tenant)
    areas = ServiceArea.objects.filter(specialist=profile).order_by("kind", "created_at")
    return {
        "specialist_id": str(profile.pk),
        "city": tenant.city,
        "places": [place_dict(place)] if place is not None else [],
        "areas": [area_dict(area) for area in areas],
    }


def owned_item(profile: SpecialistProfile, item_id) -> ServiceLocation | ServiceArea | None:
    tenant = solo_workspace(profile)
    place = own_place(profile, tenant)
    if place is not None and str(place.pk) == str(item_id):
        return place
    return ServiceArea.objects.filter(pk=item_id, specialist=profile).first()


def _locked_profile(profile_id) -> SpecialistProfile:
    return (
        SpecialistProfile.objects.select_for_update(of=("self",))
        .select_related("tenant", "works_at")
        .get(pk=profile_id)
    )


@transaction.atomic
def create_place(profile_id, *, kind: str, address: str, label: str = "", note_for_client: str = "") -> ServiceLocation:
    profile = _locked_profile(profile_id)
    tenant = solo_workspace(profile)
    if profile.works_at_id is not None:
        reason = REASON_PLACE_ALREADY_SET if own_place(profile, tenant) else REASON_PLACE_OUTSIDE_WORKSPACE
        raise PlaceRefused(reason)
    place = ServiceLocation(
        tenant=tenant, kind=kind, label=label, address=address, city=tenant.city or "",
        note_for_client=note_for_client, status=LocationStatus.REVIEW_REQUIRED,
    )
    place.full_clean()
    place.save()
    profile.works_at = place
    profile.clean()
    profile.save(update_fields=["works_at", "updated_at"])
    return place


@transaction.atomic
def create_area(profile_id, *, coverage: str) -> ServiceArea:
    profile = _locked_profile(profile_id)
    tenant = solo_workspace(profile)
    if ServiceArea.objects.filter(specialist=profile, kind=AreaKind.MOBILE).exists():
        raise PlaceRefused(REASON_AREA_ALREADY_SET)
    area = ServiceArea(specialist=profile, kind=AreaKind.MOBILE, city=tenant.city or "", coverage=coverage)
    area.full_clean()
    area.save()
    return area


@transaction.atomic
def update_place(profile_id, place_id, fields: dict) -> ServiceLocation | None:
    profile = _locked_profile(profile_id)
    tenant = solo_workspace(profile)
    current = own_place(profile, tenant)
    if current is None or str(current.pk) != str(place_id):
        return None
    place = ServiceLocation.objects.select_for_update().get(pk=current.pk)
    changed: set[str] = set()
    if "address" in fields:
        if not same_address(fields["address"], place.address):
            for name, value in {**_CONFIRMATION_RESET, **_GEOCODE_RESET}.items():
                setattr(place, name, value)
            changed |= set(_CONFIRMATION_RESET) | set(_GEOCODE_RESET)
        place.address = fields["address"]
        changed.add("address")
    for name in ("kind", "label", "note_for_client"):
        if name in fields:
            setattr(place, name, fields[name])
            changed.add(name)
    if changed:
        place.full_clean()
        place.save(update_fields=[*sorted(changed), "updated_at"])
    return place


@transaction.atomic
def update_area(profile_id, area_id, *, coverage: str) -> ServiceArea | None:
    profile = _locked_profile(profile_id)
    solo_workspace(profile)
    area = ServiceArea.objects.select_for_update().filter(pk=area_id, specialist=profile).first()
    if area is None:
        return None
    area.coverage = coverage
    area.full_clean()
    area.save(update_fields=["coverage", "updated_at"])
    return area
