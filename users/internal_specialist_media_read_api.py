"""Фото мастера и работы портфолио — сам файл, для бота (DRF-2539).

Каталог отдавал наружу адрес хранилища: ``FieldFile.url`` при
``endpoint_url=http://minio:9000``, ``custom_domain=None`` и подписи на час
(умолчание django-storages) — ``http://minio:9000/…?AWSAccessKeyId=…&
Signature=…&Expires=…``. Телефон этот хост не видит, подпись умирает через
час, а в адресе у каждого клиента лежит имя ключа хранилища. Подпись при этом
не защищает ничего: бакет ``public-read``.

Решение владельца 29.09 — вариант 3, «отдача через наш бэкенд»: каталог
отдаёт байты боту, бот — Mini App. Приём тот же, что у снимка еды
(``nutrition.views.InternalFoodLogPhotoView``, DRF-2455).

# Кому отдаём

Сервисному токену бота (``IsInternalBearer``) — без субъекта. Фото мастера и
его работы — публичное лицо мастера: их уже показывает публичная лента
специалистов и витрина бота. Это чтение, не запись: запись аватара и
портфолио остаётся за субъектной ручкой мастера
(``internal_specialist_profile_api``).

# Один ответ на все отсутствия

Нет мастера, нет фото, объект пропал из хранилища, объект пуст — всё 404.
Пустое тело с 200 поверхность прочитала бы как «фото есть, но сломано».
"""

from __future__ import annotations

import logging
import mimetypes
from uuid import UUID

from django.http import FileResponse
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from users.models import SpecialistPortfolio, SpecialistProfile
from users.permissions import IsInternalBearer
from users.response import error_response

logger = logging.getLogger(__name__)

#: Меньше — не фото, а заглушка или обрезок (замер DRF-2455: объекты в
#: несколько сотен байт на стенде). Порог — тот же, что у снимка еды.
MIN_IMAGE_BYTES = 1024

#: Отдаём только картинки: тип из имени, иное — ``octet-stream``.
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


def _absent() -> Response:
    return error_response("NOT_FOUND", "Фото нет", status_code=status.HTTP_404_NOT_FOUND)


def _file_response(field_file, *, what: str, key: object) -> FileResponse | Response:
    """Байты файла или 404 — одинаковый на «нет», «пропал», «пуст»."""
    if not field_file:
        return _absent()
    try:
        handle = field_file.open("rb")
    except (FileNotFoundError, OSError) as exc:
        logger.warning("users.specialist_media.object_absent what=%s key=%s err=%s", what, key, type(exc).__name__)
        return _absent()
    try:
        size = field_file.size
    except (FileNotFoundError, OSError):
        size = 0
    if size < MIN_IMAGE_BYTES:
        handle.close()
        logger.warning("users.specialist_media.object_empty what=%s key=%s size=%s", what, key, size)
        return _absent()
    content_type = mimetypes.guess_type(field_file.name)[0] or "application/octet-stream"
    if content_type not in IMAGE_TYPES:
        content_type = "application/octet-stream"
    response = FileResponse(handle, content_type=content_type)
    # Приватно и ненадолго: общий кэш между ботом и хранилищем не нужен,
    # а смену фото бот сбивает версией в своём адресе.
    response["Cache-Control"] = "private, max-age=300"
    return response


class InternalSpecialistAvatarFileView(APIView):
    """GET /api/v1/internal/specialists/{id}/media/avatar/file/ — байты аватара."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]
    throttle_classes = [ScopedRateThrottle]
    #: Свой бюджет: витрина открывает десятки мастеров сразу.
    throttle_scope = "specialist_media_internal"

    def get(self, request: Request, specialist_id: UUID):
        profile = SpecialistProfile.objects.filter(pk=specialist_id).only("avatar").first()
        if profile is None:
            return _absent()
        return _file_response(profile.avatar, what="avatar", key=specialist_id)


class InternalSpecialistPortfolioFileView(APIView):
    """GET /api/v1/internal/specialists/{id}/portfolio/{item}/file/ — байты работы."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "specialist_media_internal"

    def get(self, request: Request, specialist_id: UUID, item_id: UUID):
        # Фильтр по мастеру — в самом запросе: работа чужого мастера под
        # этим адресом не отдаётся (404, как «нет»).
        item = SpecialistPortfolio.objects.filter(pk=item_id, specialist_id=specialist_id).first()
        if item is None:
            return _absent()
        return _file_response(item.image, what="portfolio", key=item_id)
