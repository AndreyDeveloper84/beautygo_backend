"""DRF-2529 — после конца изображения не уезжает ничего.

Живые фото Android дописывают MP4 после маркера конца JPEG (``EOI``), а в
атомах MP4 бывают координаты (``©xyz``). Замер на dev ``e8a5927``:

* JPEG — baseline, progressive, с RST — с MP4-хвостом: хвост проходил
  насквозь, байт в байт (``_strip_jpeg`` копировал от начала скана до конца
  файла);
* JPEG и второй JPEG за ``EOI`` (как кадр MPF): второй проходил целиком;
* WebP с хвостом за концом RIFF: хвост проходил, а выход НЕ ОТКРЫВАЛСЯ —
  размер RIFF пересчитывался вместе с хвостом;
* WebP с неизвестным чанком после кадра: чанк с координатами проходил;
* PNG с хвостом после ``IEND``: срезался — и это теперь закреплено узлом.

Узлы держат то же, что и 2518: ложный вход обязателен (координаты подставлены
нарочно и сначала найдены во входе), данные изображения не меняются (пиксели и
байты скана), и вторая сторона — снимок без хвоста выходит как раньше.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageCms, ImageSequence, PngImagePlugin
from PIL.ExifTags import IFD

from core.image_privacy import ORIENTATION_TAG, strip_metadata

#: Координаты в атоме ``©xyz`` — как их пишет камера телефона.
XYZ = b"+55.7558+037.6173/"
#: Хвост живого фото: MP4 с ``ftyp`` и ``moov/©xyz``.
MP4_TAIL = (
    b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    + (8 + 8 + len(XYZ)).to_bytes(4, "big") + b"moov"
    + (8 + len(XYZ)).to_bytes(4, "big") + b"\xa9xyz" + XYZ
)


def _jpeg(*, mode: str = "RGB", with_gps: bool = False, orientation: int = 1, **save) -> bytes:
    img = Image.effect_noise((96, 64), 60).convert(mode)
    exif = Image.Exif()
    if orientation != 1:
        exif[ORIENTATION_TAG] = orientation
    if with_gps:
        exif[0x010F] = "ProbePhone"
        gps = exif.get_ifd(IFD.GPSInfo)
        gps[1] = "N"
        gps[2] = (55.0, 45.0, 21.36)
    if len(exif):
        # Пустой EXIF Pillow всё равно пишет сегментом APP1 — «чистый» снимок
        # должен быть без него.
        save["exif"] = exif
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, **save)
    return buf.getvalue()


def _from_first_scan(data: bytes) -> bytes:
    """Байты от первого SOS до конца — обходом сегментов по длине.

    Не ``find(b"\\xff\\xda")``: внутри APP1 бывает JPEG-миниатюра со своим SOS.
    """
    i = 2
    while i + 4 <= len(data):
        assert data[i] == 0xFF, "проба не из сегментов"
        if data[i + 1] == 0xDA:
            return data[i:]
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    raise AssertionError("скан не найден")


def _pixels(data: bytes) -> list[bytes]:
    with Image.open(io.BytesIO(data)) as img:
        return [frame.convert("RGBA").tobytes() for frame in ImageSequence.Iterator(img)]


def _durations(data: bytes) -> list[int]:
    with Image.open(io.BytesIO(data)) as img:
        return [frame.info.get("duration", 0) for frame in ImageSequence.Iterator(img)]


def _app(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


JPEG_VARIANTS = {
    "baseline": {},
    "progressive": {"progressive": True},
    "restart": {"restart_marker_blocks": 1},
    "progressive-restart": {"progressive": True, "restart_marker_rows": 1},
    "subsampling-444": {"subsampling": 0},
    "grayscale": {"mode": "L"},
    "cmyk": {"mode": "CMYK"},
}


class TestJpegTail:
    @pytest.mark.parametrize("variant", JPEG_VARIANTS)
    def test_the_tail_after_eoi_is_cut_and_the_image_is_untouched(self, variant) -> None:
        clean_in = _jpeg(with_gps=True, **JPEG_VARIANTS[variant])
        dirty = clean_in + MP4_TAIL
        assert XYZ in dirty, "проба без координат ничего не доказывает"

        out = strip_metadata(dirty)

        assert XYZ not in out
        assert out.endswith(b"\xff\xd9")
        assert out == strip_metadata(clean_in), "хвост изменил что-то кроме себя"
        assert _from_first_scan(out) == _from_first_scan(clean_in), "данные изображения изменились"
        assert _pixels(out) == _pixels(clean_in)

    def test_a_second_jpeg_after_eoi_is_cut(self) -> None:
        first = _jpeg()
        second = _jpeg(with_gps=True)
        assert b"ProbePhone" in second

        out = strip_metadata(first + second)

        assert out == strip_metadata(first)
        assert b"ProbePhone" not in out

    def test_twin_a_clean_jpeg_with_a_tail_becomes_exactly_the_clean_jpeg(self) -> None:
        # Без EXIF у Pillow остаётся только JFIF — срезать нечего, и снимок
        # без хвоста обязан выйти байт в байт тем же, что вошёл.
        clean_in = _jpeg()
        assert strip_metadata(clean_in) == clean_in

        assert strip_metadata(clean_in + MP4_TAIL) == clean_in

    def test_orientation_survives_the_tail_and_a_second_pass(self) -> None:
        dirty = _jpeg(with_gps=True, orientation=6) + MP4_TAIL

        once = strip_metadata(dirty)
        twice = strip_metadata(once)

        assert XYZ not in once
        assert dict(Image.open(io.BytesIO(once)).getexif()) == {ORIENTATION_TAG: 6}
        # Снимок еды проходит срезание дважды: во вьюхе и в ``FieldFile.save``.
        assert twice == once

    def test_an_app1_between_scans_is_cut(self) -> None:
        progressive = _jpeg(progressive=True)
        scan = len(progressive) - len(_from_first_scan(progressive))
        # После первого SOS любой «FF C4» — настоящий DHT между сканами: в
        # данных скана FF всегда идёт со стаффингом.
        between = progressive.index(b"\xff\xc4", scan)
        exif = _app(0xE1, b"Exif\x00\x00ProbePhone" + XYZ)
        dirty = progressive[:between] + exif + progressive[between:]
        assert _pixels(dirty) == _pixels(progressive), "libjpeg не прочёл пробу"

        out = strip_metadata(dirty)

        assert XYZ not in out and b"ProbePhone" not in out
        assert out == strip_metadata(progressive)

    def test_a_jpeg_thumbnail_inside_app1_does_not_stop_the_parse(self) -> None:
        # Миниатюра EXIF — целый JPEG со своими SOS и EOI внутри APP1. Её EOI
        # не конец снимка: APP1 пропускается по длине.
        clean_in = _jpeg()
        thumbnail = _app(0xE1, b"Exif\x00\x00" + _jpeg())
        dirty = clean_in[:2] + thumbnail + clean_in[2:] + MP4_TAIL

        out = strip_metadata(dirty)

        assert out == clean_in

    def test_fill_bytes_and_garbage_between_segments_do_not_open_the_tail(self) -> None:
        clean_in = _jpeg()
        # T.81 B.1.1.2: перед маркером допустимы заполнители FF. Раньше первый
        # же байт не на месте отдавал остаток файла как есть — с хвостом.
        filled = clean_in[:2] + b"\xff\xff\xff" + clean_in[2:] + MP4_TAIL
        garbage = clean_in[:2] + b"\x00\x12\x34" + clean_in[2:] + MP4_TAIL

        for dirty in (filled, garbage):
            out = strip_metadata(dirty)

            assert XYZ not in out
            assert out == clean_in

    def test_a_dropped_segment_longer_than_the_file_is_never_copied(self) -> None:
        # APP1 объявляет 0xFFFF байт, а файл короче. Срезаемое не копируется
        # никогда — иначе битая длина отдавала бы EXIF целиком.
        exif = b"\xff\xe1\xff\xff" + b"Exif\x00\x00ProbePhone" + XYZ
        dirty = _jpeg()[:2] + exif + b"\x00" * 64

        out = strip_metadata(dirty)

        assert b"ProbePhone" not in out and XYZ not in out

    def test_no_cut_point_in_the_header_releases_the_exif(self) -> None:
        dirty = _jpeg(with_gps=True)
        header = len(dirty) - len(_from_first_scan(dirty))
        assert b"ProbePhone" in dirty[:header]

        leaked = [cut for cut in range(2, header + 1) if b"ProbePhone" in strip_metadata(dirty[:cut])]

        assert leaked == []

    def test_a_flood_of_empty_segments_stops_at_the_ceiling_without_copying_the_rest(self) -> None:
        # Скан, затем миллион пустых SOS и координаты в конце. Разбор
        # останавливается на потолке сегментов и остаток не копирует.
        scan = _jpeg()[:-2]
        dirty = scan + b"\xff\xda\x00\x02" * 250_000 + MP4_TAIL

        out = strip_metadata(dirty)

        assert XYZ not in out
        assert len(out) < len(scan) + 4096 * 4

    def test_mpo_keeps_only_the_first_frame(self) -> None:
        frames = [Image.effect_noise((64, 48), 40).convert("RGB") for _ in range(2)]
        buf = io.BytesIO()
        frames[0].save(buf, format="MPO", save_all=True, append_images=frames[1:])
        mpo = buf.getvalue()
        with Image.open(io.BytesIO(mpo)) as img:
            assert img.n_frames == 2, "проба без второго кадра ничего не доказывает"
        # Второй кадр — целый JPEG за EOI первого: его SOI с маркером («FF D8 FF»)
        # в данных скана невозможен — там FF всегда со стаффингом.
        assert mpo.find(b"\xff\xd8\xff", 2) != -1

        out = strip_metadata(mpo)

        assert out.find(b"\xff\xd8\xff", 2) == -1, "второй кадр пережил срезание"
        assert out.endswith(b"\xff\xd9")
        assert _pixels(out) == _pixels(mpo)[:1]


def _webp(*, frames: int = 1, mode: str = "RGB", **save) -> bytes:
    images = [Image.effect_noise((32, 24), 30 + k).convert(mode) for k in range(frames)]
    buf = io.BytesIO()
    if frames > 1:
        images[0].save(
            buf, format="WEBP", save_all=True, append_images=images[1:],
            duration=[100, 200, 300][:frames], loop=0, **save,
        )
    else:
        images[0].save(buf, format="WEBP", **save)
    return buf.getvalue()


def _riff(body: bytes) -> bytes:
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + body


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return kind + len(payload).to_bytes(4, "little") + payload + b"\x00" * (len(payload) % 2)


WEBP_VARIANTS = {
    "lossy": {},
    "lossless": {"lossless": True},
    "lossy-alpha": {"mode": "RGBA"},
    "animated-lossy": {"frames": 3, "mode": "RGBA"},
    "animated-lossless": {"frames": 3, "lossless": True},
}


class TestWebpTail:
    @pytest.mark.parametrize("variant", WEBP_VARIANTS)
    def test_the_tail_after_riff_is_cut_and_the_file_opens(self, variant) -> None:
        clean_in = _webp(**WEBP_VARIANTS[variant])
        dirty = clean_in + MP4_TAIL
        assert XYZ in dirty

        out = strip_metadata(dirty)

        assert XYZ not in out
        assert out == strip_metadata(clean_in)
        assert _pixels(out) == _pixels(clean_in), "выход не открылся или кадры другие"
        assert _durations(out) == _durations(clean_in)

    def test_an_unknown_chunk_after_the_frame_is_cut(self) -> None:
        clean_in = _webp()
        dirty = _riff(clean_in[12:] + _chunk(b"GPS ", XYZ))
        assert _pixels(dirty) == _pixels(clean_in), "libwebp не прочёл пробу"

        out = strip_metadata(dirty)

        assert XYZ not in out
        assert _pixels(out) == _pixels(clean_in)

    def test_an_unknown_chunk_inside_an_animation_frame_is_cut(self) -> None:
        clean_in = _webp(frames=3, mode="RGBA")
        anmf = clean_in.index(b"ANMF")
        length = int.from_bytes(clean_in[anmf + 4:anmf + 8], "little")
        frame_end = anmf + 8 + length + (length % 2)
        payload = clean_in[anmf + 8:anmf + 8 + length] + b"\x00" * (length % 2) + _chunk(b"GPS ", XYZ)
        body = clean_in[12:anmf] + _chunk(b"ANMF", payload) + clean_in[frame_end:]
        dirty = _riff(body)
        assert _pixels(dirty) == _pixels(clean_in), "libwebp не прочёл пробу"

        out = strip_metadata(dirty)

        assert XYZ not in out
        assert _pixels(out) == _pixels(clean_in)
        assert _durations(out) == _durations(clean_in)

    def test_the_icc_profile_is_cut(self) -> None:
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        dirty = _webp(icc_profile=icc)
        assert b"ICCP" in dirty

        out = strip_metadata(dirty)

        assert b"ICCP" not in out
        assert _pixels(out) == _pixels(dirty)


def _png(*, frames: int = 1, **save) -> bytes:
    images = [Image.effect_noise((32, 24), 30 + k).convert("RGB") for k in range(frames)]
    buf = io.BytesIO()
    if frames > 1:
        images[0].save(buf, format="PNG", save_all=True, append_images=images[1:], duration=100)
    else:
        images[0].save(buf, format="PNG", **save)
    return buf.getvalue()


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    import zlib

    crc = zlib.crc32(kind + payload).to_bytes(4, "big")
    return len(payload).to_bytes(4, "big") + kind + payload + crc


class TestPngTail:
    @pytest.mark.parametrize("frames", [1, 3], ids=["png", "apng"])
    def test_the_tail_after_iend_is_cut(self, frames) -> None:
        clean_in = _png(frames=frames)
        dirty = clean_in + MP4_TAIL

        out = strip_metadata(dirty)

        assert XYZ not in out
        assert out == clean_in
        assert _pixels(out) == _pixels(clean_in)

    def test_a_chunk_after_iend_is_cut(self) -> None:
        clean_in = _png()
        dirty = clean_in + _png_chunk(b"tEXt", b"Comment\x00" + XYZ)

        out = strip_metadata(dirty)

        assert XYZ not in out
        assert out == clean_in

    def test_a_png_without_iend_does_not_carry_the_tail(self) -> None:
        clean_in = _png()
        iend = clean_in.rindex(b"IEND") - 4
        dirty = clean_in[:iend] + MP4_TAIL

        out = strip_metadata(dirty)

        assert XYZ not in out
        assert out == clean_in[:iend]

    def test_private_c2pa_and_icc_chunks_before_iend_are_cut(self) -> None:
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        clean_in = _png(icc_profile=icc)
        assert b"iCCP" in clean_in
        iend = clean_in.rindex(b"IEND") - 4
        extra = _png_chunk(b"caBX", XYZ) + _png_chunk(b"prVt", XYZ)
        dirty = clean_in[:iend] + extra + clean_in[iend:]
        assert _pixels(dirty) == _pixels(clean_in), "Pillow не прочёл пробу"

        out = strip_metadata(dirty)

        assert XYZ not in out
        assert b"iCCP" not in out
        assert _pixels(out) == _pixels(clean_in)

    def test_png_text_is_still_cut_and_a_second_pass_changes_nothing(self) -> None:
        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", "снято дома")
        dirty = _png(pnginfo=info) + MP4_TAIL

        once = strip_metadata(dirty)

        assert "снято дома".encode() not in once and XYZ not in once
        assert strip_metadata(once) == once
