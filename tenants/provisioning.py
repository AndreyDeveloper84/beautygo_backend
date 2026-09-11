"""Заведение салона по slug для экрана «подключить салон» (DRF-1525).

До этого изменения оператор заводил салон в админке Ayla, читал глазами
его ``id`` из read-only поля формы и вставлял UUID в форму бота. Владелец
11.09.2026: «человек UUID не вводит и не видит». Значит UUID должен
приходить из единственного места, где он рождается, — отсюда.

### Почему идемпотентно по slug, а не «создать»

Slug — проводной идентификатор салона (``Tenant.slug`` ``unique=True``,
заголовок ``X-Tenant``). Экран бота не знает, заводили ли салон в Ayla
раньше; ему нужен ответ на вопрос «какой UUID у салона ``<slug>``», и
только если такого нет — завести. Один вызов на оба случая: повтор с
теми же данными безвреден, второй экран не создаст второй салон.

### Почему одноимённость проверяется, а город — нет

Тот же slug с ДРУГИМ названием — это либо опечатка оператора, либо два
разных салона, претендующих на один идентификатор. Оба случая нельзя
решать молча: отказ называет существующее имя, оператор решает сам.

Город — нет: у существующей строки он может быть пустым (DRF-1587 завёл
поле позже самих салонов), и совпадение здесь ничего не доказывает.
Поверх существующей строки ничего не пишется: этот вызов заводит, а не
правит — правка живёт в админке Ayla.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction

from tenants.models import Tenant


@dataclass(frozen=True)
class TenantNameMismatch(Exception):
    """Салон с таким slug есть, но зовётся иначе."""

    slug: str
    existing_name: str
    requested_name: str

    def __str__(self) -> str:  # pragma: no cover - текст для логов
        return (
            f"tenant {self.slug!r} exists as {self.existing_name!r}, "
            f"requested {self.requested_name!r}"
        )


def ensure_tenant(*, slug: str, name: str, city: str = "") -> tuple[Tenant, bool]:
    """Вернуть салон по slug, заведя его, если такого нет.

    Возвращает ``(tenant, created)``. Существующая строка возвращается
    как есть — в том числе неактивная: решать, что делать с выключенным
    салоном, будет вызывающая сторона, а не этот вызов молча.

    Гонка двух одновременных вызовов с одним slug разрешается уникальным
    индексом: проигравший ``IntegrityError`` перечитывает строку
    победителя и проходит ту же проверку имени.
    """
    name = name.strip()
    city = (city or "").strip()

    existing = Tenant.all_objects.filter(slug=slug).first()
    if existing is not None:
        return _same_or_refuse(existing, name), False

    try:
        with transaction.atomic():
            return Tenant.all_objects.create(slug=slug, name=name, city=city), True
    except IntegrityError:
        existing = Tenant.all_objects.get(slug=slug)
        return _same_or_refuse(existing, name), False


def _same_or_refuse(existing: Tenant, name: str) -> Tenant:
    if existing.name.strip() != name:
        raise TenantNameMismatch(
            slug=existing.slug, existing_name=existing.name, requested_name=name
        )
    return existing
