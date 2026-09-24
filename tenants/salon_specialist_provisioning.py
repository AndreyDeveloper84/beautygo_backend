"""Завести специалиста в УЖЕ СУЩЕСТВУЮЩЕМ салоне (DRF-2379).

Решение владельца §77 п.27 (24.09): привязка мастера к каталогу должна
происходить **сама**, когда салон заводит мастера. До этого листа она была
тремя шагами в двух системах с двумя правами.

# Почему нельзя было обойтись `solo_provisioning`

Соседний модуль заводит **тенант целиком** (`Tenant(kind=solo)` + рабочий
`User` + DRAFT-профиль). Салонному мастеру это не подходит по существу: у
салона тенант уже есть, и второй завёл бы параллельный кабинет с теми же
людьми. Поэтому здесь тенант **ищется**, а не создаётся, и его отсутствие —
отказ, а не повод создать.

# Какое поле здесь рождается и почему это важно

В зеркале бота живут **два разных** понятия, и их путали:

* ``CatalogMaster.linked_bot_user`` — «кто этот человек» (ADR-0008, роль
  мастера). Его пишет привязка личности.
* ``CatalogMaster.catalog_specialist_id`` — «каким ключом эту строку знает
  каталог» (DRF-1933). Его требуют часы и расписание.

Привязка человека второго НЕ создаёт, поэтому после неё часы отвечали 403
``CatalogSpecialistUnresolved``. Эта ручка рождает именно второе: возвращает
``specialist_id``, который бот кладёт в свою колонку.

# Идемпотентность

По ``SpecialistProfile.provisioned_external_user_id`` — тому же claim, что у
соло. Повтор с тем же ключом возвращает **того же** специалиста и ничего не
создаёт: бот вправе повторить вызов после обрыва, и второй специалист в
салоне был бы не «лишней строкой», а вторым человеком в расписании.

Claim — провенанс провижининга, **не ребро личности**: резолверы личности его
не читают, как и у соло.

# Роль

Сигнал даёт ``staff``, и здесь она такой и остаётся — в отличие от соло, где
владелец своего кабинета получает ``admin``. Специалист салона не
администрирует салон.
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

#: Пространство имён рабочих аккаунтов салонных специалистов. Вне ``bot:``
#: по той же причине, что у соло: иначе ``bind`` ответит ``collision``.
USERNAME_PREFIX = "salon-specialist:"


class SalonSpecialistRefused(Exception):
    """Отказ с машинной причиной: вызывающий различает их без разбора текста."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SalonSpecialist:
    profile: SpecialistProfile
    created: bool


def provision_salon_specialist(
    *,
    tenant_id: UUID | str,
    external_user_id: str,
    display_name: str,
) -> SalonSpecialist:
    """Завести специалиста в салоне ``tenant_id`` или вернуть заведённого.

    ``external_user_id`` — claim бота (``bot:max:<id>`` и родня), он же ключ
    идемпотентности. ``display_name`` пишется только при создании: на
    существующей строке здесь ничего не обновляется, как и у соло.

    Отказы (``SalonSpecialistRefused.reason``):

    * ``invalid_external_user_id`` — claim не той формы;
    * ``tenant_not_found`` — салона с таким id нет; заводить его здесь нельзя,
      это другая ручка и другое право;
    * ``tenant_is_solo`` — это кабинет соло-мастера, а не салон: специалиста
      туда заводит ``solo_provisioning``, и второй сломал бы «один кабинет —
      один человек»;
    * ``claim_bound_elsewhere`` — claim уже занят специалистом ДРУГОГО
      тенанта. Молча переселять человека между салонами нельзя.
    """
    if not is_valid_external_user_id(external_user_id):
        raise SalonSpecialistRefused("invalid_external_user_id")

    existing = _by_claim(external_user_id)
    if existing is not None:
        return _same_tenant_or_refuse(existing, tenant_id=tenant_id)

    tenant = Tenant.all_objects.filter(id=tenant_id).first()
    if tenant is None:
        raise SalonSpecialistRefused("tenant_not_found")
    if tenant.kind == Tenant.Kind.SOLO:
        raise SalonSpecialistRefused("tenant_is_solo")

    try:
        with transaction.atomic():
            specialist = _create(
                tenant=tenant,
                external_user_id=external_user_id,
                display_name=display_name,
            )
    except IntegrityError:
        # Гонка двух одинаковых вызовов: проигравший перечитывает по claim.
        # Тот же приём, что у соло, и по той же причине — повтор бота после
        # обрыва не должен рождать второго человека в расписании.
        existing = _by_claim(external_user_id)
        if existing is None:
            raise
        return _same_tenant_or_refuse(existing, tenant_id=tenant_id)

    logger.info(
        "tenants.salon_specialist.provisioned tenant=%s specialist=%s",
        tenant.id,
        specialist.profile.id,
    )
    return specialist


def _by_claim(external_user_id: str) -> SpecialistProfile | None:
    return (
        SpecialistProfile.objects
        .select_related("tenant", "user")
        .filter(provisioned_external_user_id=external_user_id)
        .first()
    )


def _same_tenant_or_refuse(
    profile: SpecialistProfile, *, tenant_id: UUID | str,
) -> SalonSpecialist:
    if str(profile.tenant_id) != str(tenant_id):
        raise SalonSpecialistRefused("claim_bound_elsewhere")
    return SalonSpecialist(profile=profile, created=False)


def _create(
    *, tenant: Tenant, external_user_id: str, display_name: str,
) -> SalonSpecialist:
    # Имя аккаунта выводится из claim, а не из имени человека: имена
    # повторяются, claim уникален, и на нём же стоит идемпотентность.
    user = User(
        username=f"{USERNAME_PREFIX}{external_user_id}",
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
    profile.save(
        update_fields=["display_name", "tenant", "provisioned_external_user_id"],
    )

    # Роль не трогаем: сигнал дал ``staff``, и специалист салона — именно
    # staff. Это единственное отличие от соло, где владелец кабинета
    # переписывается в ``admin``.
    if not TenantUserRelationship.objects.filter(
        user=user, tenant=tenant, is_active=True,
    ).exists():  # pragma: no cover — сигнал заводит строку; страховка от его правки
        TenantUserRelationship.objects.create(
            user=user,
            tenant=tenant,
            role=TenantUserRelationship.Role.STAFF,
            granted_by=TenantUserRelationship.GrantedBy.SYSTEM,
            is_active=True,
        )

    return SalonSpecialist(profile=profile, created=True)


__all__ = [
    "SalonSpecialist",
    "SalonSpecialistRefused",
    "provision_salon_specialist",
]
