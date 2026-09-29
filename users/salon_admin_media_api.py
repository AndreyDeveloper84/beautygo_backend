"""Фото мастера рукой администратора салона (DRF-2619).

``POST /api/v1/tenants/me/masters/{specialist_id}/media/avatar/``.

До этой двери администратор салона грузил фото мастера на диск БОТА, а
зеркало бота держит ``photo_url`` отражением ``avatar_url`` каталога без
оговорок — первая же синхронизация стирала адрес. Кнопка делала вид, что
работает. Владелец байтов фото мастера — каталог, одна дверь для мастера
(``…/internal/specialists/{id}/media/avatar/``) и эта — для администратора.

**Кто.** Только собственный токен администратора салона по подписи MAX
(DRF-2607, решение владельца 29.09 «правом самого администратора салона —
его личность, его токен»). Ни служебного ключа, ни обычного JWT: у двери
одна форма опознания, и журнал всегда пишет одно и то же
``caller_purpose``. Токен привязан к одному салону —
:class:`SalonAdminTokenAuthentication` отказывает, если ``X-Tenant`` называет
другой.

**Может ли.** Те же права, что у расписания салона (``_ADMIN_PERMISSIONS``):
активная связь ``role=admin`` в ЭТОМ салоне. И мастер обязан принадлежать
этому салону: администратор салона A не грузит фото мастеру салона B —
мастер чужого салона «не найден» (404), как у отгулов и исключений.

**Что.** Те же проверки, что у двери мастера (:func:`validate_image`):
JPEG / PNG / WebP по содержимому, ≤ 5 МБ, квадрат с допуском 2 %. Замена
удаляет прежний файл после фиксации транзакции.

**Журнал §96.** Строка на каждую попытку аутентифицированного
администратора — и допущенную, и отказанную (не админ этого салона, мастер
чужого салона) — с заполненным ``tenant``. Запись фото и строка журнала —
одна транзакция. Миксин :class:`AuditedPersonalDataAccess` здесь не
годится: он читает вердикт субъектного сторожа внутренней поверхности, а
у салонной поверхности вердикт выносит ``IsTenantAdminOrPlatformAdmin``,
который его не публикует.
"""

from __future__ import annotations

import logging
from uuid import UUID

from django.db import transaction
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.errors import ErrorCode
from privacy_audit.models import PersonalDataAccessLog
from privacy_audit.services import AuditUnavailable, basis_from, record_or_lose

from .internal_specialist_profile_api import (
    AVATAR_MAX_BYTES,
    MediaRefused,
    _delete_after_commit,
    _profile_state,
    _refused,
    validate_image,
)
from .max_salon_admin_auth import SalonAdminTokenAuthentication, auth_method
from .response import error_response, success_response
from .schedule_admin_api import _ADMIN_PERMISSIONS

logger = logging.getLogger(__name__)

_Log = PersonalDataAccessLog


def _record(request: Request, specialist_id, *, allowed: bool, reason: str = "") -> None:
    """Строка §96 от имени администратора; отказ записи — по правилу §107."""
    record_or_lose(
        caller_purpose=_Log.CallerPurpose.SALON_ADMIN_TOKEN,
        actor=request.user,
        object_id=specialist_id,
        operation=_Log.Operation.UPLOAD_MEDIA,
        object_category=_Log.ObjectCategory.SPECIALIST_PROFILE,
        result=_Log.Result.ALLOWED if allowed else _Log.Result.DENIED,
        actor_named=True,
        denial_reason=reason,
        basis=basis_from(request),
        request_id=str(getattr(request, "request_id", "") or ""),
        tenant=getattr(request, "tenant", None),
    )


class AdminSpecialistAvatarView(APIView):
    """См. докстринг модуля."""

    authentication_classes = [SalonAdminTokenAuthentication]
    permission_classes = _ADMIN_PERMISSIONS
    parser_classes = [MultiPartParser, FormParser]

    def permission_denied(self, request: Request, message=None, code=None):
        # Отказ аутентифицированному — тоже попытка (§96): администратор
        # другого салона или человек без связи ``admin``. Анонимный стук
        # журнала не растит — как у внутренней поверхности.
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            _record(request, self.kwargs.get("specialist_id"), allowed=False, reason="not_salon_admin")
        super().permission_denied(request, message=message, code=code)

    def post(self, request: Request, specialist_id: UUID) -> Response:
        from .models import SpecialistProfile

        try:
            with transaction.atomic():
                # Мастер — ЭТОГО салона: тенант из токена и X-Tenant, никогда
                # из тела. Мастер чужого салона и мастер без салона — 404.
                profile = (
                    SpecialistProfile.objects.select_for_update(of=("self",))
                    .filter(pk=specialist_id, tenant=request.tenant)
                    .first()
                )
                if profile is None:
                    _record(request, specialist_id, allowed=False, reason="specialist_not_in_salon")
                    return error_response(
                        ErrorCode.SPECIALIST_NOT_FOUND,
                        "Specialist not found.",
                        status_code=status.HTTP_404_NOT_FOUND,
                    )
                _record(request, specialist_id, allowed=True)
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
        except AuditUnavailable:
            return error_response(
                "SERVICE_UNAVAILABLE",
                "Access to personal data requires a durable audit record, and the "
                "audit journal is unavailable.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        logger.info(
            "salon.master_avatar_uploaded actor=%s via=%s tenant=%s specialist=%s bytes=%s",
            request.user.pk, auth_method(request), request.tenant.pk, profile.pk, upload.size,
        )
        return success_response(_profile_state(profile))
