"""Готовность к публикации и публикация соло-мастера (DRF-1796, M4; P78, P81–P83, P85).

Решения владельца, из которых сложен модуль:

* **G1 / ruling 6** — публикуется только связанный мастер (LINKED): до связи
  у него есть право настраивать workspace (M28), но не выставлять его
  клиентам. LINKED в каталоге — прокси бота с ``linked_user`` на рабочий
  аккаунт мастера (``bind_external_identity_by_operator``).
* **G2 → б** — ACTIVE ставит модератор. «Опубликовать» мастера переводит
  DRAFT → PENDING («Отправлен на проверку»); ACTIVE — только действие
  модератора, и оно проверяет ту же готовность (``approval_refusal``).
* **Готовность считает сервер одним местом** — эта функция; ручка
  readiness, публикация и гейт модератора читают её, а не свои копии.
  Ответ — поимённый список недостающего ``{code, section, detail}``;
  ``section`` — те же ключи, что у проекции бота (``services``,
  ``location``, ``hours``, ``profile``) плюс ``identity``. Ссылки экранов
  у бота (``onboarding_readiness.DEEP_LINKS``), каталог маршрутов Mini App
  не знает.

Пункты:

=====================  ==========  =========================================
code                   section     когда
=====================  ==========  =========================================
photo_missing          profile     ``SpecialistProfile.avatar`` пуст
display_name_missing   profile     имя пусто
no_configured_service  services    нет ни одной настроенной услуги (M8:
                                   активная строка + предложение с ценой и
                                   длительностью — ``SelectedService.configured``)
location_not_assigned  location    ``works_at`` пуст
location_not_          location    место есть, но не участвует в расстоянии —
participating                      тот же критерий, что у движка
                                   (``tenants.distance.participating_place_q``)
no_working_day         hours       нет рабочего дня с началом и концом
identity_not_linked    identity    связи нет
=====================  ==========  =========================================

У пунктов места ``detail.area_option = location_area_unavailable``: выезд и
«весь город» появятся с M11, до того этой дороги нет — она названа, а не
спрятана.

Только соло: каталог и публикацию салона ведёт владелец салона —
``salon_publication_owner_managed``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction

from appointments.models import SpecialistWorkingHours
from services.offer_selection import SelectionRefused, selected_services
from tenants.distance import participating_place_q
from tenants.models import Tenant
from users.models import SpecialistProfile, SpecialistPublicationRequest, User

logger = logging.getLogger(__name__)

SECTION_PROFILE = "profile"
SECTION_SERVICES = "services"
SECTION_LOCATION = "location"
SECTION_HOURS = "hours"
SECTION_IDENTITY = "identity"

#: Выезд / весь город — дорога к пункту «место», которой до M11 нет.
AREA_UNAVAILABLE = "location_area_unavailable"


@dataclass(frozen=True)
class MissingItem:
    code: str
    section: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "section": self.section, "detail": dict(self.detail)}


@dataclass(frozen=True)
class Readiness:
    missing: tuple[MissingItem, ...]

    @property
    def ready(self) -> bool:
        return not self.missing

    @property
    def codes(self) -> list[str]:
        return [item.code for item in self.missing]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "READY" if self.ready else "NOT_READY",
            "missing": [item.as_dict() for item in self.missing],
        }


class PublicationRefused(Exception):
    """Отказ с машинной причиной — 409 наружу, ничего не изменено."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PublicationNotReady(Exception):
    """Готовность неполна — публикации нет, отказ не записывается."""

    def __init__(self, readiness: Readiness) -> None:
        super().__init__("not_ready")
        self.readiness = readiness


@dataclass(frozen=True)
class PublishResult:
    request: SpecialistPublicationRequest
    replayed: bool


def solo_tenant(profile: SpecialistProfile) -> Tenant:
    tenant = profile.tenant
    if tenant is None:
        raise PublicationRefused("no_workspace_tenant")
    if tenant.kind != Tenant.Kind.SOLO:
        raise PublicationRefused("salon_publication_owner_managed")
    return tenant


def is_linked(profile: SpecialistProfile) -> bool:
    """LINKED: прокси бота связана с рабочим аккаунтом этого мастера."""

    return User.objects.filter(is_proxy=True, linked_user_id=profile.user_id).exists()


def publication_readiness(profile: SpecialistProfile) -> Readiness:
    solo_tenant(profile)
    missing: list[MissingItem] = []

    if not profile.avatar:
        missing.append(MissingItem("photo_missing", SECTION_PROFILE))
    if not (profile.display_name or "").strip():
        missing.append(MissingItem("display_name_missing", SECTION_PROFILE))

    try:
        entries = selected_services(profile)
    except SelectionRefused as exc:  # соло уже проверено; причина одна на оба модуля
        raise PublicationRefused(exc.reason) from exc
    if not any(entry.configured for entry in entries):
        missing.append(MissingItem(
            "no_configured_service",
            SECTION_SERVICES,
            {"selected": sum(1 for e in entries if e.salon_service.is_active), "configured": 0},
        ))

    if profile.works_at_id is None:
        missing.append(MissingItem(
            "location_not_assigned", SECTION_LOCATION, {"area_option": AREA_UNAVAILABLE},
        ))
    elif not SpecialistProfile.objects.filter(pk=profile.pk).filter(participating_place_q()).exists():
        missing.append(MissingItem(
            "location_not_participating", SECTION_LOCATION, {"area_option": AREA_UNAVAILABLE},
        ))

    has_working_day = SpecialistWorkingHours.objects.filter(
        specialist=profile,
        is_working_day=True,
        start_time__isnull=False,
        end_time__isnull=False,
    ).exists()
    if not has_working_day:
        missing.append(MissingItem("no_working_day", SECTION_HOURS))

    if not is_linked(profile):
        missing.append(MissingItem("identity_not_linked", SECTION_IDENTITY))

    return Readiness(tuple(missing))


