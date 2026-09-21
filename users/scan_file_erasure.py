"""Файлы фото сканера при «забудь всё» — пачкой и до транзакции (DRF-2256).

Путь бота C5.2 (``users.personal_data_api.InternalPersonalDataDeleteView``) снимал
файлы сканов по одному, тремя вызовами хранилища на файл (``exists`` →
``delete`` → ``exists``, :func:`users.deletion_executor._delete_file`), внутри
``atomic()`` и уже после блокировки строки профиля питания. Таймаут бота — 10 с,
в проде хранилище S3 (Minio): каждый вызов сетевой. Сколько сканов у людей на
стенде — не измерено и измерено не будет, поэтому бюджет держит форма, а не
замер: 500 сканов — один вызов хранилища.

Здесь — две вещи:

* :func:`scan_file_names` — имена файлов сканов всех личностей субъекта, читаются
  до транзакции стирания и до блокировок (открыта лишь транзакция журнала
  доступа ``privacy_audit.mixins`` — вне её ничего на этом маршруте не бывает);
* :func:`remove_scan_files` — снять их пачкой: у S3-хранилища — ``delete_objects``
  (до 1000 ключей за вызов; отсутствующий ключ — не ошибка), у остальных —
  прежний путь по одному (локальный диск в dev/тестах).

Порядок «файл раньше строки» сохранён: вызывающий снимает файлы, затем в
транзакции стирает строки. Откат транзакции оставляет строку без файла —
повтор найдёт её и дочистит; файл без строки не нашёл бы никто.

Стойкий сбой хранилища (``Errors`` в ответе пачки или файл остался) —
:class:`users.deletion_executor.IncompleteErasure`: вызывающий отвечает 500, в
базе ничего не стёрто, повтор бота (DRF-1950) повторит всё.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

logger = logging.getLogger(__name__)

#: Предел S3 ``DeleteObjects`` — 1000 ключей за вызов.
S3_BATCH_LIMIT = 1000


def _storage():
    """Хранилище поля фото. ``DefaultStorage`` — ленивый прокси: атрибуты
    (``bucket``, ``delete``, ``exists``) он отдаёт от настоящего хранилища."""
    from nutrition.models import FoodScan

    return FoodScan._meta.get_field("image").storage


def scan_file_names(users: Iterable) -> list[str]:
    """Имена файлов сканов всех личностей субъекта — без пустых, без повторов."""
    from nutrition.models import FoodScan

    names = FoodScan.objects.filter(user__in=list(users)).exclude(image="").values_list(
        "image", flat=True
    )
    return sorted({n for n in names if n})


def remove_scan_files(names: list[str]) -> int:
    """Снять файлы пачкой. Возвращает, сколько имён отдано хранилищу.

    Raises:
        IncompleteErasure: хранилище не сняло хотя бы один файл.
    """
    from users.deletion_executor import IncompleteErasure

    if not names:
        return 0
    storage = _storage()
    bucket = getattr(storage, "bucket", None)
    if bucket is not None and hasattr(bucket, "delete_objects"):
        normalize = getattr(storage, "_normalize_name", lambda n: n)
        for start in range(0, len(names), S3_BATCH_LIMIT):
            chunk = names[start:start + S3_BATCH_LIMIT]
            response = bucket.delete_objects(
                Delete={"Objects": [{"Key": normalize(n)} for n in chunk], "Quiet": True}
            )
            errors = (response or {}).get("Errors") or []
            if errors:
                # Число, не ключи: ключ несёт идентификатор человека.
                logger.warning("forget_all.scan_files.batch_failed errors=%d", len(errors))
                raise IncompleteErasure(f"scan files not removed: {len(errors)}")
        return len(names)

    removed = 0
    for name in names:
        storage.delete(name)
        if storage.exists(name):
            raise IncompleteErasure("scan file still present")
        removed += 1
    return removed


__all__ = ["S3_BATCH_LIMIT", "remove_scan_files", "scan_file_names"]
