"""Администратор салона из админки бота — свежая учётка + TUR + связь (DRF-2085).

OWNER RULING 18.09 (вариант А). До этого листа администратор, получивший
код в боте, упирался в ``IsTenantAdmin`` на первом действии: прокси
``bot:max:<id>`` не связан ни с какой учёткой, у прокси нет TUR. Связать
было чем только руками — ``provision_salon_admin`` по SSH и затем
«Связать с Ayla → Администратор салона» в админке каталога.

Здесь — одна операция, которую запускает оператор бота
(``platform_operations`` в его Django Admin) через ручку
``POST /api/v1/internal/tenants/<slug>/salon-admins/`` под собственным
credential (:class:`users.permissions.IsSalonAdminLinkBearer`). Контракт
ruling'а, пункт за пунктом:

2. вход — actor, MAX identity, salon (slug в адресе), correlation_id,
   idempotency_key;
3. салон существует и активен — иначе ``tenant_not_found`` (404) /
   ``tenant_inactive`` (409); slug приходит от оператора, проверяет каталог;
4. MAX identity не связана с другой учёткой — иначе ``identity_already_bound``;
5. совпадение с существующим человеком или неоднозначность — FAIL_CLOSED:
   под внешним id уже лежит НАСТОЯЩАЯ учётка → ``identity_not_proxy``;
   личность уже кому-то привязана → п.4. Ни то ни другое здесь не
   разрешается — это оператор каталога руками;
6. создаётся только СВЕЖАЯ учётка: ``username`` вне пространства ``bot:``
   и вне телефонов, ``role=admin``, ``is_proxy=False``, не staff, пароль
   непригоден, без телефона и имени (лишних PII нет);
7. создаётся только TUR ``role=admin`` в названном салоне, ``granted_by=admin``;
8. повтор с тем же ключом — те же id, ничего нового; тот же ключ с другим
   телом — ``idempotency_key_reused``;
9. SUCCESS только после authoritative readback: после коммита личность
   резолвится ``resolve_external_user_readonly`` в созданную учётку, и
   именно :class:`users.permissions.IsTenantAdmin` — та проверка, что
   стоит на салонной поверхности, — отвечает «да» для этого салона;
   иначе ``readback_failed``;
10. аудит: строка :class:`users.models.SalonAdminLinkRequest` (внутри той
    же транзакции) + событие привязки ``emit_identity_binding`` с
    initiator ``bot_salon_admin_link`` (строгое, внутри транзакции бинда)
    + лог с correlation_id; внешний id в лог не пишется (``pii_guard``
    считает идентификаторы каналов персональными данными).

Что операция НЕ делает — и это её scope: не трогает существующие учётки
и TUR, не перепривязывает, не выдаёт роль, отличную от admin, не
принимает id учётки на вход. Утёкший credential = право завести
администратора в салоне, который назван slug'ом, и ничего больше; за это
он и отдельный (п.11), и с лимитом (п.12).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

from django.db import transaction

from tenants.models import Tenant
from users.models import SalonAdminLinkRequest, TenantUserRelationship, User
from users.permissions import IsTenantAdmin
from users.services import (
    INITIATOR_BOT_SALON_ADMIN_LINK,
    IdentityBindingError,
    InvalidExternalUserIDError,
    bind_external_identity,
    is_valid_external_user_id,
    resolve_external_user_readonly,
)

logger = logging.getLogger(__name__)

#: Пространство имён свежей учётки: не ``bot:`` (там прокси — бинд ответил
#: бы ``collision``), не ``solo:`` (рабочие аккаунты соло-мастеров) и не
#: телефон (так заводит ``provision_salon_admin``).
USERNAME_PREFIX = "salon-admin:"

#: Единственная роль, которую ручка выдаёт (ruling: «роль, отличная от
#: salon_admin» — запрещено). В каталоге это ``TenantUserRelationship.Role.ADMIN``.
LINK_ROLE = TenantUserRelationship.Role.ADMIN

REASON_INVALID_EXTERNAL_ID = "invalid_external_user_id"
REASON_TENANT_NOT_FOUND = "tenant_not_found"
REASON_TENANT_INACTIVE = "tenant_inactive"
REASON_IDENTITY_NOT_PROXY = "identity_not_proxy"
REASON_IDENTITY_ALREADY_BOUND = "identity_already_bound"
REASON_IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
REASON_BIND_REFUSED = "bind_refused"
REASON_READBACK_FAILED = "readback_failed"

#: HTTP-статус по причине — ручка отдаёт один код ошибки и причину именем.
STATUS_BY_REASON: dict[str, int] = {
    REASON_INVALID_EXTERNAL_ID: 400,
    REASON_TENANT_NOT_FOUND: 404,
    REASON_TENANT_INACTIVE: 409,
    REASON_IDENTITY_NOT_PROXY: 409,
    REASON_IDENTITY_ALREADY_BOUND: 409,
    REASON_IDEMPOTENCY_KEY_REUSED: 409,
    REASON_BIND_REFUSED: 409,
    REASON_READBACK_FAILED: 500,
}


class SalonAdminLinkRefused(Exception):
    """Отказ с машинной причиной; при отказе ничего не создано."""

    def __init__(self, reason: str, **details) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details
        self.status_code = STATUS_BY_REASON[reason]


@dataclass(frozen=True)
class SalonAdminLink:
    tenant: Tenant
    user: User
    relationship: TenantUserRelationship
    request: SalonAdminLinkRequest
    created: bool


def _refuse(reason: str, *, correlation_id: str, tenant_slug: str, **details) -> SalonAdminLinkRefused:
    # Внешний id в лог не пишется (pii_guard: идентификатор канала — ПД);
    # зацепка оператору — correlation_id, slug и причина.
    logger.warning(
        "users.salon_admin_link.refused reason=%s tenant=%s correlation_id=%s details=%s",
        reason, tenant_slug, correlation_id or "-", details,
    )
    return SalonAdminLinkRefused(reason, **details)


def _readback(tenant: Tenant, external_user_id: str, user: User) -> bool:
    """Пункт 9: тот же резолвер и та же permission, что на салонной поверхности."""
    resolved = resolve_external_user_readonly(external_user_id)
    if resolved is None or resolved.pk != user.pk or resolved.is_proxy:
        return False
    probe = SimpleNamespace(user=resolved, tenant=tenant)
    return IsTenantAdmin().has_permission(probe, None)


def link_salon_admin(
    *,
    tenant_slug: str,
    external_user_id: str,
    actor: str,
    correlation_id: str = "",
    idempotency_key: str,
) -> SalonAdminLink:
    """Завести свежего администратора салона и связать с ним MAX-личность.

    Возвращает :class:`SalonAdminLink`; ``created=False`` — повтор с тем же
    ключом (ничего не создано, те же id, readback выполнен заново).
    Отказ — :class:`SalonAdminLinkRefused` с причиной; см. модуль.
    """
    if not is_valid_external_user_id(external_user_id):
        raise _refuse(REASON_INVALID_EXTERNAL_ID, correlation_id=correlation_id, tenant_slug=tenant_slug)

    # Пункт 3 — салон существует и активен. ``all_objects``: неактивный
    # салон отличаем от несуществующего по имени, а не одним 404.
    tenant = Tenant.all_objects.filter(slug=tenant_slug).first()
    if tenant is None:
        raise _refuse(REASON_TENANT_NOT_FOUND, correlation_id=correlation_id, tenant_slug=tenant_slug)
    if not tenant.is_active:
        raise _refuse(REASON_TENANT_INACTIVE, correlation_id=correlation_id, tenant_slug=tenant_slug)

    with transaction.atomic():
        # Пункт 8 — ключ идемпотентности. Строка есть → либо тот же запрос
        # (повтор), либо чужое тело под тем же ключом (отказ по имени).
        # Без select_related: FOR UPDATE на nullable-стороне outer join
        # Postgres не принимает (user SET_NULL), а блокировка нужна именно
        # строке ключа — два повтора с одним ключом ждут друг друга здесь.
        prior = (
            SalonAdminLinkRequest.objects.select_for_update()
            .filter(idempotency_key=idempotency_key)
            .first()
        )
        if prior is not None:
            if prior.tenant_id != tenant.id or prior.external_user_id != external_user_id:
                raise _refuse(
                    REASON_IDEMPOTENCY_KEY_REUSED, correlation_id=correlation_id, tenant_slug=tenant_slug,
                )
            user = prior.user
            relationship = (
                TenantUserRelationship.objects.filter(
                    user=user, tenant=tenant, role=LINK_ROLE, is_active=True,
                ).first()
                if user is not None
                else None
            )
            if user is None or relationship is None:
                raise _refuse(REASON_READBACK_FAILED, correlation_id=correlation_id, tenant_slug=tenant_slug)
            result = SalonAdminLink(
                tenant=tenant, user=user, relationship=relationship, request=prior, created=False,
            )
        else:
            # Пункты 4–5 — состояние внешнего id ДО того, как что-либо создано.
            # Под блокировкой, чтобы гонка двух операторов не завела двух
            # администраторов на одну личность: второй увидит linked_user.
            existing = (
                User.objects.select_for_update().filter(username=external_user_id).first()
            )
            if existing is not None and not existing.is_proxy:
                raise _refuse(
                    REASON_IDENTITY_NOT_PROXY, correlation_id=correlation_id, tenant_slug=tenant_slug,
                )
            if existing is not None and existing.linked_user_id is not None:
                raise _refuse(
                    REASON_IDENTITY_ALREADY_BOUND, correlation_id=correlation_id, tenant_slug=tenant_slug,
                )

            # Пункт 6 — свежая учётка. ``tenant=None`` нарочно: сигнал
            # ``users.signals`` заводил бы TUR сам с ``granted_by=self``, а
            # здесь выдача — решение оператора (пункт 7, ``granted_by=admin``).
            user = User(
                username=f"{USERNAME_PREFIX}{tenant.slug}:{uuid.uuid4().hex[:12]}",
                role="admin",
                is_proxy=False,
                is_guest=False,
                is_staff=False,
                is_superuser=False,
                tenant=None,
                phone=None,
            )
            user.set_unusable_password()
            user.save()
            relationship = TenantUserRelationship.objects.create(
                user=user,
                tenant=tenant,
                role=LINK_ROLE,
                is_active=True,
                granted_by=TenantUserRelationship.GrantedBy.ADMIN,
            )
            # Связь — той же единственной дверью, что у оператора каталога и у
            # bind-external; цель сужена до ``admin`` (bindable_target_q
            # требует активную admin-TUR — она только что создана).
            try:
                bind_external_identity(
                    external_user_id,
                    user.pk,
                    initiator=INITIATOR_BOT_SALON_ADMIN_LINK,
                    request_id=correlation_id or None,
                    target_roles=(LINK_ROLE,),
                )
            except (IdentityBindingError, InvalidExternalUserIDError) as exc:
                # Исключение внутри atomic откатывает учётку и TUR вместе с ним.
                raise _refuse(
                    REASON_BIND_REFUSED, correlation_id=correlation_id, tenant_slug=tenant_slug,
                    bind=type(exc).__name__,
                ) from exc
            request_row = SalonAdminLinkRequest.objects.create(
                idempotency_key=idempotency_key,
                tenant=tenant,
                external_user_id=external_user_id,
                user=user,
                actor=actor,
                correlation_id=correlation_id,
                result="created",
            )
            result = SalonAdminLink(
                tenant=tenant, user=user, relationship=relationship, request=request_row, created=True,
            )

    # Пункт 9 — после коммита, по настоящим строкам.
    if not _readback(tenant, external_user_id, result.user):
        raise _refuse(REASON_READBACK_FAILED, correlation_id=correlation_id, tenant_slug=tenant_slug)

    logger.info(
        "users.salon_admin_link.%s tenant=%s user_id=%s relationship_id=%s actor=%s correlation_id=%s",
        "created" if result.created else "replayed",
        tenant.slug, result.user.pk, result.relationship.pk, actor, correlation_id or "-",
    )
    return result


__all__ = [
    "LINK_ROLE",
    "SalonAdminLink",
    "SalonAdminLinkRefused",
    "USERNAME_PREFIX",
    "link_salon_admin",
]
