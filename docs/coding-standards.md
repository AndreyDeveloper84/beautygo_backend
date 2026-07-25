# Coding Standards (Ayla backend)

> Вынесено из `CLAUDE.md` (chore/slim-claude-md). Это справочник — читать при написании нового кода, а не на каждой задаче. Краткая таблица naming-conventions остаётся в `CLAUDE.md`.

## Python Style
```python
# ✅ GOOD
class AppointmentService:
    """Service for managing appointments."""

    def __init__(self, appointment_repo: AppointmentRepository):
        self._repo = appointment_repo

    def create_appointment(
        self,
        client_id: UUID,
        specialist_id: UUID,
        service_id: UUID,
        start_datetime: datetime,
    ) -> Appointment:
        """
        Create a new appointment.

        Args:
            client_id: The client's UUID
            specialist_id: The specialist's UUID
            service_id: The service UUID
            start_datetime: When the appointment starts

        Returns:
            The created appointment

        Raises:
            SlotNotAvailableError: If the slot is taken
            ValidationError: If input is invalid
        """
        if not self._is_slot_available(specialist_id, start_datetime):
            raise SlotNotAvailableError("Slot is not available")

        appointment = Appointment(
            client_id=client_id,
            specialist_id=specialist_id,
            service_id=service_id,
            start_datetime=start_datetime,
        )

        return self._repo.save(appointment)

# ❌ BAD
class apptService:
    def create(self, c, s, svc, dt):
        a = Appointment(client_id=c, specialist_id=s)
        a.save()
        return a
```

## Naming Conventions
| Тип | Стиль | Пример |
|-----|-------|--------|
| Класс | PascalCase | `AppointmentService` |
| Функция/метод | snake_case | `create_appointment` |
| Переменная | snake_case | `user_id` |
| Константа | SCREAMING_SNAKE | `MAX_SLOTS_PER_DAY` |
| Модуль | snake_case | `appointment_service.py` |
| URL path | kebab-case | `/api/v1/appointments/` |

## File Organization
```python
"""Module docstring."""
# 1. Standard library imports
import uuid
from datetime import datetime
from decimal import Decimal
# 2. Third-party imports
from django.db import models
from django.utils.translation import gettext_lazy as _
# 3. Local imports
from apps.core.models import BaseModel
# 4. Constants
MAX_RATING = 5
# 5. Classes
class Review(BaseModel):
    ...
```

## Serializers
```python
# ✅ Отдельные serializers для разных операций
class AppointmentListSerializer(serializers.ModelSerializer):
    """For list view — minimal fields."""
    class Meta:
        model = Appointment
        fields = ["id", "start_datetime", "status"]

class AppointmentDetailSerializer(serializers.ModelSerializer):
    """For detail view — all fields + nested."""
    specialist = SpecialistShortSerializer(read_only=True)
    service = ServiceSerializer(read_only=True)

    class Meta:
        model = Appointment
        fields = "__all__"

class AppointmentCreateSerializer(serializers.Serializer):
    """For creation — validation + custom logic."""
    specialist_id = serializers.UUIDField()
    service_id = serializers.UUIDField()
    start_datetime = serializers.DateTimeField()

    def validate_start_datetime(self, value):
        if value < timezone.now():
            raise serializers.ValidationError("Cannot book in the past")
        return value
```

## Views
```python
# ✅ ViewSets для CRUD
class AppointmentViewSet(viewsets.ModelViewSet):
    queryset = Appointment.objects.all()
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == "list":
            return AppointmentListSerializer
        if self.action == "create":
            return AppointmentCreateSerializer
        return AppointmentDetailSerializer

    def get_queryset(self):
        user = self.request.user
        if user.is_client:
            return self.queryset.filter(client=user)
        if user.is_specialist:
            return self.queryset.filter(specialist=user.specialist_profile)
        return self.queryset.none()

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Cancel appointment."""
        appointment = self.get_object()
        service = AppointmentService()
        service.cancel(appointment, cancelled_by=request.user)
        return Response({"status": "cancelled"})
```

## Services (Business Logic)
```python
# ✅ Бизнес-логика в services
class SlotCalculator:
    """Calculate available booking slots."""

    SLOT_INTERVAL_MINUTES = 30
    MIN_BOOKING_NOTICE_HOURS = 1

    def __init__(self, specialist: SpecialistProfile):
        self.specialist = specialist
        self._cache = caches["default"]

    def get_available_slots(self, date: date, service: Service) -> list[datetime]:
        """Get available slots for a specific date and service."""
        cache_key = f"slots:{self.specialist.pk}:{date}:{service.pk}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        slots = self._calculate_slots(date, service)
        self._cache.set(cache_key, slots, timeout=60)
        return slots

    def _calculate_slots(self, date: date, service: Service) -> list[datetime]:
        schedule = self._get_schedule(date)
        if not schedule:
            return []
        all_slots = self._generate_slots(
            date, schedule.start_time, schedule.end_time, service.duration,
        )
        available = self._filter_blocked(all_slots, date)
        available = self._filter_booked(available, service.duration)
        available = self._filter_past(available)
        return available
```

## Celery Tasks
```python
# ✅ Идемпотентные, с retry, с логированием
@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(ConnectionError, TimeoutError),
)
def send_appointment_reminder(self, appointment_id: str):
    """Send reminder notification for upcoming appointment."""
    logger.info(f"Sending reminder for appointment {appointment_id}")
    try:
        appointment = Appointment.objects.get(id=appointment_id)
    except Appointment.DoesNotExist:
        logger.warning(f"Appointment {appointment_id} not found, skipping")
        return
    if appointment.status != AppointmentStatus.CONFIRMED:
        logger.info(f"Appointment {appointment_id} not confirmed, skipping")
        return
    notification_service = NotificationService()
    notification_service.send(
        user=appointment.client,
        template_id="appointment_reminder_2h",
        context={
            "specialist_name": appointment.specialist.display_name,
            "service_name": appointment.service.name,
            "time": appointment.start_datetime.strftime("%H:%M"),
            "address": appointment.specialist.address,
        },
    )
    logger.info(f"Reminder sent for appointment {appointment_id}")
```
