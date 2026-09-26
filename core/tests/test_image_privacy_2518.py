"""DRF-2518 — координаты съёмки не уезжают вместе со снимком.

Снимок еды сохранялся и уходил внешнему провайдеру байт в байт. У телефонов
геометка включена по умолчанию, еду фотографируют дома — наружу уезжал
домашний адрес человека.

Узлы держат три вещи, и каждая проверяется ПОДСТАНОВКОЙ, а не совпадением:

* **ложный вход обязателен:** снимок с заведомо подставленными координатами на
  входе → на выходе координат нет. Проверка на файле без метаданных прошла бы
  всегда и не доказала бы ничего;
* **обе стороны:** снимок БЕЗ метаданных не ломается и не теряет качества —
  иначе мы починили бы приватность и сломали распознавание;
* **данные изображения не изменились** — сравнением байтов энтропийной части,
  а не доверием к слову «срезаем, а не перекодируем».

Форматов три, потому что сериализатор принимает три (``ALLOWED_CONTENT_TYPES``
= jpeg, png, webp), и EXIF носят все три. Закрыть один и оставить два — это
та же беда, что «починили в двух клиентах из трёх».
"""

from __future__ import annotations

import io

import pytest
from PIL import Image
from PIL.ExifTags import IFD

from core.image_privacy import ORIENTATION_TAG, strip_metadata


def _entropy(data: bytes) -> bytes:
    """Данные изображения JPEG: от начала скана до конца файла."""
    i = data.find(b"\xff\xda")
    return data[i:] if i >= 0 else b""


def jpeg(*, with_gps: bool, orientation: int = 1) -> bytes:
    img = Image.new("RGB", (64, 48), (120, 160, 200))
    exif = img.getexif()
    if orientation != 1:
        exif[ORIENTATION_TAG] = orientation
    if with_gps:
        exif[0x010F] = "ProbePhone"           # Make
        exif[0x0110] = "Model X"              # Model
        exif[0x0132] = "2026:09:25 12:00:00"  # DateTime
        gps = exif.get_ifd(IFD.GPSInfo)
        gps[1] = "N"
        gps[2] = (55.0, 45.0, 21.36)
        gps[3] = "E"
        gps[4] = (37.0, 37.0, 2.0)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92, exif=exif)
    return buf.getvalue()


def png(*, with_text: bool) -> bytes:
    from PIL import PngImagePlugin

    img = Image.new("RGB", (32, 24), (200, 100, 50))
    info = PngImagePlugin.PngInfo()
    if with_text:
        info.add_text("Comment", "снято дома, 55.7500 37.6167")
        info.add_text("Software", "ProbePhone 1.0")
    buf = io.BytesIO()
    img.save(buf, format="PNG", pnginfo=info)
    return buf.getvalue()


def webp(*, with_exif: bool) -> bytes:
    img = Image.new("RGB", (32, 24), (50, 200, 100))
    buf = io.BytesIO()
    if with_exif:
        exif = img.getexif()
        exif[0x010F] = "ProbePhone"
        gps = exif.get_ifd(IFD.GPSInfo)
        gps[1] = "N"
        gps[2] = (55.0, 45.0, 21.36)
        img.save(buf, format="WEBP", exif=exif)
    else:
        img.save(buf, format="WEBP")
    return buf.getvalue()


