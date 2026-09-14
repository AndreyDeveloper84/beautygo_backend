"""Provisioning solo-workspace: Tenant с UUID бота + рабочий User + DRAFT-профиль (DRF-1828, G1/G4).

Решение владельца 12.09 (`PROMPT_AGENT_MASTER_ONBOARDING_G1_G6_VERTICAL_SLICE.md`
§1, §4): при создании solo workspace каталог сразу заводит ``Tenant`` и
``SpecialistProfile(status=DRAFT)``; pre-LINKED — workspace setup
authority, НЕ identity authority; после LINKED новый профиль не создаётся —
существующий DRAFT привязывается к подтверждённой личности одной записью.

Дизайн (B) из pre-flight (`docs/PREFLIGHT_MASTER_ONBOARDING_G1G3G4_2026-09-12.md`
§(а)) — ни один FK при LINKED не движется:

===============  =================================================================
что создаётся    зачем
===============  =================================================================
``Tenant`` T     ``id`` = ``Tenant.id`` бота (одна связь — общий UUID, как у
                 салонов ``connect_salon``), ``kind=solo``, ``slug=solo-…``
``User`` W       рабочий real-аккаунт специалиста: ``role=specialist``,
                 ``is_proxy=False``, ``username`` ВНЕ пространства ``bot:``
                 (иначе ``bind`` ответит ``collision``), пароль непригоден
``SpecialistProfile``  сигнал ``users.signals.create_user_profile`` заводит
                 DRAFT; здесь дописываются ``display_name``, ``tenant`` и claim
``TenantUserRelationship``  сигнал даёт ``staff``; владелец своего workspace —
                 ``admin`` (``granted_by=system``), иначе ``IsTenantAdmin``
                 его не пустит
===============  =================================================================

**Claim** ``SpecialistProfile.provisioned_external_user_id`` = ``bot:max:<id>``
— провенанс провижининга, не ребро личности. Идемпотентность ручки — по
нему: повтор с тем же claim и тем же tenant/slug → тот же workspace; тот
же slug/UUID с ДРУГИМ claim → отказ. Резолверы личности его не читают, и
это стережёт тест (риск 1 pre-flight: claim не должен стать второй
личностью).

**LINKED** потом делает оператор существующим
``bind_external_identity_by_operator(external_user_id, W.id)``:
``proxy(bot:max:X).linked_user = W`` — профиль тот же, второй профиль
``OneToOne`` не допускает.

Что ручка НЕ делает — и это её scope (риск §17): не принимает id
существующего специалиста/тенанта для правки, ничего не обновляет на
существующих строках, не создаёт ничего в чужом тенанте. Утёкший
provisioning-токен = право завести ещё один пустой solo-workspace, не
право писать в чужие.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from django.db import IntegrityError, transaction

from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User
from users.services import is_valid_external_user_id

logger = logging.getLogger(__name__)

#: Пространство имён рабочего аккаунта: НЕ ``bot:`` (там живут прокси —
#: ``bind`` отказал бы ``collision``) и не телефон.
USERNAME_PREFIX = "solo:"


class SoloProvisioningRefused(Exception):
    """Отказ с машинной причиной — 409 наружу, ничего не создано."""

    def __init__(self, reason: str, **details) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


@dataclass(frozen=True)
class SoloWorkspace:
    tenant: Tenant
    user: User
    profile: SpecialistProfile
    created: bool


def provision_solo_workspace(
    *,
    tenant_id: UUID,
    slug: str,
    name: str,
    city: str,
    external_user_id: str,
    display_name: str,
) -> SoloWorkspace:
    """Завести solo-workspace или вернуть уже заведённый по тому же claim.

    Идемпотентно по ``external_user_id`` (claim). Любое несовпадение —
    отказ: slug занят другим тенантом, UUID занят другим тенантом,
    claim заведён под другим slug/UUID. На существующих строках ничего
    не обновляется.
    """

    slug = slug.strip()
    name = name.strip()
    city = (city or "").strip()
    display_name = " ".join((display_name or "").split())
    if not is_valid_external_user_id(external_user_id):
        raise SoloProvisioningRefused("invalid_external_user_id")
    if not display_name:
        raise SoloProvisioningRefused("display_name_required")

    existing = _by_claim(external_user_id)
    if existing is not None:
        return _same_workspace_or_refuse(existing, tenant_id=tenant_id, slug=slug)

    _refuse_if_taken(tenant_id=tenant_id, slug=slug)

    try:
        with transaction.atomic():
            workspace = _create(
                tenant_id=tenant_id,
                slug=slug,
                name=name,
                city=city,
                external_user_id=external_user_id,
                display_name=display_name,
            )
    except IntegrityError:
        # Гонка двух одинаковых вызовов: проигравший перечитывает по claim
        # и проходит ту же сверку; чужой slug/UUID — отказ, как без гонки.
        existing = _by_claim(external_user_id)
        if existing is None:
            _refuse_if_taken(tenant_id=tenant_id, slug=slug)
            raise
        return _same_workspace_or_refuse(existing, tenant_id=tenant_id, slug=slug)

    logger.info(
        "tenants.solo_provisioning.created tenant=%s slug=%s specialist=%s user=%s",
        workspace.tenant.id, workspace.tenant.slug, workspace.profile.id, workspace.user.id,
    )
    return workspace


def _by_claim(external_user_id: str) -> SpecialistProfile | None:
    return (
        SpecialistProfile.objects
        .select_related("user", "tenant")
        .filter(provisioned_external_user_id=external_user_id)
        .first()
    )


def _same_workspace_or_refuse(
    profile: SpecialistProfile, *, tenant_id: UUID, slug: str,
) -> SoloWorkspace:
    tenant = profile.tenant
    if tenant is None or tenant.id != tenant_id or tenant.slug != slug:
        raise SoloProvisioningRefused(
            "claim_bound_elsewhere",
            existing_tenant_id=str(tenant.id) if tenant is not None else None,
            existing_slug=tenant.slug if tenant is not None else None,
        )
    return SoloWorkspace(tenant=tenant, user=profile.user, profile=profile, created=False)


def _refuse_if_taken(*, tenant_id: UUID, slug: str) -> None:
    by_id = Tenant.all_objects.filter(id=tenant_id).first()
    if by_id is not None:
        raise SoloProvisioningRefused("tenant_id_taken", existing_slug=by_id.slug)
    by_slug = Tenant.all_objects.filter(slug=slug).first()
    if by_slug is not None:
        raise SoloProvisioningRefused("slug_taken", existing_tenant_id=str(by_slug.id))
    # DRF-1874: рабочий аккаунт без тенанта и claim (ручная правка, откат) —
    # его username занят; без этой проверки create падал IntegrityError → 500.
    username = f"{USERNAME_PREFIX}{slug}"
    if User.objects.filter(username=username).exists():
        raise SoloProvisioningRefused("username_taken", username=username)


def _create(
    *,
    tenant_id: UUID,
    slug: str,
    name: str,
    city: str,
    external_user_id: str,
    display_name: str,
) -> SoloWorkspace:
    tenant = Tenant.all_objects.create(
        id=tenant_id, slug=slug, name=name, city=city, kind=Tenant.Kind.SOLO,
    )
    user = User(
        username=f"{USERNAME_PREFIX}{slug}",
        role="specialist",
        is_proxy=False,
        tenant=tenant,
        phone=None,
    )
    user.set_unusable_password()
    user.save()  # сигналы: Profile, SpecialistProfile(DRAFT), TUR(staff)

    profile = SpecialistProfile.objects.get(user=user)
    profile.display_name = display_name
    profile.tenant = tenant
    profile.provisioned_external_user_id = external_user_id
    profile.save(update_fields=["display_name", "tenant", "provisioned_external_user_id"])

    # Владелец своего workspace — admin, не staff: сигнал даёт staff,
    # а вторую активную строку на пару (user, tenant) запрещает
    # ``tur_unique_active`` — значит, переписываем ту же.
    TenantUserRelationship.objects.filter(user=user, tenant=tenant, is_active=True).update(
        role=TenantUserRelationship.Role.ADMIN,
        granted_by=TenantUserRelationship.GrantedBy.SYSTEM,
    )
    return SoloWorkspace(tenant=tenant, user=user, profile=profile, created=True)


__all__ = ["SoloProvisioningRefused", "SoloWorkspace", "provision_solo_workspace"]
