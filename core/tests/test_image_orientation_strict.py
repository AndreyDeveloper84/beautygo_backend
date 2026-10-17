"""Ориентация после срезания читается СТРОГИМ разборщиком, а не Pillow.

NO-TICKET: image_privacy APP1 double-prefix breaks EXIF orientation in Chromium.

``_orientation_segment`` писал ``b"Exif\\0\\0" + Image.Exif().tobytes()``, а
``tobytes()`` в Pillow 11 сам начинается с ``Exif\\0\\0``. Сегмент выходил
``Exif\\0\\0Exif\\0\\0MM…``. Pillow такое прощает, поэтому узлы DRF-2518,
читавшие результат тем же Pillow, были зелёными. Chromium (движок Mini App) —
нет: снимок с Orientation=6 после срезания показывался без поворота, то есть
каждый вертикальный снимок с телефона лёг на бок.

Поэтому здесь ориентацию читает собственный разбор по спецификации EXIF/TIFF
и PNG — так, как это делает браузер: ровно один ``Exif\\0\\0`` в начале APP1,
сразу за ним порядок байт ``MM``/``II`` и число 42, IFD0 с тегом 0x0112.
Pillow в чтении не участвует — иначе узел опять не смог бы покраснеть.

Проверка в самом Chromium (headless, до и после) — в описании PR: в CI
браузера нет.
"""

from __future__ import annotations

import io
import zlib

import pytest
from PIL import Image
from PIL.ExifTags import IFD

from core.image_privacy import ORIENTATION_TAG, strip_metadata

EXIF_PREFIX = b"Exif\x00\x00"


def _tiff_orientation(tiff: bytes) -> int | None:
    """Значение 0x0112 из IFD0 блока TIFF, или None. Любое отклонение от формы — AssertionError."""
    order = tiff[:2]
    assert order in (b"MM", b"II"), f"блок TIFF начинается не с порядка байт: {tiff[:8]!r}"
    endian = "big" if order == b"MM" else "little"
    assert int.from_bytes(tiff[2:4], endian) == 42, "нет числа 42 TIFF-заголовка"
    ifd = int.from_bytes(tiff[4:8], endian)
    count = int.from_bytes(tiff[ifd:ifd + 2], endian)
    for n in range(count):
        entry = tiff[ifd + 2 + 12 * n: ifd + 14 + 12 * n]
        tag = int.from_bytes(entry[0:2], endian)
        if tag == ORIENTATION_TAG:
            assert int.from_bytes(entry[2:4], endian) == 3, "ориентация не SHORT"
            assert int.from_bytes(entry[4:8], endian) == 1, "ориентация не одно значение"
            return int.from_bytes(entry[8:10], endian)
    return None


def _jpeg_app1_payloads(data: bytes) -> list[bytes]:
    """Тела всех APP1 до начала скана."""
    assert data[:2] == b"\xff\xd8", "не JPEG"
    out, i = [], 2
    while i < len(data) - 1:
        assert data[i] == 0xFF, f"на {i} не маркер"
        marker = data[i + 1]
        if marker in (0xD9, 0xDA):
            break
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if marker == 0xE1:
            out.append(data[i + 4:i + 2 + length])
        i += 2 + length
    return out


def _jpeg_orientation(data: bytes) -> int | None:
    exif = [p for p in _jpeg_app1_payloads(data) if p.startswith(EXIF_PREFIX)]
    if not exif:
        return None
    assert len(exif) == 1, "несколько EXIF-сегментов"
    tiff = exif[0][len(EXIF_PREFIX):]
    assert not tiff.startswith(EXIF_PREFIX), "префикс Exif\\0\\0 повторён — браузер сегмент не узнает"
    return _tiff_orientation(tiff)


def _png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Куски PNG с проверкой CRC каждого."""
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


def _png_orientation(data: bytes) -> int | None:
    chunks = _png_chunks(data)
    kinds = [k for k, _ in chunks]
    exif = [b for k, b in chunks if k == b"eXIf"]
    if not exif:
        return None
    assert len(exif) == 1, "несколько кусков eXIf"
    assert kinds.index(b"eXIf") < kinds.index(b"IDAT"), "eXIf после IDAT — браузер его не читает"
    assert not exif[0].startswith(EXIF_PREFIX), "в PNG eXIf префикса Exif\\0\\0 быть не должно"
    return _tiff_orientation(exif[0])


def _dirty(fmt: str, orientation: int) -> bytes:
    img = Image.new("RGB", (60, 20), (200, 30, 30))
    exif = Image.Exif()
    exif[ORIENTATION_TAG] = orientation
    exif[0x010F] = "ProbePhone"
    gps = exif.get_ifd(IFD.GPSInfo)
    gps[1] = "N"
    gps[2] = (55.0, 45.0, 21.36)
    buf = io.BytesIO()
    img.save(buf, format=fmt, exif=exif.tobytes())
    return buf.getvalue()


class TestTheProbeIsReadable:
    """Положительная пара: строгий разбор правда читает ориентацию на входе.

    Без неё «после срезания ориентации нет» доказывало бы только то, что
    разборщик слеп.
    """

    def test_jpeg_input(self) -> None:
        assert _jpeg_orientation(_dirty("JPEG", 6)) == 6

    def test_png_input(self) -> None:
        assert _png_orientation(_dirty("PNG", 6)) == 6


class TestOrientationSurvivesForAStrictReader:
    @pytest.mark.parametrize("orientation", [3, 6, 8])
    def test_jpeg(self, orientation: int) -> None:
        assert _jpeg_orientation(strip_metadata(_dirty("JPEG", orientation))) == orientation

    @pytest.mark.parametrize("orientation", [3, 6, 8])
    def test_png(self, orientation: int) -> None:
        assert _png_orientation(strip_metadata(_dirty("PNG", orientation))) == orientation

    @pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
    def test_upright_gets_no_orientation_block(self, fmt: str) -> None:
        """Ориентация 1 — умолчание: блок не нужен, и не нужно лишнего."""
        out = strip_metadata(_dirty(fmt, 1))
        read = _jpeg_orientation if fmt == "JPEG" else _png_orientation
        assert read(out) is None


class TestNothingElseCameBack:
    """Возвращённый блок несёт только ориентацию — координатам в нём негде жить."""

    @pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
    def test_no_gps_or_maker(self, fmt: str) -> None:
        out = strip_metadata(_dirty(fmt, 6))
        assert b"ProbePhone" not in out
        with Image.open(io.BytesIO(out)) as im:
            assert dict(im.getexif()) == {ORIENTATION_TAG: 6}


class TestTheVisibleImageIsTheSame:
    """Pillow по-прежнему поворачивает, и пиксели те же, что до срезания."""

    @pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
    def test_transposed_pixels_match(self, fmt: str) -> None:
        from PIL import ImageOps

        dirty = _dirty(fmt, 6)
        out = strip_metadata(dirty)
        with Image.open(io.BytesIO(dirty)) as a, Image.open(io.BytesIO(out)) as b:
            ta, tb = ImageOps.exif_transpose(a), ImageOps.exif_transpose(b)
            assert ta.size == tb.size == (20, 60)
            assert ta.convert("RGB").tobytes() == tb.convert("RGB").tobytes()
