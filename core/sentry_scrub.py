"""Чистка событий Sentry перед отправкой (DRF-1804, M12a).

Подсказка адреса места мастера (``…/geocoding/suggest/``) принимает в теле
строку ``q`` — адрес, который вводит мастер, часто домашний. Ручка исключений
наружу не выпускает, но исключение до или вокруг неё (промежуточный слой,
разбор запроса) дало бы 500. Интеграция Django тогда приложила бы к событию
тело запроса: ``max_request_body_size`` по умолчанию «medium», а
``send_default_pii=False`` тело не покрывает.

Узко, по решению главного окна: для этого пути из события убираются тело
(``request.data``) и строка запроса (``request.query_string``). Остальные
события не меняются. Глобальный ``max_request_body_size`` не трогается: это
решение шире листа.

Функция чистая и импортируемая: ``settings`` подключает её в
``_sentry_before_send``. Сам ``sentry_sdk.init`` в тестах не вызывается (DSN
пуст), поэтому связь с настройками стережёт отдельная текстовая проверка.
"""
from __future__ import annotations

from typing import Any

#: Пути, у событий которых тело и строка запроса не уходят в Sentry.
SCRUBBED_PATH_MARKERS: tuple[str, ...] = ("/geocoding/suggest/",)

#: Что убирается из ``event["request"]`` на этих путях.
SCRUBBED_REQUEST_KEYS: tuple[str, ...] = ("data", "query_string")


def scrub_event(event: Any, hint: Any = None) -> Any:
    """Вернуть событие без тела и строки запроса для путей ввода адреса; остальное — как есть."""
    if not isinstance(event, dict):
        return event
    request = event.get("request")
    if not isinstance(request, dict):
        return event
    url = str(request.get("url") or "")
    if any(marker in url for marker in SCRUBBED_PATH_MARKERS):
        for key in SCRUBBED_REQUEST_KEYS:
            request.pop(key, None)
    return event
