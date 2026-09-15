"""Все учётные строки одного субъекта: сам аккаунт и связанные с ним прокси (DRF-1038).

Привязка (``users.services.bind_external_identity``) переводит разрешение
бот-личности на реальный аккаунт, но строки, записанные на прокси ДО привязки,
остаются на прокси. Экспорт и стирание C5 и исполнитель удаления D3 обязаны
видеть их тоже — и видеть одинаково. Поэтому обход живёт здесь, в одном месте,
а не тремя копиями цикла.
"""
from __future__ import annotations

from users.models import User


def subject_users(user: User) -> list[User]:
    """``user`` первым, затем связанные прокси в порядке ``username``.

    Порядок полный: ``username`` уникален. Связь читается в момент вызова —
    исполнитель D3 зовёт функцию ДО отвязки прокси.
    """
    proxies = User.objects.filter(is_proxy=True, linked_user=user).order_by("username")
    return [user, *proxies]
