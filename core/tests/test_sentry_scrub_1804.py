"""DRF-1804 (M12a) — событие Sentry для подсказки адреса уходит без тела и строки запроса.

Стерегутся обе стороны: на пути ``…/geocoding/suggest/`` тело и строка запроса
убраны, остальное в событии не тронуто; событие другого пути не меняется вовсе.
Связь с настройками — текстом ``settings/base.py``: ``sentry_sdk.init`` в тестах
не вызывается (DSN пуст), и это названный предел проверки.
"""
from __future__ import annotations

import copy
from pathlib import Path

from core.sentry_scrub import SCRUBBED_PATH_MARKERS, scrub_event

HOME = "Пушкина 45 кв 12"


def _event(url: str) -> dict:
    return {
        "event_id": "e1",
        "level": "error",
        "request": {
            "url": url,
            "method": "POST",
            "data": {"q": HOME},
            "query_string": f"q={HOME}",
            "headers": {"Content-Type": "application/json"},
        },
        "tags": {"route": "internal"},
    }


def test_suggest_event_leaves_without_body_and_query_string():
    event = _event("https://api.example/api/v1/internal/specialists/1/geocoding/suggest/")

    got = scrub_event(event, hint={})

    assert "data" not in got["request"] and "query_string" not in got["request"]
    assert HOME not in str(got)
    # Остальное событие на месте — отчёт об ошибке не потерял смысла.
    assert got["request"]["method"] == "POST"
    assert got["request"]["url"].endswith("/geocoding/suggest/")
    assert got["tags"] == {"route": "internal"}


def test_event_of_another_path_is_not_changed():
    event = _event("https://api.example/api/v1/internal/specialists/1/service-locations/")
    before = copy.deepcopy(event)

    got = scrub_event(event, hint={})

    assert got == before
    assert got["request"]["data"] == {"q": HOME}


def test_event_without_a_request_is_returned_as_is():
    event = {"event_id": "e2", "message": "boot"}
    assert scrub_event(copy.deepcopy(event)) == event
    assert scrub_event({"request": "not-a-dict"}) == {"request": "not-a-dict"}


def test_the_marker_is_the_route_the_view_serves():
    from django.urls import reverse

    subject = "11111111-1111-1111-1111-111111111111"
    path = reverse("internal-specialist-address-suggest", kwargs={"specialist_id": subject})
    assert any(marker in path for marker in SCRUBBED_PATH_MARKERS), path


def test_settings_wire_the_scrub_into_before_send():
    """Названный предел: init в тестах не вызывается, связь проверяется текстом настроек."""
    text = (Path(__file__).resolve().parents[2] / "djangoProject" / "settings" / "base.py").read_text(encoding="utf-8")
    assert "from core.sentry_scrub import scrub_event" in text
    assert "return scrub_event(event, hint)" in text
    assert "before_send=_sentry_before_send" in text