class TestTheDirtyInputLosesItsCoordinates:
    """Ложный вход: координаты подставлены нарочно и обязаны исчезнуть."""

    def test_jpeg_with_gps_comes_out_without_it(self) -> None:
        dirty = jpeg(with_gps=True)
        # Положительная пара: вход правда «грязный». Без неё узел зеленел бы
        # на снимке, в котором координат и не было.
        before = Image.open(io.BytesIO(dirty)).getexif()
        assert dict(before.get_ifd(IFD.GPSInfo)), "проба без координат ничего не доказывает"

        clean = strip_metadata(dirty)

        after = Image.open(io.BytesIO(clean)).getexif()
        assert dict(after.get_ifd(IFD.GPSInfo)) == {}, "координаты пережили срезание"
        assert after.get(0x010F) is None, "модель аппарата пережила срезание"
        assert after.get(0x0132) is None, "время съёмки пережило срезание"

    def test_png_text_chunks_are_gone(self) -> None:
        dirty = png(with_text=True)
        # Положительная пара: подпись правда лежит в файле. PNG пишет
        # не-ASCII как iTXt в UTF-8, поэтому ищем закодированные байты.
        marker = "снято дома".encode("utf-8")
        assert marker in dirty, "проба без текстового куска ничего не доказывает"

        clean = strip_metadata(dirty)

        assert marker not in clean
        assert b"ProbePhone" not in clean

    def test_webp_exif_chunk_is_gone(self) -> None:
        dirty = webp(with_exif=True)
        assert b"EXIF" in dirty, "проба без EXIF ничего не доказывает"

        clean = strip_metadata(dirty)

        assert b"EXIF" not in clean
        assert b"ProbePhone" not in clean


class TestTheCleanInputSurvivesUntouched:
    """Вторая сторона: снимок без метаданных не ломается и не худеет."""

    def test_jpeg_without_metadata_keeps_its_image_data(self) -> None:
        clean_in = jpeg(with_gps=False)

        out = strip_metadata(clean_in)

        assert _entropy(out) == _entropy(clean_in), "данные изображения изменились"
        assert Image.open(io.BytesIO(out)).size == (64, 48)

    @pytest.mark.parametrize("make", [png, webp])
    def test_other_formats_stay_openable(self, make) -> None:
        kw = {"with_text": False} if make is png else {"with_exif": False}
        clean_in = make(**kw)

        out = strip_metadata(clean_in)

        assert Image.open(io.BytesIO(out)).size == Image.open(io.BytesIO(clean_in)).size


class TestTheImageItselfIsNotRecompressed:
    """Срезание, а не перекодирование: иначе чиним приватность и ломаем разбор."""

    def test_jpeg_entropy_bytes_are_identical(self) -> None:
        dirty = jpeg(with_gps=True)

        clean = strip_metadata(dirty)

        assert _entropy(clean) == _entropy(dirty), (
            "данные изображения изменились — значит снимок пережат, "
            "а лист это прямо запрещает"
        )

    def test_stripping_actually_made_the_file_smaller(self) -> None:
        # Положительная пара к утверждению выше: если бы срезание ничего не
        # делало, равенство энтропийных частей выполнялось бы тривиально.
        dirty = jpeg(with_gps=True)

        clean = strip_metadata(dirty)

        assert len(clean) < len(dirty), "ничего не срезано — проверка выше вакуумна"


class TestOrientationSurvives:
    """Перевёрнутый снимок не уезжает к провайдеру лежащим на боку."""

    def test_orientation_is_preserved(self) -> None:
        dirty = jpeg(with_gps=True, orientation=6)

        clean = strip_metadata(dirty)

        after = Image.open(io.BytesIO(clean)).getexif()
        assert after.get(ORIENTATION_TAG) == 6
        assert dict(after.get_ifd(IFD.GPSInfo)) == {}, "вместе с ориентацией уцелели координаты"
        assert _entropy(clean) == _entropy(dirty)

    def test_nothing_but_orientation_survives(self) -> None:
        """Белый список из одного поля, а не чёрный из многих.

        Если однажды сюда добавят «ещё одно безобидное поле», узел упадёт —
        и это правильно: каждое исключение должно быть решением, а не
        привычкой.
        """
        dirty = jpeg(with_gps=True, orientation=6)

        clean = strip_metadata(dirty)

        after = dict(Image.open(io.BytesIO(clean)).getexif())
        assert set(after) == {ORIENTATION_TAG}, f"уцелело лишнее: {after}"
