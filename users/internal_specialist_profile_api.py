"""Профиль мастера, аватар и портфолио под субъектом (DRF-1813, M21; P57, P59–P62).

Экран 07 кабинета мастера ходит через бота, а у каталога до этого была одна
дверь записи профиля — Pro-JWT (``/api/v1/specialists/me/`` и
``…/me/portfolio/``), из бота недостижимая.

* ``GET/PATCH …/specialists/{id}/profile/`` — имя (не короче 2 символов) и
  «о себе» (не длиннее 500 — один лимит). Профиль без фото сохраняется:
  требование фото — пункт готовности к публикации (M4), не условие записи.
* ``POST/DELETE …/specialists/{id}/media/avatar/`` — JPEG / PNG / WebP,
  проверенные по содержимому (Pillow), а не по заявленному типу; ≤ 5 МБ;
  квадрат с допуском 2 %. Замена удаляет прежний файл.
* ``GET/POST …/specialists/{id}/portfolio/``, ``DELETE …/portfolio/{item_id}/``
  — не больше 10 фото (``limit`` в ответе — данные контракта, экран число
  не зашивает); ≤ 10 МБ, те же форматы.

**Сторож** — :class:`IsInternalBearerForSpecialistSubject`: связанный мастер
или pre-LINKED владелец своего DRAFT workspace; чужой профиль — 403.

**Журнал §96** — :class:`AuditedPersonalDataAccess`, категория
``specialist_profile``. Чтение — ``read_profile``, запись полей —
``write_specialist_profile``, загрузка фото — ``upload_media``, удаление —
``delete_media``. Удаление фото — разрушающая операция из закрытого списка
владельца (``privacy_audit.policy``): журнал недоступен — 503, ничего не
удалено, повтор безопасен.

**Файлы удаляются после фиксации транзакции** (``transaction.on_commit``).
Миксин журнала пишет строку в той же транзакции, что и изменение: упал
журнал — транзакция откатилась. Удали вид файл сразу, строка профиля после
отката указывала бы на файл, которого уже нет.
"""

from __future__ import annotations

import logging
from uuid import UUID

from django.db import transaction
from PIL import Image, UnidentifiedImageError
from rest_framework import serializers, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.errors import ErrorCode
from privacy_audit.mixins import AuditedPersonalDataAccess
from privacy_audit.models import PersonalDataAccessLog
from users.models import SpecialistPortfolio, SpecialistProfile
from users.permissions import IsInternalBearerForSpecialistSubject
from users.response import error_response, success_response

logger = logging.getLogger(__name__)

BIO_MAX_LENGTH = 500
DISPLAY_NAME_MIN_LENGTH = 2
AVATAR_MAX_BYTES = 5 * 1024 * 1024
PORTFOLIO_MAX_BYTES = 10 * 1024 * 1024
PORTFOLIO_LIMIT = 10
#: Лимиты в ответе профиля (DRF-1960) — ровно проверяемые значения.
PROFILE_LIMITS = {
    "bio": BIO_MAX_LENGTH,
    "display_name_min": DISPLAY_NAME_MIN_LENGTH,
    "avatar_bytes": AVATAR_MAX_BYTES,
    "portfolio_bytes": PORTFOLIO_MAX_BYTES,
    "portfolio_count": PORTFOLIO_LIMIT,
}
#: Допуск «квадрата» аватара: |ширина − высота| ≤ 2 % большей стороны.
SQUARE_TOLERANCE = 0.02
#: Формат по содержимому (Pillow) → допустимый заявленный тип.
ALLOWED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}

_Op = PersonalDataAccessLog.Operation


class MediaRefused(Exception):
    """Файл не принят — 400 с машинной причиной, ничего не записано."""

    def __init__(self, reason: str, **extra) -> None:
        super().__init__(reason)
        self.reason = reason
        self.extra = extra


def validate_image(upload, *, max_bytes: int, square: bool) -> None:
    """Проверить загрузку: наличие, размер, заявленный тип, содержимое, квадрат."""

    if upload is None:
        raise MediaRefused("image_required")
    if upload.size > max_bytes:
        raise MediaRefused("file_too_large", limit_bytes=max_bytes)
    if upload.content_type not in ALLOWED_FORMATS.values():
        raise MediaRefused("unsupported_type")
    try:
        upload.seek(0)
        Image.open(upload).verify()
        upload.seek(0)
        image = Image.open(upload)
        fmt, (width, height) = image.format, image.size
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise MediaRefused("not_an_image") from exc
    finally:
        upload.seek(0)
    if fmt not in ALLOWED_FORMATS:
        raise MediaRefused("unsupported_type")
    if square and abs(width - height) > SQUARE_TOLERANCE * max(width, height):
        raise MediaRefused("not_square")


