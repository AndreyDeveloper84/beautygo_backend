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
трогаются вовсе. Для JPEG это сегменты APPn и COM. У PNG и WebP — наоборот,
белый список (DRF-2529): остаются только куски, без которых изображение не
прочитать (данные, палитра, прозрачность, анимация, цветовое пространство),
всё прочее срезается — и текст, и EXIF, и куски, о которых здесь не думали.

Хвост после конца изображения
-----------------------------

DRF-2529. Живые фото Android дописывают **MP4 после конца JPEG**, а в атомах
MP4 бывают координаты (``©xyz``). Хвост — не поле метаданных, поэтому срезание
по сегментам его не видело: JPEG копировался от начала скана до конца файла
вместе со всем, что дописано после.

Теперь после конца контейнера не копируется ничего: у JPEG — после ``EOI``, у
PNG — после ``IEND``, у WebP — за размером ``RIFF`` из заголовка. Видеочасть
живого фото при этом теряется; человек этого не видит.

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
* **Не сохраняет ICC-профиль** — ни в JPEG (APP2), ни в PNG (``iCCP``), ни в
  WebP (``ICCP``); у двух последних — с DRF-2529, до неё профиль у них
  проходил. Профиль — это метаданные, и его описание у телефонов бывает
  устройство-зависимым. Цена названа: изображение с широким охватом цвета
  будет прочитано как sRGB, цвета сместятся. Это сознательный размен в пользу
  правила «не оставляем того, о чём не подумали».
* **Не молчит на незнакомом контейнере.** Формат, который здесь не разобран,
  проходит через перекодирование Pillow — приватность не должна отказывать
  «открытым» способом. Цена перекодирования в этой ветке принимается
  сознательно: лучше потерять качество, чем тихо выпустить координаты.
"""

from __future__ import annotations

import io
import re
import zlib

#: Тег ориентации в EXIF. Единственное, что переживает срезание.
ORIENTATION_TAG = 0x0112

#: Маркеры JPEG, которые срезаются: APP1..APP15 (EXIF, XMP, IPTC, ICC…) и COM.
#: APP0 (JFIF) остаётся: это заголовок контейнера, а не сведения о человеке.
_JPEG_DROP = frozenset(range(0xE1, 0xF0)) | {0xFE}
#: DRF-2529 — следующий маркер вне скана ищется, как ``next_marker()`` в
#: libjpeg: мусор и заполнители ``FF`` между сегментами пропускаются (T.81
#: B.1.1.2). Раньше первый же байт не на месте отдавал остаток файла как есть.
_JPEG_NEXT_MARKER = re.compile(rb"\xff[^\x00\xff]")
#: Конец энтропийных данных скана: ``FF`` и маркер C0..CF или D9..FE.
#: ``FF 00`` — байт-стаффинг, ``FF D0..D7`` — RST внутри скана, ``FF FF`` —
#: заполнитель. Одним поиском, а не циклом по байтам: цикл на Python разбирал
#: 10-мегабайтный скан из ``FF 00`` секунды.
_JPEG_SCAN_END = re.compile(rb"\xff[\xc0-\xcf\xd9-\xfe]")
#: Маркеры без длины: RST0..7, SOI, TEM.
_JPEG_STANDALONE = frozenset(range(0xD0, 0xD9)) | {0x01}
#: У настоящих снимков сегментов десятки (progressive + optimize — 25). Без
#: потолка файл из миллионов пустых сегментов разбирался бы секунды. Дальше
#: потолка не копируется ничего.
_JPEG_MAX_SEGMENTS = 4096
#: Куски PNG, которые остаются (DRF-2529 — белый список вместо чёрного):
#: данные, палитра, прозрачность, гамма и цветовое пространство без профиля,
#: плотность пикселей, анимация APNG. Прочее срезается — текст, EXIF, ``iCCP``,
#: C2PA (``caBX``) и приватные куски.
_PNG_KEEP = frozenset({
    b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"sBIT",
    b"bKGD", b"pHYs", b"acTL", b"fcTL", b"fdAT", b"cICP", b"mDCV", b"cLLI",
})
#: Чанки WebP, которые остаются (DRF-2529 — белый список): кадр, альфа,
#: расширенный заголовок, анимация. ``EXIF``, ``XMP ``, ``ICCP`` и неизвестные
#: срезаются. Флаги ``VP8X`` о срезанных чанках не сбрасываются — как и раньше,
#: libwebp такие файлы открывает.
_WEBP_KEEP = frozenset({b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF"})
#: Чанки внутри кадра анимации (``ANMF``): неизвестный чанк там спецификация
#: допускает, и libwebp его читает.
_WEBP_FRAME_KEEP = frozenset({b"ALPH", b"VP8 ", b"VP8L"})


def _orientation_of(data: bytes) -> int:
    """Ориентация снимка, или 1, если её нет и читать нечего."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            value = img.getexif().get(ORIENTATION_TAG)
    except Exception:  # noqa: BLE001 — битый EXIF не повод ронять загрузку
        return 1
    return value if isinstance(value, int) and 1 <= value <= 8 else 1


