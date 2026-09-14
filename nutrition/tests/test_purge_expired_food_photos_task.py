"""DRF-1843 — §134/§135 по расписанию: задача beat и событие прогона.

Команда ``purge_expired_food_photos`` была, запускать её было некому
(расписания нет, события нет). Здесь стережётся:

* задача стоит в ``CELERY_BEAT_SCHEDULE`` и зовёт тот же ``purge_one``;
* **выключенный флаг ничего не удаляет**, но прогон всё равно записан
  событием с числом «не удалено в срок» — §134 требует выявлять их;
* включённый флаг удаляет объект хранилища и строку просроченного скана и
  не трогает молодой;
* отказ хранилища не снимает строку и считается «не удалено».
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.files.base import ContentFile
from django.utils import timezone

from analytics import event_catalogue
from analytics.models import AnalyticsEvent
from nutrition.models import FoodScan
from nutrition.tasks import purge_expired_food_photos_task

pytestmark = pytest.mark.django_db

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture(autouse=True)
def isolated_media(settings, tmp_path):
    """Своё хранилище на тест — по образцу ``test_purge_expired_food_photos``."""
    import copy

    storages_cfg = copy.deepcopy(settings.STORAGES)
    storages_cfg["default"] = {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": str(tmp_path)},
    }
    settings.STORAGES = storages_cfg
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create(username="photo-owner-1843")


def _scan(user, *, age_days: int) -> FoodScan:
    scan = FoodScan.objects.create(
        user=user,
        dish_name="Борщ",
        confidence=0.9,
        provider_used=FoodScan.Provider.OPENAI,
        raw_response={"vision": "тарелка борща"},
    )
    scan.image.save(f"{scan.id}.jpg", ContentFile(PNG), save=True)
    FoodScan.objects.filter(pk=scan.pk).update(
        created_at=timezone.now() - timedelta(days=age_days)
    )
    scan.refresh_from_db()
    return scan


def _run_event() -> AnalyticsEvent:
    events = list(
        AnalyticsEvent.objects.filter(event_name=event_catalogue.FOOD_PHOTO_PURGE_RUN)
    )
    assert len(events) == 1, events
    return events[0]


class TestDisabledByDefault:
    def test_the_flag_is_off_unless_the_environment_says_so(self) -> None:
        from django.conf import settings

        assert settings.FOOD_PHOTO_PURGE_ENABLED is False

    def test_disabled_deletes_nothing_and_still_records_the_debt(self, user, settings) -> None:
        settings.FOOD_PHOTO_PURGE_ENABLED = False
        old = _scan(user, age_days=31)
        storage, name = old.image.storage, old.image.name

        result = purge_expired_food_photos_task()

        # POSITIVE first: the run happened and saw the expired photo.
        assert result["expired_before"] == 1
        assert result["expired_after"] == 1
        event = _run_event()
        assert event.payload["enabled"] is False
        assert event.payload["expired_after"] == 1
        # Nothing irreversible without the owner's switch.
        assert FoodScan.objects.filter(pk=old.pk).exists()
        assert storage.exists(name)
        assert result["deleted"] == 0


class TestEnabled:
    def test_expired_goes_young_stays_and_the_run_is_recorded(self, user, settings) -> None:
        settings.FOOD_PHOTO_PURGE_ENABLED = True
        old = _scan(user, age_days=31)
        young = _scan(user, age_days=5)
        storage, name = old.image.storage, old.image.name

        result = purge_expired_food_photos_task()

        assert result["deleted"] == 1
        assert result["expired_after"] == 0
        assert FoodScan.objects.filter(pk=young.pk).exists()
        assert not FoodScan.objects.filter(pk=old.pk).exists()
        assert not storage.exists(name)
        event = _run_event()
        assert event.payload["deleted"] == 1
        assert event.payload["expired_after"] == 0

    def test_storage_refusal_keeps_the_row_and_counts_as_not_deleted(
        self, user, settings, monkeypatch
    ) -> None:
        from django.core.files.storage import FileSystemStorage

        settings.FOOD_PHOTO_PURGE_ENABLED = True
        old = _scan(user, age_days=31)

        def refuse(self, name):
            raise OSError("bucket says no")

        monkeypatch.setattr(FileSystemStorage, "delete", refuse)

        result = purge_expired_food_photos_task()

        assert result["refused"] == 1
        assert result["expired_after"] == 1
        assert FoodScan.objects.filter(pk=old.pk).exists()
        assert _run_event().payload["refused"] == 1


class TestWiring:
    def test_beat_runs_it_daily(self, settings) -> None:
        entry = settings.CELERY_BEAT_SCHEDULE["purge-expired-food-photos"]
        assert entry["task"] == "nutrition.purge_expired_food_photos"

    def test_the_event_name_is_whitelisted(self) -> None:
        assert event_catalogue.FOOD_PHOTO_PURGE_RUN in event_catalogue.EVENT_NAMES

    def test_the_command_and_the_task_share_one_purge(self) -> None:
        from nutrition.management.commands import purge_expired_food_photos as cmd

        # Command._purge_one delegates to the module function the task imports.
        assert cmd.Command._purge_one.__code__.co_names.count("purge_one") == 1
