"""Аватар и работа портфолио уходят к боту без EXIF, даже если в хранилище он есть.

Срезание при записи (``MetadataFreeImageField``) стоит с 28.09. Файлы,
легшие раньше, несут EXIF с координатами съёмки. Здесь такой файл кладётся в
хранилище МИМО поля, как лежал бы старый, и отдаётся через ручку бота.

Чистоту ответа проверяет собственный строгий разбор контейнера, а не Pillow:
в IFD0 возвращённого блока разрешён ровно один тег — ориентация. Указатели
на GPS (0x8825) и Exif-IFD (0x8769), модель устройства, XMP, текстовые куски
PNG и чанки WebP — всё это красный.

Положительная пара (``TestTheProbeIsDirty``) доказывает, что разбор видит
координаты во входе. Без неё «в ответе их нет» доказывало бы только то, что
разбор слеп.
"""

from __future__ import annotations

import io
import random
import uuid
import zlib

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from PIL import Image
from PIL.ExifTags import IFD
from rest_framework.test import APIClient

from core.image_privacy import ORIENTATION_TAG
from tenants.solo_provisioning import provision_solo_workspace
from users.models import SpecialistPortfolio, SpecialistProfile

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token-strip-on-read"  # noqa: S105

EXIF_PREFIX = b"Exif\x00\x00"
GPS_IFD_POINTER = 0x8825
MAKER = b"ProbePhone"
XMP_MARK = b"http://ns.adobe.com/xap/1.0/"


@pytest.fixture(autouse=True)
def _settings(settings, tmp_path):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture
def olga() -> SpecialistProfile:
    return provision_solo_workspace(
        tenant_id=uuid.uuid4(),
        slug="solo-max-exifread",
        name="Студия exifread",
        city="Пенза",
        external_user_id="bot:max:exifread01",
        display_name="Мастер",
    ).profile


# --- вход: снимок «как с телефона» -----------------------------------------


def _dirty(fmt: str, orientation: int = 6) -> bytes:
    """Шумный снимок (не сжимается ниже порога ручки) с GPS, моделью и ориентацией."""
    side = 120
    img = Image.frombytes("RGB", (side, side), random.Random(fmt).randbytes(side * side * 3))
    exif = Image.Exif()
    exif[ORIENTATION_TAG] = orientation
    exif[0x010F] = MAKER.decode()
    gps = exif.get_ifd(IFD.GPSInfo)
    gps[1] = "N"
    gps[2] = (55.0, 45.0, 21.36)
    buf = io.BytesIO()
    extra = {"xmp": b"<x:xmpmeta xmlns:x='adobe:ns:meta/'/>"} if fmt == "JPEG" else {}
    img.save(buf, format=fmt, exif=exif.tobytes(), **extra)
    return buf.getvalue()


def _double_prefixed_jpeg(orientation: int = 6) -> bytes:
    """JPEG, каким его писал старый ``_orientation_segment``: ``Exif\\0\\0Exif\\0\\0MM…``."""
    side = 120
    img = Image.frombytes("RGB", (side, side), random.Random("old").randbytes(side * side * 3))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    data = buf.getvalue()
    exif = Image.Exif()
    exif[ORIENTATION_TAG] = orientation
    raw = exif.tobytes()
    assert raw.startswith(EXIF_PREFIX)
    payload = EXIF_PREFIX + raw
    segment = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    return data[:2] + segment + data[2:]


# --- строгий разбор ---------------------------------------------------------


