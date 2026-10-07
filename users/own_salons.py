"""«Свои салоны» клиента — один предикат на все поверхности (O-1b, DRF-2831).

Салон свой, когда у клиента с ним действующее отношение покупателя
(``TenantUserRelationship``, роль ``customer``). Читают его полка «Твои
места» и режим ``OWN_SALONS`` резолвера; правило у них одно, поэтому и
место одно — две копии разошлись бы.
"""
from __future__ import annotations

from uuid import UUID

from users.models import TenantUserRelationship


def own_salon_ids(user) -> tuple[UUID, ...]:
    """Салоны, с которыми у клиента действующее отношение покупателя."""
    return tuple(
        TenantUserRelationship.objects
        .filter(
            user=user,
            is_active=True,
            role=TenantUserRelationship.Role.CUSTOMER,
        )
        .order_by("tenant_id")
        .values_list("tenant_id", flat=True)
    )
