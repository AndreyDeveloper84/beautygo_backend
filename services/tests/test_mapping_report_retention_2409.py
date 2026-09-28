"""Отчёт разбора услуг: том и срок хранения (DRF-2409, решение владельца 28.09, п.5).

«Храним отчёты 90 дней с последнего прогона по конкретному салону.» Вопрос
владельцу был о том, переживает ли отчёт выкладку, — поэтому том и срок идут
вместе: срок без тома описывал бы файлы, которые до 90 дней не доживают.

Узлы:

* граница по сроку — 89 дней жив, 91 истёк;
* граница держится **содержимым**, а не датой файла: старый ``mtime`` при
  свежем ``generated_at`` — жив, и наоборот;
* срок у каждого салона свой;
* нечитаемый отчёт не удаляется и называется — «неизвестно» не «старый»;
* «каталога нет» отличается от «отчётов нет»; чужие файлы не трогаются;
* без ``apply`` ничего не удаляется, но истёкшие посчитаны;
* задача по расписанию удаляет только с флагом и пишет событие с числами,
  без названий салонов;
* том смонтирован в оба сервиса, которым нужен отчёт, и стоит в расписании.
"""

from __future__ import annotations

import json
import os
import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.utils import timezone

from services.mapping import store

ROOT = Path(__file__).resolve().parents[2]


def _report(directory: Path, slug: str, *, days_ago: float | None, **over) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"tenant": slug, "rules": "test", "summary": {}, "rows": {}}
    if days_ago is not None:
        payload["generated_at"] = (timezone.now() - timedelta(days=days_ago)).isoformat()
    payload.update(over)
    path = directory / f"{slug}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def reports(settings, tmp_path):
    settings.MAPPING_REPORT_DIR = str(tmp_path / "mapping_reports")
    settings.MAPPING_REPORT_RETENTION_DAYS = 90
    return tmp_path / "mapping_reports"


def _states(ages):
    return {a.slug: a.state for a in ages}


def test_the_boundary_is_ninety_days(reports):
    _report(reports, "salon-89", days_ago=89)
    _report(reports, "salon-91", days_ago=91)

    exists, ages = store.review_reports()

    assert exists is True
    assert _states(ages) == {"salon-89": store.FRESH, "salon-91": store.EXPIRED}


def test_the_boundary_is_held_by_generated_at_not_by_the_file_date(reports):
    fresh_inside = _report(reports, "fresh-inside-old-file", days_ago=1)
    old_inside = _report(reports, "old-inside-new-file", days_ago=200)
    two_hundred_days = (timezone.now() - timedelta(days=200)).timestamp()
    os.utime(fresh_inside, (two_hundred_days, two_hundred_days))
    now = timezone.now().timestamp()
    os.utime(old_inside, (now, now))

    _, ages = store.review_reports()

    assert _states(ages) == {
        "fresh-inside-old-file": store.FRESH,
        "old-inside-new-file": store.EXPIRED,
    }


def test_each_salon_has_its_own_clock(reports):
    _report(reports, "salon-a", days_ago=120)
    _report(reports, "salon-b", days_ago=3)

    result = store.purge_expired_reports(apply=True)

    assert result["examined"] == 2
    assert result["deleted"] == 1
    assert not (reports / "salon-a.json").exists()
    assert (reports / "salon-b.json").exists()


OLD = "2020-01-01T00:00:00+00:00"


@pytest.mark.parametrize(
    "slug, content",
    [
        ("no-generated-at", '{"tenant": "no-generated-at", "rows": {}}'),
        ("naive-generated-at", '{"tenant": "naive-generated-at", "generated_at": "2020-01-01T00:00:00"}'),
        ("foreign-tenant", '{"tenant": "someone-else", "generated_at": "%s"}' % OLD),
        ("broken-json", "{не json"),
    ],
)
def test_an_unreadable_report_is_named_and_never_deleted(reports, slug, content):
    """Даже с датой 2020 года внутри — если отчёт не свой, он не удаляется."""
    reports.mkdir(parents=True)
    path = reports / f"{slug}.json"
    path.write_text(content, encoding="utf-8")

    result = store.purge_expired_reports(apply=True)

    assert result["examined"] == 1
    assert result["unreadable_slugs"] == [slug]
    assert result["deleted"] == 0
    assert path.exists()


def test_no_directory_is_not_the_same_as_no_reports(reports):
    missing = store.purge_expired_reports(apply=True)
    reports.mkdir(parents=True)
    empty = store.purge_expired_reports(apply=True)

    assert missing["dir_exists"] is False
    assert empty["dir_exists"] is True
    assert missing["examined"] == empty["examined"] == 0


def test_foreign_files_in_the_directory_are_left_alone(reports):
    _report(reports, "salon-old", days_ago=365)
    (reports / "notes.txt").write_text("не отчёт", encoding="utf-8")

    result = store.purge_expired_reports(apply=True)

    assert result["deleted"] == 1
    assert (reports / "notes.txt").exists()


def test_without_apply_nothing_is_deleted_but_expired_are_counted(reports):
    path = _report(reports, "salon-old", days_ago=365)

    result = store.purge_expired_reports(apply=False)

    assert result["expired"] == 1
    assert result["deleted"] == 0
    assert path.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("enabled, survives", [(False, True), (True, False)])
def test_the_task_deletes_only_with_the_flag_and_writes_counts(reports, settings, enabled, survives):
    from analytics import event_catalogue
    from analytics.models import AnalyticsEvent
    from services.tasks import purge_expired_mapping_reports_task

    settings.MAPPING_REPORT_PURGE_ENABLED = enabled
    path = _report(reports, "salon-old", days_ago=365)

    counts = purge_expired_mapping_reports_task()

    assert path.exists() is survives
    assert counts["expired"] == 1
    assert counts["deleted"] == (0 if survives else 1)
    event = AnalyticsEvent.objects.get(event_name=event_catalogue.MAPPING_REPORT_PURGE_RUN)
    assert event.payload["expired"] == 1
    assert "salon-old" not in json.dumps(event.payload)


def _services_mounting(volume_line: str) -> set[str]:
    """Сервисы docker-compose.yml, в чьих строках есть ``volume_line``."""
    found, current, in_services = set(), None, False
    for line in (ROOT / "docker-compose.yml").read_text(encoding="utf-8").splitlines():
        if line.startswith("services:"):
            in_services = True
            continue
        if in_services and re.match(r"^\S", line):
            in_services = False
        match = re.match(r"^  ([a-z][\w-]*):\s*$", line)
        if in_services and match:
            current = match.group(1)
        if in_services and current and volume_line in line.split("#")[0]:
            found.add(current)
    return found


def test_the_volume_is_mounted_where_the_report_is_written_and_purged():
    mount = "mapping_reports:/app/var/mapping_reports"
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert _services_mounting(mount) == {"web", "celery_worker"}
    assert re.search(r"^volumes:\s*$[\s\S]*^  mapping_reports:\s*$", compose, re.MULTILINE)


def test_the_purge_is_scheduled():
    entry = django_settings.CELERY_BEAT_SCHEDULE["purge-expired-mapping-reports"]
    assert entry["task"] == "services.purge_expired_mapping_reports"