def _orientation_tiff(orientation: int) -> bytes:
    """Блок TIFF (заголовок + IFD0) с ОДНИМ тегом ориентации, без префикса ``Exif\\0\\0``.

    ``Image.Exif().tobytes()`` в Pillow 11 сам начинается с ``Exif\\0\\0``.
    Раньше префикс дописывался ещё раз, и сегмент выходил
    ``Exif\\0\\0Exif\\0\\0MM…``: Pillow такое прощает, Chromium — нет, и снимок
    в браузере показывался без поворота. Префикс снимается здесь, а ставится
    там, где его требует контейнер (JPEG — да, PNG ``eXIf`` — нет).
    """
    from PIL import Image

    exif = Image.Exif()
    exif[ORIENTATION_TAG] = orientation
    raw = exif.tobytes()
    return raw[6:] if raw.startswith(b"Exif\x00\x00") else raw


def _orientation_segment(orientation: int) -> bytes:
    """APP1 c ОДНИМ тегом ориентации и больше ничем."""
    payload = b"Exif\x00\x00" + _orientation_tiff(orientation)
    return b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload


def _orientation_chunk(orientation: int) -> bytes:
    """Кусок PNG ``eXIf`` с ОДНИМ тегом ориентации. По спецификации PNG — без ``Exif\\0\\0``."""
    data = _orientation_tiff(orientation)
    kind = b"eXIf"
    crc = zlib.crc32(kind + data).to_bytes(4, "big")
    return len(data).to_bytes(4, "big") + kind + data + crc


def _strip_jpeg(data: bytes) -> bytes:
    """JPEG без APPn/COM и без всего, что дописано после ``EOI``.

    Скан (энтропийные данные) копируется байт в байт до следующего маркера;
    у прогрессивного JPEG сканов несколько, между ними — таблицы, разбор идёт
    дальше. ``EOI`` — конец: хвост живого фото (MP4), второй кадр MPF и любой
    другой довесок не копируются (DRF-2529).
    """
    out = bytearray(data[:2])  # SOI
    i, n = 2, len(data)
    for _ in range(_JPEG_MAX_SEGMENTS):
        found = _JPEG_NEXT_MARKER.search(data, i)
        if found is None:
            break
        j = found.start()
        marker = data[j + 1]
        if marker == 0xD9:  # EOI — всё, что после него, не копируется
            out += b"\xff\xd9"
            break
        if marker in _JPEG_STANDALONE:
            i = j + 2
            continue
        if j + 4 > n:
            break
        end = j + 2 + max(int.from_bytes(data[j + 2:j + 4], "big"), 2)
        if end > n:
            # Сегмент длиннее файла. Срезаемый не копируется никогда: иначе
            # битая длина APP1 отдала бы EXIF целиком. Прочий — остаток как
            # есть, как и раньше.
            if marker not in _JPEG_DROP:
                out += data[j:]
            break
        if marker == 0xDA:  # SOS: заголовок скана и данные до следующего маркера
            scan_end = _JPEG_SCAN_END.search(data, end)
            if scan_end is None:
                # Файл оборван внутри скана — остаток как есть, как и раньше.
                out += data[j:]
                break
            out += data[j:scan_end.start()]
            i = scan_end.start()
            continue
        if marker not in _JPEG_DROP:
            out += data[j:end]
        i = end
    return bytes(out)


def _strip_png(data: bytes, orientation: int = 1) -> bytes:
    out = bytearray(data[:8])  # сигнатура
    i = 8
    while i + 8 <= len(data):
        length = int.from_bytes(data[i:i + 4], "big")
        kind = data[i + 4:i + 8]
        end = i + 12 + length
        if kind in _PNG_KEEP:
            out.extend(data[i:end])
            # Ориентация — сразу за IHDR: ``eXIf`` обязан стоять до IDAT.
            if kind == b"IHDR" and orientation != 1:
                out.extend(_orientation_chunk(orientation))
        i = end
        if kind == b"IEND":
            break
    return bytes(out)


def _webp_chunks(data: bytes, start: int, end: int, keep: frozenset, *, top: bool) -> bytes:
    """Чанки из ``data[start:end]`` по белому списку ``keep``; ни байта за ``end``."""
    i = start
    kept: list[bytes] = []
    while i + 8 <= end:
        kind = data[i:i + 4]
        length = int.from_bytes(data[i + 4:i + 8], "little")
        nxt = i + 8 + length + (length % 2)  # чанки выровнены по чётности
        if kind == b"ANMF" and top and length >= 16:
            # Кадр анимации: 16 байт заголовка кадра и его собственные чанки.
            inner = _webp_chunks(data, i + 24, min(i + 8 + length, end), _WEBP_FRAME_KEEP, top=False)
            payload = data[i + 8:i + 24] + inner
            pad = b"\x00" * (len(payload) % 2)
            kept.append(b"ANMF" + len(payload).to_bytes(4, "little") + payload + pad)
        elif kind in keep:
            kept.append(data[i:min(nxt, end)])
        i = nxt
    return b"".join(kept)


def _strip_webp(data: bytes) -> bytes:
    # DRF-2529 — конец контейнера берётся из заголовка RIFF: всё, что дописано
    # после, не копируется. Раньше обход шёл до конца файла, хвост читался как
    # чанки и попадал в новый размер RIFF — и уезжал, и ломал файл.
    riff_end = min(len(data), 8 + int.from_bytes(data[4:8], "little"))
    body = _webp_chunks(data, 12, riff_end, _WEBP_KEEP, top=True)
    # Размер в заголовке RIFF обязан сойтись, иначе файл не откроется.
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + body


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
        return _strip_png(data, _orientation_of(data))

    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        # Ориентация у WebP не возвращается: Chromium (движок Mini App) тег
        # ориентации у WebP не применяет вовсе — проверено 17.10 на снимке
        # с Orientation=6, — так что потеря тега на экране не видна.
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
