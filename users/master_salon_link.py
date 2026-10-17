"""Назначение мастера в салон оператором — обе половины связи сразу (DRF-2913).

Связь «мастер ↔ салон» в каталоге хранится в двух местах:

* ``SpecialistProfile.tenant`` — каталог и запись: чей это мастер, какому
  салону достаётся запись;
* активная строка ``TenantUserRelationship`` с ролью staff/admin — доступ:
  права мастера на записи салона и на расписание.

Штатное заведение (``tenants.solo_provisioning``,
``tenants.salon_specialist_provisioning``) ставит ``User.tenant``, и вторую
половину заводит сигнал ``users.signals``. Админка писала только первую:
мастер, заведённый оператором в салон формой, продавался клиенту, а к
расписанию салона доступа не имел. Замер пилота 08.10 — четыре таких
мастера в одном салоне.

Здесь — одна операция для обоих входов админки (форма мастера и блок
мастеров на форме салона).

Чего модуль НЕ делает:

* не возвращает отозванного. Отзыв — действие администратора салона;
  вернуть человека побочным эффектом формы значило бы его отменить (тот
  же запрет, что в ``users.signals``, DRF-1248);
* не чинит уже заведённых мастеров без связи — это запись в данные и
  делается отдельно;
* не трогает ``User.tenant``.
"""

from __future__ import annotations

from django.utils import timezone

from .models import TenantUserRelationship

#: Причина, с которой гасится клиентская строка, когда человек становится
#: сотрудником того же салона. Активная строка на пару (человек, салон)
#: одна; погашенная рядом с активной — смена роли, а не отзыв (DRF-1248).
ROLE_CHANGE_REASON = "role_change_to_staff"


class MasterWasRevokedError(Exception):
    """Связь этого человека с салоном отозвана; формой её не возвращают."""


def was_revoked_from(user, tenant_id) -> bool:
    """Отозван: есть погашенная строка и нет ни одной активной (DRF-1248)."""
    if user is None or tenant_id is None:
        return False
    states = list(
        TenantUserRelationship.objects.filter(user=user, tenant_id=tenant_id).values_list("is_active", flat=True)
    )
    return bool(states) and not any(states)


def grant_staff_on_admin_assignment(user, tenant_id) -> bool:
    """Оператор назначил мастеру салон — у мастера есть доступ сотрудника.

    ``True`` — строка заведена сейчас; ``False`` — человек уже работает в
    этом салоне (staff, admin или ресепшен), менять нечего.

    Вызывать внутри транзакции сохранения формы.
    """
    Role = TenantUserRelationship.Role
    active = (
        TenantUserRelationship.objects.select_for_update()
        .filter(user=user, tenant_id=tenant_id, is_active=True)
        .first()
    )
    if active is not None:
        if active.role != Role.CUSTOMER:
            return False
        # Клиент салона становится его мастером: роль меняется, а не
        # добавляется — активная строка на пару одна.
        active.is_active = False
        active.revoked_at = timezone.now()
        active.revoke_reason = ROLE_CHANGE_REASON
        active.save(update_fields=["is_active", "revoked_at", "revoke_reason"])
    elif was_revoked_from(user, tenant_id):
        raise MasterWasRevokedError(f"user={user.pk} tenant={tenant_id}")
    TenantUserRelationship.objects.create(
        user=user, tenant_id=tenant_id, role=Role.STAFF,
        granted_by=TenantUserRelationship.GrantedBy.ADMIN,
    )
    return True
