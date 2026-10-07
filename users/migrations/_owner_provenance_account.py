"""Именная учётка владельца для провенанса — общее для миграций и теста.

Модуль лежит в пакете миграций намеренно: логика принадлежит миграции
``0031``, имя на ``_`` загрузчик Django миграцией не считает. Ключ отсюда
читают и миграции других приложений, которым нужен автор решения.

Зачем учётка нужна
------------------
Решения владельца о каноне (юридический класс, область классификации)
пишутся с провенансом «кто + когда + основание», и схема без «кто» значение
не принимает. Админки для этих полей нет, у стенда нет ни shell, ни ORM —
пишет миграция. Автором решения при этом остаётся человек, и в провенансе
должно стоять его имя, а не техническая учётка ``admin`` и не тестовый
аккаунт салона (решение владельца 07.10).

Что учётка такое
----------------
Запись об авторстве, а не способ войти. Пароль непригоден, телефона и
внешней личности нет, прав персонала и суперпользователя нет, салона нет.
Исполнитель (кто накатил решение) в неё не пишется — он называется в
основании (``source_ref``) у каждой записи.

Как на неё ссылаться
--------------------
По ключу — ``username``, функцией ``account_id``. Хардкод pk запрещён:
если на какой-то базе учётка с этим именем уже заведена руками, её pk
другой, и это не ошибка. На чистой базе pk ставится фиксированный, чтобы
dev и пилот совпадали.

Чего шаг не делает
------------------
* Существующую учётку с этим ключом не правит — ни имя, ни права.
* Обратного действия нет: на учётку ссылаются провенансы под ``PROTECT``.
"""
from __future__ import annotations

#: Стабильный ключ. Точка, а не двоеточие: ``bot:…`` — пространство имён
#: внешних личностей.
USERNAME = "owner.andrey.tikhonov"

#: pk на чистой базе: uuid5(NAMESPACE_URL, "ayla:provenance:" + USERNAME).
FIXED_ID = "996717f8-0800-5cfa-8360-e133c785af99"

FIRST_NAME = "Андрей"
LAST_NAME = "Тихонов"

#: Копия ``User.ROLE_CHOICES`` на момент миграции: у модели нет роли «не
#: клиент и не мастер» кроме этой, а клиентом или мастером владелец не
#: является.
ROLE = "admin"


class NotAProvenanceAccount(RuntimeError):
    """Под ключом лежит учётка, которой авторство приписывать нельзя."""


def ensure(user_model, *, unusable_password: str) -> tuple[object, bool]:
    """Завести учётку, если её нет; вернуть ``(pk, создана ли)``."""
    existing = user_model.objects.filter(username=USERNAME).first()
    if existing is not None:
        _refuse_impostor(existing)
        return existing.pk, False
    account = user_model.objects.create(
        id=FIXED_ID,
        username=USERNAME,
        first_name=FIRST_NAME,
        last_name=LAST_NAME,
        role=ROLE,
        password=unusable_password,
        is_active=True,
        is_staff=False,
        is_superuser=False,
    )
    return account.pk, True


def account_id(user_model):
    """pk учётки по ключу; ``None`` — её нет или ей нельзя приписать авторство.

    Читатель обязан понимать ``None`` как отказ, а не подставлять другого
    автора.
    """
    account = user_model.objects.filter(username=USERNAME, is_active=True).first()
    if account is None or _is_impostor(account):
        return None
    return account.pk


def _is_impostor(account) -> bool:
    # Внешняя личность, гость и тестовая персона — не владелец, как бы
    # ни звалась их учётка.
    return bool(account.is_proxy or account.is_guest or account.is_test_persona)


def _refuse_impostor(account) -> None:
    if _is_impostor(account):
        raise NotAProvenanceAccount(
            "ключ учётки провенанса занят внешней, гостевой или тестовой личностью"
        )