def _ifd0_tags(tiff: bytes) -> dict[int, int | None]:
    """Теги IFD0 блока TIFF → значение SHORT×1 (или None). Любое отклонение от формы — AssertionError."""
    order = tiff[:2]
    assert order in (b"MM", b"II"), f"блок TIFF начинается не с порядка байт: {tiff[:8]!r}"
    endian = "big" if order == b"MM" else "little"
    assert int.from_bytes(tiff[2:4], endian) == 42, "нет числа 42 TIFF-заголовка"
    ifd = int.from_bytes(tiff[4:8], endian)
    count = int.from_bytes(tiff[ifd:ifd + 2], endian)
    tags: dict[int, int | None] = {}
    for n in range(count):
        entry = tiff[ifd + 2 + 12 * n: ifd + 14 + 12 * n]
        tag = int.from_bytes(entry[0:2], endian)
        is_short = int.from_bytes(entry[2:4], endian) == 3 and int.from_bytes(entry[4:8], endian) == 1
        tags[tag] = int.from_bytes(entry[8:10], endian) if is_short else None
    return tags


def _jpeg_app_segments(data: bytes) -> list[tuple[int, bytes]]:
    """(маркер, тело) всех APPn и COM до начала скана."""
    assert data[:2] == b"\xff\xd8", "не JPEG"
    out, i = [], 2
    while i < len(data) - 1:
        assert data[i] == 0xFF, f"на {i} не маркер"
        marker = data[i + 1]
        if marker in (0xD9, 0xDA):
            break
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if 0xE1 <= marker <= 0xEF or marker == 0xFE:
            out.append((marker, data[i + 4:i + 2 + length]))
        i += 2 + length
    return out


def _png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "не PNG"
    out, i = [], 8
    while i + 8 <= len(data):
        length = int.from_bytes(data[i:i + 4], "big")
        kind, body = data[i + 4:i + 8], data[i + 8:i + 8 + length]
        crc = int.from_bytes(data[i + 8 + length:i + 12 + length], "big")
        assert zlib.crc32(kind + body) == crc, f"битый CRC у {kind!r}"
        out.append((kind, body))
        i += 12 + length
        if kind == b"IEND":
            break
    return out


def _webp_chunks(data: bytes) -> list[bytes]:
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP", "не WebP"
    assert int.from_bytes(data[4:8], "little") == len(data) - 8, "размер RIFF не сходится"
    out, i = [], 12
    while i + 8 <= len(data):
        kind = data[i:i + 4]
        length = int.from_bytes(data[i + 4:i + 8], "little")
        out.append(kind)
        i += 8 + length + (length % 2)
    return out


def _exif_blocks(data: bytes) -> list[bytes]:
    """Все блоки TIFF, которые читатель примет за EXIF, — без префикса."""
    if data[:2] == b"\xff\xd8":
        bodies = [b for m, b in _jpeg_app_segments(data) if m == 0xE1 and b.startswith(EXIF_PREFIX)]
        return [b[len(EXIF_PREFIX):] for b in bodies]
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return [b for k, b in _png_chunks(data) if k == b"eXIf"]
    if data[:4] == b"RIFF":
        return []  # чанки WebP проверяются по видам, см. _assert_clean
    raise AssertionError(f"незнакомый контейнер: {data[:12]!r}")


def _assert_clean(data: bytes) -> None:
    """Ничего, кроме одного тега ориентации, — по байтам, а не по Pillow."""
    assert MAKER not in data, "модель устройства осталась в байтах"
    assert XMP_MARK not in data, "XMP остался в байтах"
    if data[:2] == b"\xff\xd8":
        kinds = [m for m, _ in _jpeg_app_segments(data)]
        assert kinds in ([], [0xE1]), f"лишние сегменты APPn/COM: {[hex(k) for k in kinds]}"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        kinds = {k for k, _ in _png_chunks(data)}
        assert not kinds & {b"tEXt", b"zTXt", b"iTXt", b"tIME"}, f"текстовые куски PNG остались: {kinds}"
    else:
        kinds = set(_webp_chunks(data))
        assert not kinds & {b"EXIF", b"XMP "}, f"чанки метаданных WebP остались: {kinds}"
    blocks = _exif_blocks(data)
    assert len(blocks) <= 1, "несколько EXIF-блоков"
    for tiff in blocks:
        assert not tiff.startswith(EXIF_PREFIX), "префикс Exif\\0\\0 повторён — браузер блок не узнает"
        tags = _ifd0_tags(tiff)
        assert set(tags) == {ORIENTATION_TAG}, f"в IFD0 не только ориентация: {sorted(hex(t) for t in tags)}"


