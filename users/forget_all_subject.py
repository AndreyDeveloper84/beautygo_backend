"""«Забудь всё» по субъекту целиком — один глагол для всех путей (DRF-2305).

Путей стирания запомненного три:

* C5.2 — ``users.personal_data_api.InternalPersonalDataDeleteView`` (бот,
  повторы DRF-1950, отзыв согласия, бот-половина D3);
* кнопка в приложении — ``DELETE /users/me/personal-context/``;
* internal ``personal-context`` — ``DELETE /internal/users/{id}/personal-context/``.

С DRF-2214 C5.2 обходил всех личностей субъекта (``subject_users``: аккаунт и
связанные прокси ``bot:…``), а два других пути — одну: у прокси оставались
цели, план, профиль питания, дневник с фото и (после #545) outbox питания,
адресованный ``bot:…``. Здесь — тело C5.2 как есть; все три пути зовут его.

Порядок — как в C5.2 (DRF-2256): имена файлов фото всех личностей снимаются
пачкой ДО транзакции стирания и до любой блокировки строк; строки — одной
транзакцией: стёрто всё или ничего. Надгробие профиля (DRF-1366) — аккаунту и
тем прокси, у кого есть строка контекста; прокси без строки надгробия не
получает (DRF-1038), но его стирание пишется в журнал AMD-010, если было что
стирать.

Предел: ``subject_users`` идёт от аккаунта к прокси. Вызов с id самого прокси
связанный аккаунт не находит — так же, как C5.2 до этого листа (отдельный лист).
"""

from __future__ import annotations

from django.db import transaction

from users.forget_all_catalog import erase_remembered_catalog, remembered_scope
from users.models import UserPersonalContext
from users.personal_context_erasure import erase_personal_context
from users.personal_context_events import emit_personal_data_deleted
from users.scan_file_erasure import remove_scan_files, scan_file_names
from users.subject_identities import subject_users


def erase_remembered_for_subject(user, *, initiator: str) -> list[str]:
    """Стереть запомненное и профиль по всем личностям субъекта; вернуть scope.

    Зовётся ВНЕ транзакции: файлы фото снимаются до неё (``IncompleteErasure``
    при стойком сбое хранилища — отсюда, в базе тогда не стёрто ничего).
    """
    scope: list[str] = []
    identities = subject_users(user)
    names = scan_file_names(identities)
    remove_scan_files(names)
    removed = set(names)
    with transaction.atomic():
        for identity in identities:
            counts = erase_remembered_catalog(
                identity, initiator=initiator, removed_files=removed
            )
            also = remembered_scope(counts)
            if identity is not user and not UserPersonalContext.objects.filter(
                user=identity
            ).exists():
                if also:
                    emit_personal_data_deleted(identity, scope=also, initiator=initiator)
                identity_scope = also
            else:
                identity_scope = erase_personal_context(
                    identity, initiator=initiator, also_erased=also
                )
            for item in identity_scope:
                if item not in scope:
                    scope.append(item)
    return scope
