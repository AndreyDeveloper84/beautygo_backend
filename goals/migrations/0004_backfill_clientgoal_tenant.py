"""Бэкфилл ``ClientGoal.tenant`` для строк, созданных до DRF-1455.

Отдельным файлом от схемы нарочно: схема и данные откатываются по
разным причинам и в разном темпе. Схему откатывают, когда колонка
оказалась не той; данные — когда бэкфилл проставил не то. Слитые в один
файл, они заставляют откатывать одно ради другого.

Что проставляется — тот же порядок, что в ``goals/tenant_scope.py``,
минус то, чего в миграции нет (заголовка запроса):

1. единственная активная связь клиента с салоном
   (``TenantUserRelationship``);
2. ``User.tenant`` — легаси-привязка одиночного салона.

Всё остальное остаётся NULL, и это не пропуск, а честное «салон
неизвестен». Подставить первый попавшийся салон значило бы
воспроизвести ровно тот дефект, который DRF-1455 чинит. Цели с NULL не
теряют распознавание: каноническая таксономия ``ServiceCategory``
заводится без салона и читается всегда — молчат только чужие прайсы
``SalonService``.

Обратная операция сбрасывает ``tenant`` в NULL у всех строк. Это точный
откат: до этой миграции колонка была NULL везде (её только что добавили
в 0003), поэтому обнуление возвращает данные в прежнее состояние без
потерь — сама колонка не несла ничего, что нужно было бы сохранить.
"""
from __future__ import annotations

from django.db import migrations


def backfill(apps, schema_editor):
    ClientGoal = apps.get_model("goals", "ClientGoal")
    TenantUserRelationship = apps.get_model("users", "TenantUserRelationship")

    goals = list(
        ClientGoal.objects.filter(tenant__isnull=True).values_list(
            "id", "client_id",
        )
    )
    if not goals:
        return

    client_ids = {client_id for _, client_id in goals}

    # Одна выборка на всех клиентов вместо запроса на цель: строк целей
    # на пилоте немного, но миграция обязана вести себя предсказуемо и
    # на выросшей таблице.
    by_client: dict[object, set[object]] = {}
    for user_id, tenant_id in TenantUserRelationship.objects.filter(
        user_id__in=client_ids, is_active=True,
    ).values_list("user_id", "tenant_id"):
        by_client.setdefault(user_id, set()).add(tenant_id)

    User = apps.get_model("users", "User")
    legacy_tenant = dict(
        User.objects.filter(id__in=client_ids, tenant__isnull=False).values_list(
            "id", "tenant_id",
        )
    )

    updates: dict[object, list[object]] = {}
    for goal_id, client_id in goals:
        linked = by_client.get(client_id) or set()
        if len(linked) == 1:
            tenant_id = next(iter(linked))
        elif linked:
            # Несколько активных салонов — выбрать за клиента нельзя.
            continue
        else:
            tenant_id = legacy_tenant.get(client_id)
        if tenant_id is None:
            continue
        updates.setdefault(tenant_id, []).append(goal_id)

    for tenant_id, goal_ids in updates.items():
        ClientGoal.objects.filter(id__in=goal_ids).update(tenant_id=tenant_id)


def unbackfill(apps, schema_editor):
    ClientGoal = apps.get_model("goals", "ClientGoal")
    ClientGoal.objects.filter(tenant__isnull=False).update(tenant=None)


class Migration(migrations.Migration):

    dependencies = [
        ("goals", "0003_clientgoal_tenant"),
        ("users", "0012_tenantuserrelationship"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
