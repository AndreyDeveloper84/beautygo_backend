"""Мастер действует над СВОЕЙ клиентской записью из бота — DRF-2785.

Решение владельца 05.10 (вариант «в»): мастер в боте отвечает на клиентскую
запись «✅ Подтверждаю» или «❌ Не смогу», а из «Моего дня» закрывает визит,
отмечает неявку или переносит. До этого актором на поверхности бота был
только администратор салона (``tenants/appointments_api.py``, DRF-1063 блок D,
``IsTenantAdmin``), а мастерские действия жили лишь на мобильном пути
Pro-JWT, которого на пилоте нет.

    POST /api/v1/internal/specialists/{specialist_id}/appointments/{appointment_id}/acknowledge/
    POST …/cancel/      POST …/complete/      POST …/no-show/      POST …/reschedule/

Кто зовёт
---------
Бот, служебным Bearer, называя мастера в ``X-External-User-ID``.
:class:`users.permissions.IsInternalBearerForSpecialistSubject` проверяет,
что это ЕГО профиль в URL (мастер со связанным MAX). Дальше — та же
``resolve_booking_operator``, что у мобильного и салонного путей: мастер
действует как ``specialist`` только на строке, где он мастер.

Запись другого мастера, другого салона, несуществующая — **404**, не 403:
поверхность не подтверждает, что чужой id существует (DRF-1036).

Ничего не реализовано заново
----------------------------
Каждая ручка — тонкая обёртка над тем, что уже зовут салон и мобильный путь:
``CancelBookingService``, ``RescheduleBookingService``, ``close_booking``,
``mark_booking_no_show``. Отличаются только актор (``specialist``) и
основание (``internal_bot``). Поэтому отмена мастером идёт по политике «без
вины клиента» (``StandardCancellationPolicy.NO_FAULT_INITIATORS``, возврат
100 %), а событие несёт ``cancelled_by=master``. Новое здесь одно —
``acknowledge``: факт ответа без смены статуса
(``appointments.application.services.acknowledgement``).

В ответе нет людей
------------------
Только идентификаторы, статус, версия, время и подтверждение. Имя и телефон
клиента у бота уже есть в его зеркале записей, а эта поверхность ходит под
общим служебным токеном — тот же выбор, что у ``InternalSpecialistScheduleView``
(«there is no personal data in this response at all»). Поэтому здесь нет
и журнала доступа к персональным данным: доступа к ним нет.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from appointments.application.dto import CancelBookingDTO, RescheduleBookingDTO
from appointments.application.services.cancel_reschedule_service import (
    CancelBookingService,
    RescheduleBookingService,
)
from appointments.domain.exceptions import (
    AppointmentTerminalError,
    BookingWindowError,
    CancellationNotAllowedError,
    InvalidStateTransitionError,
    RescheduleNotAllowedError,
    SlotNotAvailableError,
    StaleVersionError,
    TenantMismatchError,
)
from appointments.domain.value_objects import OperationalActor
from appointments.models import Appointment
from users.models import SpecialistProfile
from users.permissions import IsInternalBearerForSpecialistSubject
from users.response import error_response, success_response

logger = logging.getLogger(__name__)

#: Основание в журнале переноса (``AppointmentRevision.basis``): канал запроса.
BASIS = "internal_bot"


class _VersionedSerializer(serializers.Serializer):
    """``expected_version`` обязателен: бот показывает кнопку по прочитанной записи.

    Подтверждение, закрытие и неявка относятся к тому времени визита, которое
    мастер видел. Если запись перенесли, пока он думал, ответ — 409, а не
    молчаливое подтверждение времени, которого он не видел.
    """

    expected_version = serializers.IntegerField(required=True, min_value=1)


class _CancelSerializer(serializers.Serializer):
    #: Необязательна: «Не смогу» — уже причина. Отменить запись, которую
    #: перенесли, мастер вправе и без неё; с ней — 409 на расхождении.
    expected_version = serializers.IntegerField(required=False, min_value=1)
    #: Свободный текст для людей. В ``reason_code`` не превращается: код —
    #: по роли, ``master_unavailable`` (``_resolve_cancellation_vocab``).
    reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=500,
    )


class _RescheduleSerializer(serializers.Serializer):
    new_start_datetime = serializers.DateTimeField()
    expected_version = serializers.IntegerField(required=True, min_value=1)


def booking_state(appointment) -> dict:
    """Состояние записи для бота — без людей (см. шапку модуля)."""
    acknowledged_version = appointment.master_acknowledged_version
    return {
        "appointment_id": str(appointment.id),
        "specialist_id": str(appointment.specialist_id),
        "status": appointment.status,
        "version": appointment.version,
        "start_at": appointment.start_datetime.isoformat(),
        "end_at": appointment.end_datetime.isoformat(),
        "master_acknowledged_at": (
            appointment.master_acknowledged_at.isoformat()
            if appointment.master_acknowledged_at else None
        ),
        "master_acknowledged_version": acknowledged_version,
        # Подтверждено ли ТЕКУЩЕЕ время визита: после переноса прежнее
        # подтверждение остаётся в истории, но здесь — False.
        "acknowledged": (
            acknowledged_version is not None
            and acknowledged_version == appointment.version
        ),
    }


class _SpecialistBookingBase(APIView):
    # Пусто, как у салонных ручек: JWT-аутентификатор по умолчанию ответил бы
    # 401 на служебный Bearer раньше, чем спросят разрешение.
    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSpecialistSubject]
    subject_url_kwarg = "specialist_id"

    def _locked_own_booking(self, specialist_id, appointment_id):
        """Строка записи ЭТОГО мастера под блокировкой — или None (→ 404).

        Фильтр по мастеру стоит в самом запросе: чужая запись неотличима от
        несуществующей, и до неё не доходит ни одна проверка состояния.
        ``of=("self",)`` — ``service`` допускает NULL (AMD-019), внешнее
        соединение под голым FOR UPDATE Postgres не принимает.
        """
        return (
            Appointment.objects
            .select_for_update(of=("self",))
            .select_related("specialist", "specialist__user", "client", "service")
            .filter(pk=appointment_id, specialist_id=specialist_id)
            .first()
        )

    @staticmethod
    def _own_booking(specialist_id, appointment_id):
        """Без блокировки — для отмены и переноса: свою блокировку берут сервисы."""
        return (
            Appointment.objects
            .select_related("specialist", "specialist__user")
            .filter(pk=appointment_id, specialist_id=specialist_id)
            .first()
        )

    @staticmethod
    def _authority(specialist_id, appointment):
        """В каком качестве ДЕЙСТВУЮЩИЙ мастер выступает на этой строке — общей функцией.

        ``resolve_booking_operator`` читает ``request.user``, а на этой
        поверхности ``request.user`` — аноним: человека называет заголовок, и
        разрешение уже доказало, что он владелец профиля ``specialist_id`` из
        URL. Поэтому функция зовётся от имени пользователя ЭТОГО профиля —
        действующего, а не мастера записи: правило «specialist на своей
        строке» одно на мобильный, салонный и бот-путь, и чужая запись даёт
        ``None`` здесь, независимо от фильтра в запросе выше. Салонного
        полномочия эта поверхность не даёт (``tenant=None``): ответ — только
        ``specialist`` или ``None``.
        """
        from appointments.authz import resolve_booking_operator

        profile = (
            SpecialistProfile.objects.select_related("user")
            .filter(pk=specialist_id).first()
        )
        if profile is None:
            return None
        acting = SimpleNamespace(user=profile.user, tenant=None)
        actor = resolve_booking_operator(acting, appointment)
        return actor if actor == OperationalActor.SPECIALIST.value else None

    @staticmethod
    def _not_found() -> Response:
        return error_response("NOT_FOUND", "Appointment not found.", status_code=404)

    @staticmethod
    def _stale(appointment, expected_version) -> Response:
        return error_response(
            "STALE_VERSION",
            f"Appointment {appointment.id} expected_version={expected_version} "
            f"but current version is {appointment.version}.",
            status_code=409,
        )


class InternalSpecialistBookingAcknowledgeView(_SpecialistBookingBase):
    """«✅ Подтверждаю». Статус не меняется; повтор на той же версии — 200 без события."""

    def post(self, request: Request, specialist_id, appointment_id) -> Response:
        from appointments.application.services.acknowledgement import acknowledge_booking

        body = _VersionedSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        expected_version = body.validated_data["expected_version"]

        try:
            with transaction.atomic():
                appointment = self._locked_own_booking(specialist_id, appointment_id)
                if appointment is None or self._authority(specialist_id, appointment) is None:
                    return self._not_found()
                if appointment.version != expected_version:
                    return self._stale(appointment, expected_version)
                outcome = acknowledge_booking(
                    appointment, acknowledged_by_user_id=appointment.specialist.user_id,
                )
        except DjangoValidationError as exc:
            return error_response("INVALID_STATUS", str(exc.message), status_code=422)

        logger.info(
            "specialist.booking_acknowledged appointment_id=%s specialist=%s recorded=%s",
            appointment.id, specialist_id, outcome.recorded,
        )
        return success_response({**booking_state(appointment), "recorded": outcome.recorded})


class InternalSpecialistBookingCancelView(_SpecialistBookingBase):
    """«❌ Не смогу». Отмена мастером — без вины клиента."""

    def post(self, request: Request, specialist_id, appointment_id) -> Response:
        body = _CancelSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        appointment = self._own_booking(specialist_id, appointment_id)
        if appointment is None or self._authority(specialist_id, appointment) is None:
            return self._not_found()
        expected_version = body.validated_data.get("expected_version")
        if expected_version is not None and appointment.version != expected_version:
            return self._stale(appointment, expected_version)

        dto = CancelBookingDTO(
            booking_id=appointment.id,
            initiator_user_id=appointment.specialist.user_id,
            initiator_role=OperationalActor.SPECIALIST.value,
            reason=body.validated_data.get("reason", ""),
        )
        try:
            CancelBookingService().execute(dto)
        except CancellationNotAllowedError as exc:
            return error_response("CANCELLATION_NOT_ALLOWED", str(exc), status_code=422)
        except InvalidStateTransitionError as exc:
            return error_response("INVALID_STATUS", str(exc), status_code=422)

        appointment.refresh_from_db()
        logger.info(
            "specialist.booking_cancelled appointment_id=%s specialist=%s",
            appointment.id, specialist_id,
        )
        return success_response(booking_state(appointment))


class InternalSpecialistBookingRescheduleView(_SpecialistBookingBase):
    """Перенос своей записи. Рабочие часы мастеру не помеха (как на мобильном пути)."""

    def post(self, request: Request, specialist_id, appointment_id) -> Response:
        body = _RescheduleSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        appointment = self._own_booking(specialist_id, appointment_id)
        if appointment is None or self._authority(specialist_id, appointment) is None:
            return self._not_found()

        dto = RescheduleBookingDTO(
            booking_id=appointment.id,
            initiator_user_id=appointment.specialist.user_id,
            new_start_at=body.validated_data["new_start_datetime"],
            initiator_role=OperationalActor.SPECIALIST.value,
            expected_version=body.validated_data["expected_version"],
            tenant_id=appointment.tenant_id,
            command_key=request.META.get("HTTP_X_IDEMPOTENCY_KEY") or None,
            basis=BASIS,
        )
        try:
            RescheduleBookingService().execute(dto)
        except SlotNotAvailableError as exc:
            return error_response("SLOT_NOT_AVAILABLE", str(exc), status_code=409)
        except StaleVersionError as exc:
            return error_response("STALE_VERSION", str(exc), status_code=409)
        except AppointmentTerminalError as exc:
            return error_response("APPOINTMENT_TERMINAL", str(exc), status_code=409)
        except RescheduleNotAllowedError as exc:
            return error_response("RESCHEDULE_NOT_ALLOWED", str(exc), status_code=422)
        except InvalidStateTransitionError as exc:
            return error_response("INVALID_STATUS", str(exc), status_code=422)
        except BookingWindowError as exc:
            return error_response("BOOKING_WINDOW_INVALID", str(exc), status_code=400)
        except TenantMismatchError:
            return self._not_found()

        appointment.refresh_from_db()
        logger.info(
            "specialist.booking_rescheduled appointment_id=%s specialist=%s version=%s",
            appointment.id, specialist_id, appointment.version,
        )
        return success_response(booking_state(appointment))


class InternalSpecialistBookingCompleteView(_SpecialistBookingBase):
    """«Визит состоялся» — тот же ``close_booking``, что у салона и мобильного пути."""

    def post(self, request: Request, specialist_id, appointment_id) -> Response:
        from appointments.application.services.completion import (
            close_booking,
            schedule_capture_safely,
        )

        body = _VersionedSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        expected_version = body.validated_data["expected_version"]

        try:
            with transaction.atomic():
                appointment = self._locked_own_booking(specialist_id, appointment_id)
                if appointment is None:
                    return self._not_found()
                actor = self._authority(specialist_id, appointment)
                if actor is None:
                    return self._not_found()
                if appointment.version != expected_version:
                    return self._stale(appointment, expected_version)
                close_booking(appointment, completed_by=actor)
        except DjangoValidationError as exc:
            return error_response("INVALID_STATUS", str(exc.message), status_code=422)

        # Вне транзакции, как у салона: закрытие уже состоялось, сбой брокера
        # не должен выглядеть провалом факта.
        schedule_capture_safely(appointment)
        logger.info(
            "specialist.booking_completed appointment_id=%s specialist=%s",
            appointment.id, specialist_id,
        )
        return success_response(booking_state(appointment))


class InternalSpecialistBookingNoShowView(_SpecialistBookingBase):
    """«Не пришёл» — тот же ``mark_booking_no_show``, что у салона и мобильного пути."""

    def post(self, request: Request, specialist_id, appointment_id) -> Response:
        from appointments.application.services.completion import mark_booking_no_show

        body = _VersionedSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        expected_version = body.validated_data["expected_version"]

        try:
            with transaction.atomic():
                appointment = self._locked_own_booking(specialist_id, appointment_id)
                if appointment is None:
                    return self._not_found()
                actor = self._authority(specialist_id, appointment)
                if actor is None:
                    return self._not_found()
                if appointment.version != expected_version:
                    return self._stale(appointment, expected_version)
                mark_booking_no_show(appointment, marked_by=actor)
        except DjangoValidationError as exc:
            return error_response("INVALID_STATUS", str(exc.message), status_code=422)

        logger.info(
            "specialist.booking_no_show appointment_id=%s specialist=%s",
            appointment.id, specialist_id,
        )
        return success_response(booking_state(appointment))
