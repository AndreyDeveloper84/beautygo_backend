"""DRF-1663 — tests must not write media into the storage they check.

Guards the session-scoped ``_isolated_file_storage`` fixture in the root
conftest.py. Declared expectation: with that fixture removed, BOTH tests
below go red — under ``djangoProject.settings.test`` because the file
lands in the project MEDIA_ROOT, under ``djangoProject.settings.dev``
because ``default_storage`` is S3Boto3Storage.

Each test carries a negative half (not project media, not S3) and a
positive half (bytes really written and read back) — a fixture that
broke storage outright would otherwise pass the negation alone.
"""
from pathlib import Path

import pytest
from django.conf import settings as dj_settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, default_storage

PAYLOAD = b"DRF-1663 probe"


def _project_media_root() -> Path:
    # base.py: MEDIA_ROOT = BASE_DIR / 'media' — the directory ordinary
    # tests used to litter before the fixture.
    return Path(dj_settings.BASE_DIR) / "media"


def _assert_isolated(saved_path: Path, tmp_media: Path) -> None:
    saved_path = saved_path.resolve()
    assert saved_path.is_relative_to(tmp_media.resolve()), (
        f"file landed outside the session temp dir: {saved_path}"
    )
    assert not saved_path.is_relative_to(_project_media_root().resolve()), (
        f"file landed in the project MEDIA_ROOT: {saved_path}"
    )


def test_default_storage_is_filesystem_in_temp_dir(_isolated_file_storage):
    tmp_media = _isolated_file_storage

    # NEGATIVE: not S3, whatever DJANGO_SETTINGS_MODULE the env carries.
    assert (
        dj_settings.STORAGES["default"]["BACKEND"]
        == "django.core.files.storage.FileSystemStorage"
    )
    assert isinstance(default_storage, FileSystemStorage)
    assert Path(dj_settings.MEDIA_ROOT).resolve() == tmp_media.resolve()

    name = default_storage.save("drf1663/probe.txt", ContentFile(PAYLOAD))
    try:
        _assert_isolated(Path(default_storage.path(name)), tmp_media)
        # POSITIVE: the bytes are really there and round-trip.
        with default_storage.open(name, "rb") as fh:
            assert fh.read() == PAYLOAD
    finally:
        default_storage.delete(name)


@pytest.mark.django_db
def test_model_imagefield_writes_into_temp_dir(_isolated_file_storage):
    """The defect path: FoodScan.image.save() — a FileField bound to
    ``default_storage`` at import time must follow the session swap."""
    from nutrition.models import FoodScan
    from users.models import User

    tmp_media = _isolated_file_storage
    user = User.objects.create(username="drf1663", role="client", is_proxy=True)
    scan = FoodScan(user=user)
    scan.image.save("probe.jpg", ContentFile(PAYLOAD), save=False)
    try:
        assert isinstance(scan.image.storage, FileSystemStorage)
        _assert_isolated(Path(scan.image.path), tmp_media)
        # POSITIVE: written and readable back through the field.
        with scan.image.open("rb") as fh:
            assert fh.read() == PAYLOAD
    finally:
        scan.image.delete(save=False)
