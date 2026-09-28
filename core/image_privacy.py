"""Срезание метаданных снимка перед тем, как байты уйдут из нашего контура.

DRF-2518. Снимок еды сохранялся и отправлялся внешнему провайдеру
распознавания **байт в байт, как пришёл** (``nutrition/views.py``, обе точки
загрузки). У телефонов геометка включена по умолчанию, а еду фотографируют
дома — то есть наружу уходил домашний адрес человека с точностью до десятков
метров, и он об этом не знал.

Рядом стоял аккуратный разбор 152-ФЗ ст. 18 п. 5 — строка в российской базе
обязана появиться ДО пересечения границы. Но он про **факт** передачи, а не
про **содержимое**: локализация соблюдалась, координаты уезжали.

Почему срезание, а не перекодирование
--------------------------------------

Перекодирование гарантированно убивает всё, но **меняет сами данные
изображения**: пережатие ухудшает распознавание и меняет то, что человек снял.
Замер на пробном снимке: путь ``Image.open`` → ``save`` дал другой хеш
энтропийной части, срезание сегментов — тот же.

Поэтому здесь срезаются **контейнерные блоки**, а данные изображения не
трогаются вовсе. Для JPEG это сегменты APPn и COM, для PNG — текстовые и
``eXIf``-куски, для WebP — чанки ``EXIF``/``XMP ``.

Белый список из одного поля, а не чёрный из многих
---------------------------------------------------

Ориентация живёт в тех же метаданных. Срезав всё, перевёрнутый снимок поехал
бы к провайдеру лежащим на боку — то есть приватность починилась бы за счёт
распознавания, ровно того, что лист беречь и велит.

Но «оставить всё, кроме GPS» — запрещено и правильно запрещено: отдельная
выборка полей создаёт иллюзию, что остальное безопасно, и следующий формат
метаданных пройдёт мимо. Здесь сделано **обратное**: не пропускается ничего,
**кроме одного числа** — тега ориентации. В поле, где помещается только
целое от 1 до 8, координаты подставить физически некуда.

Чего этот модуль НЕ делает
---------------------------

* **Не чинит уже загруженные снимки.** Он стоит на входе; всё, что попало в
  хранилище раньше, остаётся с метаданными. Их разбор — отдельный предмет.
* **Не сохраняет ICC-профиль.** Профиль — это метаданные, и его описание у
  телефонов бывает устройство-зависимым. Цена названа: изображение с широким
  охватом цвета будет прочитано как sRGB, цвета сместятся. Это сознательный
  размен в пользу правила «не оставляем того, о чём не подумали».
* **Не молчит на незнакомом контейнере.** Формат, который здесь не разобран,
  проходит через перекодирование Pillow — приватность не должна отказывать
  «открытым» способом. Цена перекодирования в этой ветке принимается
  сознательно: лучше потерять качество, чем тихо выпустить координаты.
"""

from __future__ import annotations

import io

#: Тег ориентации в EXIF. Единственное, что переживает срезание.
ORIENTATION_TAG = 0x0112

