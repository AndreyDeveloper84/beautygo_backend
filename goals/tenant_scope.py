"""Салон, в контексте которого принимается решение о цели (DRF-1455).

Задача
------

Распознавание названной услуги читало каталог целиком, по всем салонам
(``ServiceCategory.objects.filter(is_active=True)`` /
``SalonService.objects.filter(is_active=True)``). Клиент салона A мог
получить «цель распознана» от строки, которая есть только у салона B.
Наружу уходит булево, не данные, — но решение принималось по чужим
строкам, и человека вело к подбору по признаку, которого в его салоне
нет.

Чинится это не фильтром в одном запросе, а салоном, протянутым до слоя
цели: ``ClientGoal.tenant``. Цель — durable-факт («клиент выбрал цель
тогда-то»), и салон, в котором она названа, — часть этого факта.
Поэтому решение о **уже сохранённой** цели принимается по её
собственному салону, а не по тому, где клиент оказался сегодня.

Откуда берётся салон при создании цели
--------------------------------------

Порядок — от самого явного утверждения к самому косвенному выводу:

1. ``X-Tenant`` на запросе. Это проводной способ сказать «я действую в
   салоне X» (``TenantContextMiddleware``, DRF-242.4). Дерево
   ``/api/v1/internal/`` исключено из middleware — бот не носит
   заголовок на каждый вызов, — поэтому читаем заголовок здесь сами.
   Как только бот начнёт его слать на goal-вызовах, ничего менять не
   придётся.
2. Единственная активная связь клиента с салоном
   (``TenantUserRelationship``). Несколько активных — это штатный
   мультипровайдерный клиент (#246), и выбрать за него мы не можем:
   возвращаем None.
3. ``User.tenant`` — легаси-привязка одиночного салона.
4. Единственный активный ``Tenant`` во всём развёртывании. Это не
   догадка: если салон в системе ровно один, клиент может быть только
   в нём. Ровно этот случай и есть пилот, поэтому пилотное поведение
   правка не меняет — а на втором салоне вывод честно перестаёт
   работать, и вместо чужого каталога человек получает уточнение и
   кнопку «Найти услугу».

Ничего не найдено — None: салон неизвестен, и решение принимается
только по общей (бессалонной) части каталога. См. ``service_match``.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from users.models import User


def tenant_id_from_request(request: Any) -> uuid.UUID | None:
    """Салон, названный в запросе. Сначала ``request.tenant``, потом заголовок.

    ``request.tenant`` заполняет middleware там, где путь ей не
    исключён; на ``/api/v1/internal/`` он всегда None, и тогда читаем
    ``X-Tenant`` напрямую. Неизвестный или неактивный слаг — None
    (fail-closed: чужой салон по опечатке не подставится).
    """
    if request is None:
        return None

    tenant = getattr(request, "tenant", None)
    if tenant is not None:
        return tenant.id

    meta = getattr(request, "META", None) or {}
    slug = (meta.get("HTTP_X_TENANT") or "").strip()
    if not slug:
        return None

    from tenants.models import Tenant

    return (
        Tenant.objects.filter(slug=slug)
        .values_list("id", flat=True)
        .first()
    )


def _sole_active_relationship_tenant_id(client: User) -> uuid.UUID | None:
    """Салон клиента, если активная связь ровно одна."""
    from users.models import TenantUserRelationship

    ids = list(
        TenantUserRelationship.objects.filter(user=client, is_active=True)
        .values_list("tenant_id", flat=True)
        .distinct()[:2]
    )
    return ids[0] if len(ids) == 1 else None


def resolve_client_tenant_id(client: User, *, request: Any = None) -> uuid.UUID | None:
    """Салон, в котором клиент сейчас действует. None — неизвестен."""
    from_request = tenant_id_from_request(request)
    if from_request is not None:
        return from_request

    from_relationship = _sole_active_relationship_tenant_id(client)
    if from_relationship is not None:
        return from_relationship

    return getattr(client, "tenant_id", None)


def goal_tenant_id(goal, *, client: User | None = None) -> uuid.UUID | None:
    """Салон, по каталогу которого судить об уже сохранённой цели.

    Прежде всего — салон самой цели. Он NULL только у строк, созданных
    до DRF-1455 и не покрытых бэкфиллом (клиент без единственного
    салона); для них падаем в общее разрешение, чтобы старая цель не
    перестала распознаваться на ровном месте.
    """
    tenant_id = getattr(goal, "tenant_id", None)
    if tenant_id is not None:
        return tenant_id
    if client is None:
        return None
    return resolve_client_tenant_id(client)