def _delete_after_commit(field_file) -> None:
    name = field_file.name if field_file else ""
    if not name:
        return
    storage = field_file.storage
    transaction.on_commit(lambda: storage.delete(name))


def _url(field_file) -> str:
    return field_file.url if field_file else ""


def _profile(specialist_id: UUID, *, lock: bool = False) -> SpecialistProfile | None:
    qs = SpecialistProfile.objects.filter(pk=specialist_id)
    if lock:
        qs = qs.select_for_update(of=("self",))
    return qs.first()


def _profile_state(profile: SpecialistProfile) -> dict:
    return {
        "specialist_id": str(profile.pk),
        "display_name": profile.display_name,
        "bio": profile.bio or "",
        "avatar_url": _url(profile.avatar),
        "portfolio": {"count": profile.portfolio.count(), "limit": PORTFOLIO_LIMIT},
        # DRF-1960: те же константы, по которым проверяется запись, — один
        # источник для экрана профиля (Mini App своих чисел не держит).
        "limits": PROFILE_LIMITS,
    }


def _portfolio_item(item: SpecialistPortfolio) -> dict:
    return {
        "id": str(item.pk),
        "image_url": _url(item.image),
        "sort_order": item.sort_order,
        "created_at": item.created_at.isoformat(),
    }


def _no_specialist() -> Response:
    return error_response(
        ErrorCode.SPECIALIST_NOT_FOUND, "Specialist not found.", status_code=status.HTTP_404_NOT_FOUND,
    )


def _refused(exc: MediaRefused) -> Response:
    return error_response(
        ErrorCode.VALIDATION_ERROR,
        "File not accepted.",
        details={"reason": exc.reason, **exc.extra},
        status_code=status.HTTP_400_BAD_REQUEST,
    )


class _ProfileBody(serializers.Serializer):
    display_name = serializers.CharField(required=False, max_length=255)
    bio = serializers.CharField(
        required=False, allow_blank=True, max_length=BIO_MAX_LENGTH, trim_whitespace=False,
    )

    def validate_display_name(self, value: str) -> str:
        value = value.strip()
        if len(value) < DISPLAY_NAME_MIN_LENGTH:
            raise serializers.ValidationError(
                f"Имя должно содержать минимум {DISPLAY_NAME_MIN_LENGTH} символа."
            )
        return value


class _SpecialistProfileSurface(AuditedPersonalDataAccess, APIView):
    """Общее у четырёх видов: журнал, субъект по ``specialist_id``.

    ``permission_classes`` здесь намеренно не задан — его объявляет каждый
    вид: перепись журнала §96 ищет охраняемые виды по этому атрибуту, и
    база с ним считалась бы пятым маршрутом.
    """

    authentication_classes: list = []
    subject_url_kwarg = "specialist_id"
    audit_object_category = PersonalDataAccessLog.ObjectCategory.SPECIALIST_PROFILE


