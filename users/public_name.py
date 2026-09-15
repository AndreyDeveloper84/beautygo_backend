"""Публичное имя человека — ``username`` наружу не уходит никогда (DRF-1914).

``username`` в каталоге — служебный идентификатор, а не имя: у прокси бота это
``bot:max:<id>``, у зарегистрированного по телефону — ``user_<цифры телефона>``
(``users/services.py`` ``register``), у соцвхода — ``social_<provider>_<uid>``,
у удалённого — ``deleted:<pk>``. Подставленный вместо пустого имени, он
публикует внешний идентификатор или номер клиента (правило владельца DRF-1039).

Любая клиентская, мастерская или публичная поверхность, которой нужно имя
человека, берёт его здесь: имя и фамилия, если они есть, иначе нейтральная
подпись. Где ``username`` законно читается (админка, s2s с внешним id прокси,
операторские команды), — перечень и причины в
``users/tests/test_username_never_outward_1914.py``.
"""
from __future__ import annotations

from typing import Any

CLIENT_LABEL = "Клиент"
MASTER_LABEL = "Мастер"


def public_person_name(user: Any, *, fallback: str) -> str:
    """Имя и фамилия человека или ``fallback`` — но не ``username``."""
    if user is None:
        return fallback
    full = " ".join(f"{user.first_name or ''} {user.last_name or ''}".split())
    return full or fallback
