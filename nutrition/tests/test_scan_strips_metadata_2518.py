"""DRF-2518 — координаты не доезжают ни до хранилища, ни до провайдера.

`core/tests/test_image_privacy_2518.py` проверяет саму функцию срезания. Здесь
проверяется ПУТЬ: снимок с координатами проходит через ручку целиком, и на
обоих концах координат уже нет.

Почему узлы на обе ручки. Точек загрузки две — `FoodScanView` (клиент) и
`InternalFoodScanView` (бот). Закрыть одну и оставить вторую хуже, чем не
закрыть ни одной: появляется уверенность, что путь чист, а половина его
по-прежнему выпускает координаты наружу. Тот же довод, по которому лист
требует перепись носителей по конструктору, а не по имени файла.

Провайдеру проверка особенно важна: именно там байты покидают наш контур.
Рядом в коде стоит разбор 152-ФЗ ст. 18 п. 5 про локализацию — он про ФАКТ
передачи, а не про СОДЕРЖИМОЕ, и координаты уезжали при соблюдённой
локализации.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from PIL.ExifTags import IFD
from rest_framework.test import APIClient

from nutrition.models import FoodScan


def jpeg_with_coordinates() -> bytes:
    """Снимок, как его отдаёт телефон с включённой геометкой."""
    img = Image.new("RGB", (64, 48), (120, 160, 200))
    exif = img.getexif()
    exif[0x010F] = "ProbePhone"
    exif[0x0110] = "Model X"
    gps = exif.get_ifd(IFD.GPSInfo)
    gps[1] = "N"
    gps[2] = (55.0, 45.0, 21.36)
    gps[3] = "E"
    gps[4] = (37.0, 37.0, 2.0)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92, exif=exif)
    return buf.getvalue()


def has_gps(data: bytes) -> bool:
    return bool(dict(Image.open(io.BytesIO(data)).getexif().get_ifd(IFD.GPSInfo)))


#: Адрес берётся из соседнего файла узлов, а не выдумывается: первая
#: редакция писала "/api/nutrition/scan/" и получала 404, то есть
#: проверяла мою догадку о маршруте, а не срезание метаданных.
SCAN_URL = "/api/v1/nutrition/scan/"


def _router_result():
    """Ответ роутера в той же форме, что у соседних узлов файла `test_views`.

    Форму не выдумываю: `ScanResult` берёт `portion_g` и `raw_response`, а не
    `calories`/`raw` — первая редакция этих узлов их придумала и упала на
    `TypeError`, то есть проверяла не предмет, а мою догадку о соседях.
    """
    from nutrition.providers.base import ScanResult
    from nutrition.services.food_scanner_router import RouterResult

    return RouterResult(
        result=ScanResult(
            dish_name="Борщ",
            confidence=0.9,
            portion_g=300,
            ingredients=["свёкла"],
            provider="openai",
            latency_ms=400,
            raw_response={"ok": True},
        ),
        primary_provider_name="openai",
    )


@pytest.fixture
def auth_client(db):
    from users.models import Profile, User

    user = User.objects.create_user(
        username="nut-2518", password="x", role="client",
        phone="+79991112518",
    )
    Profile.objects.filter(user=user).update(full_name="Test", city="Penza")
    client = APIClient()
    client.defaults["HTTP_X_APP_TYPE"] = "client"
    client.force_authenticate(user=user)
    return client


class TestTheClientPath:
    def test_the_stored_image_has_no_coordinates(self, auth_client) -> None:
        dirty = jpeg_with_coordinates()
        # Положительная пара: вход правда несёт координаты. Без неё узел
        # зеленел бы на снимке, в котором их и не было.
        assert has_gps(dirty), "проба без координат ничего не доказывает"

        router = MagicMock()
        router.scan.return_value = _router_result()
        with patch("nutrition.views.FoodScannerRouter", return_value=router):
            response = auth_client.post(
                SCAN_URL,
                {"image": SimpleUploadedFile("food.jpg", dirty, "image/jpeg")},
                format="multipart",
            )

        assert response.status_code == 200, response.json()
        scan = FoodScan.objects.latest("created_at")
        stored = scan.image.read()
        assert not has_gps(stored), "координаты доехали до хранилища"
        assert b"ProbePhone" not in stored, "модель аппарата доехала до хранилища"

    def test_the_provider_receives_bytes_without_coordinates(
        self, auth_client
    ) -> None:
        """Главное место: здесь байты покидают наш контур."""
        dirty = jpeg_with_coordinates()
        assert has_gps(dirty)
        seen: dict[str, bytes] = {}

        def remember(image_bytes, *args, **kwargs):
            seen["bytes"] = image_bytes
            return _router_result()

        router = MagicMock()
        router.scan.side_effect = remember
        with patch("nutrition.views.FoodScannerRouter", return_value=router):
            auth_client.post(
                SCAN_URL,
                {"image": SimpleUploadedFile("food.jpg", dirty, "image/jpeg")},
                format="multipart",
            )

        assert "bytes" in seen, "провайдер не был вызван — узел ничего не проверил"
        assert not has_gps(seen["bytes"]), "координаты уехали провайдеру"


class TestBothUploadPointsAreClosed:
    def test_the_internal_path_strips_too(self) -> None:
        """Вторая точка — бот.

        Проверяется чтением исходника, а не прогоном: у внутренней ручки своя
        аутентификация по общему секрету, и поднимать её здесь значило бы
        проверять не тот предмет. Но пропустить её нельзя: одна закрытая и
        одна открытая точка создают уверенность там, где её нет.
        """
        from pathlib import Path

        import nutrition.views as views_module

        source = Path(views_module.__file__).read_text(encoding="utf-8")
        assert source.count("strip_metadata(image_file.read())") == 2, (
            "точек загрузки две, а срезание стоит не на обеих"
        )
        assert "image_bytes = image_file.read()" not in source, (
            "осталась точка загрузки, читающая байты без срезания"
        )