class InternalSpecialistProfileView(_SpecialistProfileSurface):
    permission_classes = [IsInternalBearerForSpecialistSubject]
    parser_classes = [JSONParser]
    audit_operations = {"GET": _Op.READ_PROFILE, "PATCH": _Op.WRITE_SPECIALIST_PROFILE}

    def get(self, request: Request, specialist_id: UUID) -> Response:
        profile = _profile(specialist_id)
        if profile is None:
            return _no_specialist()
        return success_response(_profile_state(profile))

    def patch(self, request: Request, specialist_id: UUID) -> Response:
        profile = _profile(specialist_id, lock=True)
        if profile is None:
            return _no_specialist()
        body = _ProfileBody(data=request.data)
        if not body.is_valid():
            return error_response(
                ErrorCode.VALIDATION_ERROR,
                f"display_name ≥ {DISPLAY_NAME_MIN_LENGTH} characters, bio ≤ {BIO_MAX_LENGTH}.",
                details=body.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        changed = []
        for field in ("display_name", "bio"):
            if field in body.validated_data:
                setattr(profile, field, body.validated_data[field])
                changed.append(field)
        if changed:
            profile.save(update_fields=changed)
        return success_response(_profile_state(profile))


class InternalSpecialistAvatarView(_SpecialistProfileSurface):
    permission_classes = [IsInternalBearerForSpecialistSubject]
    parser_classes = [MultiPartParser, FormParser]
    audit_operations = {"POST": _Op.UPLOAD_MEDIA, "DELETE": _Op.DELETE_MEDIA}

    def post(self, request: Request, specialist_id: UUID) -> Response:
        profile = _profile(specialist_id, lock=True)
        if profile is None:
            return _no_specialist()
        upload = request.FILES.get("image")
        try:
            validate_image(upload, max_bytes=AVATAR_MAX_BYTES, square=True)
        except MediaRefused as exc:
            return _refused(exc)
        previous = profile.avatar if profile.avatar else None
        previous_name = previous.name if previous else ""
        profile.avatar = upload
        profile.save(update_fields=["avatar"])
        if previous is not None and previous_name != profile.avatar.name:
            _delete_after_commit(previous)
        logger.info("users.specialist_avatar.uploaded specialist=%s bytes=%s", profile.pk, upload.size)
        return success_response(_profile_state(profile))

    def delete(self, request: Request, specialist_id: UUID) -> Response:
        profile = _profile(specialist_id, lock=True)
        if profile is None:
            return _no_specialist()
        if profile.avatar:
            _delete_after_commit(profile.avatar)
            profile.avatar = None
            profile.save(update_fields=["avatar"])
        return success_response(_profile_state(profile))


class InternalSpecialistPortfolioView(_SpecialistProfileSurface):
    permission_classes = [IsInternalBearerForSpecialistSubject]
    parser_classes = [MultiPartParser, FormParser]
    audit_operations = {"GET": _Op.READ_PROFILE, "POST": _Op.UPLOAD_MEDIA}

    def get(self, request: Request, specialist_id: UUID) -> Response:
        profile = _profile(specialist_id)
        if profile is None:
            return _no_specialist()
        items = list(profile.portfolio.all())
        return success_response({
            "items": [_portfolio_item(item) for item in items],
            "count": len(items),
            "limit": PORTFOLIO_LIMIT,
        })

    def post(self, request: Request, specialist_id: UUID) -> Response:
        # Блокировка профиля сериализует две загрузки: иначе обе увидели бы
        # «9 из 10» и лимит стал бы 11.
        profile = _profile(specialist_id, lock=True)
        if profile is None:
            return _no_specialist()
        count = profile.portfolio.count()
        if count >= PORTFOLIO_LIMIT:
            return error_response(
                ErrorCode.VALIDATION_ERROR,
                f"Portfolio holds at most {PORTFOLIO_LIMIT} photos.",
                details={"reason": "portfolio_limit_exceeded", "limit": PORTFOLIO_LIMIT},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        upload = request.FILES.get("image")
        try:
            validate_image(upload, max_bytes=PORTFOLIO_MAX_BYTES, square=False)
        except MediaRefused as exc:
            return _refused(exc)
        item = SpecialistPortfolio.objects.create(specialist=profile, image=upload, sort_order=count)
        logger.info(
            "users.specialist_portfolio.uploaded specialist=%s item=%s bytes=%s",
            profile.pk, item.pk, upload.size,
        )
        return success_response(_portfolio_item(item), status_code=status.HTTP_201_CREATED)


class InternalSpecialistPortfolioItemView(_SpecialistProfileSurface):
    permission_classes = [IsInternalBearerForSpecialistSubject]
    audit_operations = {"DELETE": _Op.DELETE_MEDIA}

    def delete(self, request: Request, specialist_id: UUID, item_id: UUID) -> Response:
        profile = _profile(specialist_id, lock=True)
        if profile is None:
            return _no_specialist()
        item = profile.portfolio.filter(pk=item_id).first()
        if item is None:
            return error_response(
                ErrorCode.NOT_FOUND,
                "Portfolio item not found.",
                details={"reason": "portfolio_item_not_found"},
                status_code=status.HTTP_404_NOT_FOUND,
            )
        _delete_after_commit(item.image)
        item.delete()
        return success_response({"count": profile.portfolio.count(), "limit": PORTFOLIO_LIMIT})