def _orientation(data: bytes) -> int | None:
    blocks = _exif_blocks(data)
    return _ifd0_tags(blocks[0])[ORIENTATION_TAG] if blocks else None


# --- хранилище «как до 28.09» и ручка ---------------------------------------

EXT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}


def _store_raw(prefix: str, ext: str, data: bytes) -> str:
    """Положить байты в хранилище МИМО ``MetadataFreeImageField`` — как лёг бы старый файл."""
    name = default_storage.save(f"{prefix}/old-{uuid.uuid4().hex}.{ext}", ContentFile(data))
    with default_storage.open(name, "rb") as fh:
        assert fh.read() == data, "хранилище переписало байты — проба не о старом файле"
    return name


def _get(path: str) -> bytes:
    client = APIClient()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RUNTIME_TOKEN}"
    resp = client.get(path)
    assert resp.status_code == 200
    return b"".join(resp.streaming_content) if resp.streaming else resp.content


def _served_avatar(profile: SpecialistProfile, data: bytes, ext: str) -> bytes:
    name = _store_raw("avatars", ext, data)
    SpecialistProfile.objects.filter(pk=profile.pk).update(avatar=name)
    return _get(f"/api/v1/internal/specialists/{profile.pk}/media/avatar/file/")


def _served_portfolio(profile: SpecialistProfile, data: bytes, ext: str) -> bytes:
    name = _store_raw("portfolio", ext, data)
    item = SpecialistPortfolio.objects.create(specialist=profile, image="placeholder.png")
    SpecialistPortfolio.objects.filter(pk=item.pk).update(image=name)
    return _get(f"/api/v1/internal/specialists/{profile.pk}/portfolio/{item.pk}/file/")


DOORS = {"avatar": _served_avatar, "portfolio": _served_portfolio}


class TestTheProbeIsDirty:
    """Строгий разбор правда видит во входе то, чего не должно быть в ответе."""

    @pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
    def test_dirty_input_fails_the_check(self, fmt: str) -> None:
        with pytest.raises(AssertionError):
            _assert_clean(_dirty(fmt))

    @pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
    def test_gps_pointer_is_in_the_input(self, fmt: str) -> None:
        (tiff,) = _exif_blocks(_dirty(fmt))
        assert GPS_IFD_POINTER in _ifd0_tags(tiff)


@pytest.mark.parametrize("door", sorted(DOORS))
class TestAnOldFileLeavesClean:
    @pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
    def test_no_exif_gps_or_maker_in_the_response(self, olga, door: str, fmt: str) -> None:
        body = DOORS[door](olga, _dirty(fmt), EXT[fmt])
        _assert_clean(body)

    @pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
    def test_orientation_survives(self, olga, door: str, fmt: str) -> None:
        body = DOORS[door](olga, _dirty(fmt, orientation=6), EXT[fmt])
        assert _orientation(body) == 6

    def test_old_double_prefixed_segment_comes_out_readable(self, olga, door: str) -> None:
        """Файл, записанный старым срезанием (до #727), уходит с одним префиксом — браузер его повернёт."""
        body = DOORS[door](olga, _double_prefixed_jpeg(6), "jpg")
        _assert_clean(body)
        assert _orientation(body) == 6

    @pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
    def test_the_picture_itself_is_the_same(self, olga, door: str, fmt: str) -> None:
        """Срезаются блоки, а не пиксели: декодированное изображение то же."""
        dirty = _dirty(fmt)
        body = DOORS[door](olga, dirty, EXT[fmt])
        with Image.open(io.BytesIO(dirty)) as a, Image.open(io.BytesIO(body)) as b:
            assert a.size == b.size
            assert a.convert("RGB").tobytes() == b.convert("RGB").tobytes()