def publish(profile: SpecialistProfile, command_id: UUID) -> PublishResult:
    """«Опубликовать»: один ключ команды — один переход и одна строка аудита.

    * повтор с тем же ``command_id`` — та же строка, без перехода;
    * ключ, уже использованный другим мастером, — ``command_id_reused``;
    * DRAFT и готовность полна (проверка свежая, под блокировкой профиля) —
      PENDING, ``submitted``; не полна — ``PublicationNotReady``, ничего не
      записано (повтор проверит заново);
    * уже PENDING / ACTIVE — строка с ``already_pending`` / ``already_active``
      без перехода.
    """

    solo_tenant(profile)
    replay = _replay(profile, command_id)
    if replay is not None:
        return replay

    Outcome = SpecialistPublicationRequest.Outcome
    Status = SpecialistProfile.ProfileStatus
    try:
        with transaction.atomic():
            # Блокируется только строка профиля: ``tenant`` и ``works_at`` —
            # nullable-связи, и FOR UPDATE по внешнему соединению Postgres
            # не принимает.
            locked = (
                SpecialistProfile.objects.select_for_update(of=("self",))
                .select_related("tenant", "works_at")
                .get(pk=profile.pk)
            )
            before = locked.status
            if before == Status.ACTIVE:
                outcome = Outcome.ALREADY_ACTIVE
            elif before == Status.PENDING:
                outcome = Outcome.ALREADY_PENDING
            else:
                readiness = publication_readiness(locked)
                if not readiness.ready:
                    raise PublicationNotReady(readiness)
                locked.status = Status.PENDING
                locked.save(update_fields=["status"])
                outcome = Outcome.SUBMITTED
            request = SpecialistPublicationRequest.objects.create(
                specialist=locked,
                command_id=command_id,
                outcome=outcome,
                from_status=before,
                to_status=locked.status,
            )
    except IntegrityError:
        # Гонка двух команд с одним ключом: переход проигравшего откатился
        # вместе с его строкой; отвечаем строкой победителя.
        replay = _replay(profile, command_id)
        if replay is None:
            raise
        return replay

    logger.info(
        "users.publication.command specialist=%s command=%s outcome=%s from=%s to=%s",
        profile.pk, command_id, request.outcome, request.from_status, request.to_status,
    )
    return PublishResult(request=request, replayed=False)


def publication_status(profile: SpecialistProfile) -> dict[str, Any]:
    solo_tenant(profile)
    last = (
        SpecialistPublicationRequest.objects
        .filter(specialist=profile)
        .order_by("-created_at")
        .first()
    )
    return {
        "specialist_id": str(profile.pk),
        "profile_status": profile.status,
        "readiness": publication_readiness(profile).as_dict(),
        "last_request": None if last is None else request_as_dict(last),
    }


def approval_refusal(profile: SpecialistProfile) -> list[str] | None:
    """Гейт модератора: ``None`` — можно активировать, иначе коды недостающего.

    Только соло-тенант: салонного мастера активируют как прежде (каталог и
    публикацию салона ведёт владелец салона) — разница названа тестом.
    """

    tenant = profile.tenant
    if tenant is None or tenant.kind != Tenant.Kind.SOLO:
        return None
    codes = publication_readiness(profile).codes
    return codes or None


def request_as_dict(request: SpecialistPublicationRequest) -> dict[str, Any]:
    return {
        "id": str(request.pk),
        "command_id": str(request.command_id),
        "outcome": request.outcome,
        "from_status": request.from_status,
        "to_status": request.to_status,
        "created_at": request.created_at.isoformat(),
    }


def _replay(profile: SpecialistProfile, command_id: UUID) -> PublishResult | None:
    existing = SpecialistPublicationRequest.objects.filter(command_id=command_id).first()
    if existing is None:
        return None
    if existing.specialist_id != profile.pk:
        raise PublicationRefused("command_id_reused")
    return PublishResult(request=existing, replayed=True)


__all__ = [
    "AREA_UNAVAILABLE",
    "MissingItem",
    "PublicationNotReady",
    "PublicationRefused",
    "PublishResult",
    "Readiness",
    "approval_refusal",
    "is_linked",
    "publication_readiness",
    "publication_status",
    "publish",
    "request_as_dict",
    "solo_tenant",
]