#: Маркеры JPEG, которые срезаются: APP1..APP15 (EXIF, XMP, IPTC, ICC…) и COM.
#: APP0 (JFIF) остаётся: это заголовок контейнера, а не сведения о человеке.
_JPEG_DROP = frozenset(range(0xE1, 0xF0)) | {0xFE}
#: Куски PNG, которые срезаются: текст в любом виде, EXIF и время правки.
_PNG_DROP = frozenset({b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"})
#: Чанки WebP с метаданными.
_WEBP_DROP = frozenset({b"EXIF", b"XMP "})


def _orientation_of(data: bytes) -> int:
    """Ориентация снимка, или 1, если её нет и читать нечего."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            value = img.getexif().get(ORIENTATION_TAG)
    except Exception:  # noqa: BLE001 — битый EXIF не повод ронять загрузку
        return 1
    return value if isinstance(value, int) and 1 <= value <= 8 else 1


def _orientation_segment(orientation: int) -> bytes:
    """APP1 c ОДНИМ тегом ориентации и больше ничем."""
    from PIL import Image

    exif = Image.Exif()
    exif[ORIENTATION_TAG] = orientation
    payload = b"Exif\x00\x00" + exif.tobytes()
    return b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload


def _strip_jpeg(data: bytes) -> bytes:
    out = bytearray(data[:2])  # SOI
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            # Не маркер — дальше разбирать нечего, отдаём остаток как есть.
            out.extend(data[i:])
            break
        marker = data[i + 1]
        if marker in (0xD9, 0xDA):  # EOI или начало данных изображения
            out.extend(data[i:])
            break
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if marker not in _JPEG_DROP:
            out.extend(data[i:i + 2 + length])
        i += 2 + length
    return bytes(out)


def _strip_png(data: bytes) -> bytes:
    out = bytearray(data[:8])  # сигнатура
    i = 8
    while i + 8 <= len(data):
        length = int.from_bytes(data[i:i + 4], "big")
        kind = data[i + 4:i + 8]
        end = i + 12 + length
        if kind not in _PNG_DROP:
            out.extend(data[i:end])
        i = end
        if kind == b"IEND":
            break
    return bytes(out)


def _strip_webp(data: bytes) -> bytes:
    out = bytearray(data[:12])  # RIFF + размер + WEBP
    i = 12
    kept: list[bytes] = []
    while i + 8 <= len(data):
        kind = data[i:i + 4]
        length = int.from_bytes(data[i + 4:i + 8], "little")
        end = i + 8 + length + (length % 2)  # чанки выровнены по чётности
        if kind not in _WEBP_DROP:
            kept.append(data[i:end])
        i = end
    body = b"".join(kept)
    # Размер в заголовке RIFF обязан сойтись, иначе файл не откроется.
    return bytes(out[:4]) + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + body


def strip_metadata(data: bytes) -> bytes:
    """Снимок без метаданных, с сохранённой ориентацией.

    Данные изображения не перекодируются: у знакомых контейнеров срезаются
    только блоки метаданных. Незнакомый контейнер проходит перекодированием —
    приватность не отказывает «открытым» способом.
    """
    if not data:
        return data

    if data[:2] == b"\xff\xd8":  # JPEG
        orientation = _orientation_of(data)
        stripped = _strip_jpeg(data)
        if orientation == 1:
            return stripped
        return stripped[:2] + _orientation_segment(orientation) + stripped[2:]

    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _strip_png(data)

    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _strip_webp(data)

    # Контейнер не разобран — единственный безопасный путь — перекодирование.
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            buf = io.BytesIO()
            img.save(buf, format=img.format or "JPEG")
            return buf.getvalue()
    except Exception:  # noqa: BLE001
        # Pillow не открыл — это не изображение, и сериализатор его не
        # пропустил бы. Отдаём как есть: срезать здесь нечего, а ронять
        # загрузку на неизвестном входе — не предмет этого листа.
        return data


# ---------------------------------------------------------------------------
# DRF-2522: одно место, через которое проходят ВСЕ хранимые снимки.
#
# Перепись по конструктору (реестр моделей, ``isinstance(f, ImageField)``)
# дала пять носителей хранимых байт; срезание стояло на одном — во вьюхе
# снимка еды. Вьюха — неверное место: аватары, портфолио и услуги пишутся
# через сериализаторы, админку и внутренние API, и каждый новый путь записи
# пришлось бы помнить отдельно.
#
# Все эти пути сходятся в ``FieldFile.save``: и присваивание файла с
# последующим ``model.save()`` (через ``FileField.pre_save``), и прямой
# ``instance.image.save(name, content)``. Там и стоит очистка. Сторож
# ``core/tests/test_image_fields_gate_2522.py`` краснеет, если в реестре
# появится ``ImageField`` другого класса.
# ---------------------------------------------------------------------------

from django.core.files.base import ContentFile  # noqa: E402
from django.db import models  # noqa: E402
from django.db.models.fields.files import ImageFieldFile  # noqa: E402


class MetadataFreeImageFieldFile(ImageFieldFile):
    """Файл поля, который срезает метаданные ДО записи в хранилище."""

    def save(self, name, content, save=True):
        if hasattr(content, "seek"):
            content.seek(0)
        cleaned = strip_metadata(content.read())
        super().save(name, ContentFile(cleaned, name=name), save=save)


class MetadataFreeImageField(models.ImageField):
    """``ImageField``, у которого метаданные не доезжают до хранилища."""

    attr_class = MetadataFreeImageFieldFile

    def deconstruct(self):
        # Схема БД та же, что у ImageField: разница только в рантайме.
        # Путь базового класса держит миграции пустыми — иначе на каждое
        # поле ляжет AlterField без единой строки SQL.
        name, _path, args, kwargs = super().deconstruct()
        return name, "django.db.models.ImageField", args, kwargs
