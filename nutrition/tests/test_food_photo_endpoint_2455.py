"""Фото записи доезжает до человека — и только до своего (DRF-2455).

Снимок еды сохраняется (`FoodScan.image`), но **ни одна ручка чтения не
отдаёт на него ссылку**: до дневника фото не доходит вовсе. То, что
рисуется на экранах сканера, — локальный кадр из камеры, а не файл.

Отдавать прямой адрес хранилища нельзя дважды: он внутренний
(`endpoint_url=http://minio:9000`, `custom_domain=None`) — телефон его не
видит; и бакет `public-read` — адрес работал бы у любого, кто его знает,
а это фотографии людей, снятые дома. Поэтому файл отдаёт ручка, которая
спрашивает, чья это запись.

Узлы:

* k1 — своя запись с фото: 200 и **байты файла**, а не ссылка;
* k2 — **чужая запись: 404**, и тело файла не утекает. Ручка, никогда не
  видевшая чужого запроса, не доказана — поэтому подстановка чужого
  идентификатора здесь обязательна, а не «на всякий случай»;
* k3 — запись без фото (записана текстом или снимок удалён по сроку
  §134): 404, а не пустое тело;
* k4 — признак `has_photo` в самой записи: поверхность обязана знать, что
  показывать, ДО того как запросит файл.

Подмена для проверки k2: снять фильтр по владельцу в запросе записи —
k2 краснеет, отдавая чужое фото.
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_tz

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import FoodLog, FoodScan


pytestmark = pytest.mark.django_db

SERVICE_TOKEN = "test-token-DRF-2455"
DAY = datetime(2026, 9, 14, 9, 0, tzinfo=dt_tz.utc)
PHOTO_URL = "/api/v1/nutrition/internal/food-log/{log_id}/photo/"
DAY_URL = "/api/v1/nutrition/internal/summary/"


@pytest.fixture(autouse=True)
def _service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


#: Внешние идентификаторы, которыми бот представляется каталогу. Человека
#: за ними каталог заводит сам (ленивый прокси-пользователь), поэтому
#: записи создаются ИМЕННО для того, кого резолвит заголовок: иначе узел
#: мерил бы чужой дневник и проходил вхолостую.
OWNER_EXT = "max:2455-owner"
STRANGER_EXT = "max:2455-stranger"


def _person(external_id: str):
    from users.services import resolve_external_user

    return resolve_external_user(external_id)


@pytest.fixture
def owner(db):
    return _person(OWNER_EXT)


@pytest.fixture
def stranger(db):
    return _person(STRANGER_EXT)


def _client_for(external_id: str) -> APIClient:
    c = APIClient()
    c.credentials(
        HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
        HTTP_X_EXTERNAL_USER_ID=external_id,
    )
    return c


#: Минимальный настоящий PNG — файл обязан быть файлом, а не строкой.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _real_photo() -> bytes:
    """Настоящий PNG правдоподобного размера: шум не сжимается.

    DRF-2522: раньше здесь был PNG-заголовок плюс 40 000 нулей ПОСЛЕ
    ``IEND``. Хранимые снимки теперь проходят срезание метаданных, а оно
    отбрасывает всё после конца контейнера — стенд сжимался до 67 байт,
    и ручка честно отвечала «снимка нет». Размер обязан быть в самом
    изображении, а не в хвосте, который код вправе выбросить.
    """
    import io
    import random

    from PIL import Image

    side = 120
    noise = random.Random(2455).randbytes(side * side * 3)
    buf = io.BytesIO()
    Image.frombytes("RGB", (side, side), noise).save(buf, format="PNG")
    return buf.getvalue()


#: Снимок правдоподобного размера — десятки килобайт самих пикселей.
REAL_PHOTO_BYTES = _real_photo()


def _log_with_photo(user, *, dish="Овсяная каша"):
    scan = FoodScan.objects.create(
        user=user,
        dish_name=dish,
        confidence=0.9,
        portion_g=200,
        provider_used=FoodScan.Provider.OPENAI,
        # Настоящий снимок весит десятки килобайт; стенд это повторяет,
        # иначе узел проверял бы файл, который сам код считает пустышкой
        # (см. MIN_PHOTO_BYTES и замер 25.09).
        image=SimpleUploadedFile("meal.png", REAL_PHOTO_BYTES, content_type="image/png"),
        nutrition={"matched_dish": dish, "kcal": 320.0, "protein_g": 9.0,
                   "fat_g": 7.0, "carbs_g": 52.0, "portion_g": 200},
    )
    return FoodLog.objects.create(
        user=user, scan=scan, dish_name=dish, portion_multiplier=1.0,
        calories=320.0, protein_g=9.0, fat_g=7.0, carbs_g=52.0,
        meal_type="breakfast", logged_at=DAY,
    )


def _log_without_photo(user, *, dish="Борщ"):
    """Запись текстом — снимка не было; то же состояние после §134."""
    return FoodLog.objects.create(
        user=user, scan=None, dish_name=dish, portion_multiplier=1.0,
        calories=147.0, protein_g=4.8, fat_g=6.6, carbs_g=20.1,
        meal_type="lunch", logged_at=DAY,
    )


class TestK1OwnPhotoComesBackAsBytes:
    def test_the_file_itself_is_returned(self, owner):
        log = _log_with_photo(owner)

        resp = _client_for(OWNER_EXT).get(PHOTO_URL.format(log_id=log.id))

        assert resp.status_code == status.HTTP_200_OK
        assert resp["Content-Type"].startswith("image/")
        body = b"".join(resp.streaming_content) if resp.streaming else resp.content
        assert body[:8] == PNG_BYTES[:8]
        assert len(body) == len(REAL_PHOTO_BYTES)


class TestK2SomeoneElsePhotoIsNotReachable:
    def test_a_stranger_gets_404_and_no_bytes(self, owner, stranger):
        log = _log_with_photo(owner)

        resp = _client_for(STRANGER_EXT).get(PHOTO_URL.format(log_id=log.id))

        # 404, а не 403: по коду ответа нельзя перебирать чужие записи.
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        body = b"".join(resp.streaming_content) if resp.streaming else resp.content
        assert PNG_BYTES[:8] not in body

    def test_the_owner_still_gets_it(self, owner, stranger):
        """Положительная пара: тот же файл, другой спрашивающий."""
        log = _log_with_photo(owner)

        resp = _client_for(OWNER_EXT).get(PHOTO_URL.format(log_id=log.id))

        assert resp.status_code == status.HTTP_200_OK


class TestK3NoPhotoIsNotAnEmptyBody:
    def test_a_text_entry_has_no_file(self, owner):
        log = _log_without_photo(owner)

        resp = _client_for(OWNER_EXT).get(PHOTO_URL.format(log_id=log.id))

        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_a_purged_scan_leaves_the_entry_without_a_file(self, owner):
        """§134 — через 30 суток строка скана удаляется, запись остаётся."""
        log = _log_with_photo(owner)
        log.scan.delete()
        log.refresh_from_db()

        assert log.scan_id is None  # связь обнулена, запись жива
        resp = _client_for(OWNER_EXT).get(PHOTO_URL.format(log_id=log.id))

        assert resp.status_code == status.HTTP_404_NOT_FOUND


class TestK4TheEntryTellsWhetherAPhotoExists:
    def test_has_photo_is_true_for_a_photo_entry(self, owner):
        log = _log_with_photo(owner)

        resp = _client_for(OWNER_EXT).get(
            DAY_URL, {"date": log.logged_at.date().isoformat()},
        )

        assert resp.status_code == status.HTTP_200_OK
        entries = {e["id"]: e for e in resp.json()["data"]["entries"]}
        assert str(log.id) in entries
        assert entries[str(log.id)]["has_photo"] is True

    def test_has_photo_is_false_for_a_text_entry(self, owner):
        log = _log_without_photo(owner)

        resp = _client_for(OWNER_EXT).get(
            DAY_URL, {"date": log.logged_at.date().isoformat()},
        )

        entries = {e["id"]: e for e in resp.json()["data"]["entries"]}
        assert entries[str(log.id)]["has_photo"] is False


class TestK5TheBorderOfAuthenticationIsReal:
    """Без сервисного токена ручка не отвечает.

    Без этого узла можно было снять ``permission_classes`` — и весь файл
    остался бы зелёным, то есть граница входа не доказана ничем.
    """

    def test_no_service_token_means_no_file(self, owner):
        log = _log_with_photo(owner)

        resp = APIClient().get(PHOTO_URL.format(log_id=log.id))

        assert resp.status_code in (401, 403)

    def test_a_broken_external_id_is_refused(self, owner):
        log = _log_with_photo(owner)
        c = APIClient()
        c.credentials(HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN, HTTP_X_EXTERNAL_USER_ID="не-идентификатор")

        resp = c.get(PHOTO_URL.format(log_id=log.id))

        assert resp.status_code >= 400
        assert resp.status_code != 200


class TestK6ThreeRefusalsAreIndistinguishable:
    """«Чужая», «никогда не было» и «своя без фото» отвечают одинаково.

    Иначе по ответу можно перебирать чужие записи: разный код или разное
    тело сказали бы, что запись существует.
    """

    def test_the_three_answers_match_each_other(self, owner, stranger):
        someone_elses = _log_with_photo(owner)
        mine_without = _log_without_photo(owner)
        never_existed = "99999999-9999-4999-8999-999999999999"

        answers = [
            _client_for(STRANGER_EXT).get(PHOTO_URL.format(log_id=someone_elses.id)),
            _client_for(OWNER_EXT).get(PHOTO_URL.format(log_id=mine_without.id)),
            _client_for(OWNER_EXT).get(PHOTO_URL.format(log_id=never_existed)),
        ]

        # Сначала о наличии: все три ответа получены и это отказы.
        assert [r.status_code for r in answers] == [404, 404, 404]
        bodies = [r.json() for r in answers]
        # И об отсутствии различий: ни код, ни текст не выдают существование.
        assert bodies[0] == bodies[1] == bodies[2]


class TestK7AnObjectMissingFromStorage:
    """Строка ссылается на объект, которого в хранилище нет.

    Не гипотеза: команда очистки (§134) знает этот исход под именем
    ``object_absent`` и насчитала такие строки на пилоте. Человеку это то
    же «снимка нет», а не 500.
    """

    def test_a_dangling_reference_is_not_a_crash(self, owner):
        log = _log_with_photo(owner)
        # Файл убираем, строку оставляем — ровно то состояние пилота.
        log.scan.image.storage.delete(log.scan.image.name)

        resp = _client_for(OWNER_EXT).get(PHOTO_URL.format(log_id=log.id))

        assert resp.status_code == status.HTTP_404_NOT_FOUND


class TestK8ThePhotoIsNotCachedForEveryone:
    def test_the_answer_is_private(self, owner):
        log = _log_with_photo(owner)

        resp = _client_for(OWNER_EXT).get(PHOTO_URL.format(log_id=log.id))

        assert resp.status_code == status.HTTP_200_OK
        # Докстрока обещает приватность — узел это и проверяет.
        assert "private" in resp["Cache-Control"]


class TestK9AnEmptyObjectIsNotAPhoto:
    """Объект есть, но пуст — третье состояние, найденное замером 25.09.

    Три из пятнадцати живых строк на стенде ссылались на объект в
    несколько сотен байт. Отдать их байтами хуже, чем ответить «снимка
    нет»: человек увидел бы битую картинку, а поверхность не отличила бы
    её от настоящего снимка.
    """

    def test_a_few_hundred_bytes_are_treated_as_absent(self, owner):
        log = _log_with_photo(owner)
        # Кладём на то же имя пустышку — ровно то, что нашлось на стенде.
        log.scan.image.storage.delete(log.scan.image.name)
        log.scan.image.storage.save(log.scan.image.name, ContentFile(b"x" * 379))

        resp = _client_for(OWNER_EXT).get(PHOTO_URL.format(log_id=log.id))

        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_a_real_photo_still_passes(self, owner):
        """Положительная пара: порог не отсекает настоящий снимок."""
        log = _log_with_photo(owner)
        log.scan.image.storage.delete(log.scan.image.name)
        log.scan.image.storage.save(
            log.scan.image.name, ContentFile(REAL_PHOTO_BYTES),
        )

        resp = _client_for(OWNER_EXT).get(PHOTO_URL.format(log_id=log.id))

        assert resp.status_code == status.HTTP_200_OK
