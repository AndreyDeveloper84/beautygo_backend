"""Медицинский гейт на пути СОЗДАНИЯ записи.

Решение владельца §89: гейт стоит на записи, а не на витрине. До этого
сторожа единственным местом, где он исполнялся, был диалоговый скилл бота,
и его собственный докстринг это признавал дословно:

    «It is the conversational channel's routing policy, not a system-wide
    safety interlock.»

Здесь он становится системным: путь создания один
(``Appointment.objects.create`` встречается в репозитории ровно раз), и все
вызывающие проходят через него.

Два отказа носят РАЗНЫЕ имена, и это не украшение: человек, которому
отказали из-за противопоказания, и человек, про услугу которого никто
ничего не сказал, находятся в разном положении. Первому нужен скрининг,
второму — ответ салона.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.exceptions import HealthScreeningRequiredError
from services.models import (
    SalonService,
    ServiceCategory,
    ServiceTemplate,
    SpecialistService,
)

pytestmark = pytest.mark.django_db


def _start_at() -> datetime:
    """Далёкое будущее на сетке — чтобы окно и сетка не мешали предмету."""
    base = datetime.now(timezone.utc) + timedelta(days=14)
    return base.replace(hour=10, minute=0, second=0, microsecond=0)


@pytest.fixture
def salon_category(db):
    return ServiceCategory.objects.create(name="Гейт-категория")


def _salon_service(specialist, category, **over):
    fields = dict(
        tenant=specialist.tenant,
        category=category,
        name="Услуга под гейтом",
        duration_minutes=60,
    )
    fields.update(over)
    return SalonService.objects.create(**fields)


def _bookable(specialist, salon, **over):
    fields = dict(
        salon_service=salon,
        specialist=specialist,
        duration_minutes=60,
        price=Decimal("2000"),
    )
    fields.update(over)
    return SpecialistService.objects.create(**fields)


def _dto(client_user, specialist, service_id, **over):
    fields = dict(
        client_id=client_user.id,
        specialist_id=specialist.id,
        service_id=service_id,
        start_at=_start_at(),
        idempotency_key=str(uuid4()),
        payment_required=False,
        confirm_immediately=True,
    )
    fields.update(over)
    return CreateBookingDTO(**fields)


def test_unknown_health_status_refuses_with_its_own_name(
    client_user, specialist, salon_category,
):
    """Шаблона нет, флаг никто не поднимал → отказ с именем UNKNOWN.

    Это состояние 95 из 95 записываемых рёбер пилотного салона на
    09.09.2026. До правки они бронировались без единого вопроса о
    здоровье, потому что каскад отвечал `False` за салон.
    """
    salon = _salon_service(specialist, salon_category, template=None)
    _bookable(specialist, salon)

    with pytest.raises(HealthScreeningRequiredError) as exc:
        CreateBookingService().execute(_dto(client_user, specialist, salon.id))

    assert exc.value.reason == HealthScreeningRequiredError.UNKNOWN


def test_required_health_status_refuses_with_a_different_name(
    client_user, specialist, salon_category,
):
    """Каталог сказал «нужен скрининг» → отказ с именем REQUIRED.

    Отдельное имя — не косметика: сложить два отказа в один счётчик
    значит потерять единственное число, которое говорит, какая часть
    отказов вызвана нашими же недостающими данными.
    """
    gated = ServiceTemplate.objects.create(
        category=salon_category, name="Гейтед шаблон", name_short="Гейт",
        duration_default=60, requires_health_check=True,
    )
    salon = _salon_service(specialist, salon_category, template=gated)
    _bookable(specialist, salon)

    with pytest.raises(HealthScreeningRequiredError) as exc:
        CreateBookingService().execute(_dto(client_user, specialist, salon.id))

    assert exc.value.reason == HealthScreeningRequiredError.REQUIRED


def test_a_known_no_lets_the_booking_through(
    client_user, specialist, salon_category,
):
    """Положительная стража: гейт закрывает НЕ всё.

    Без неё оба теста выше зеленели бы и на коде, который отказывает
    всегда, — то есть на выключенной броне вместо работающего гейта.
    """
    open_template = ServiceTemplate.objects.create(
        category=salon_category, name="Открытый шаблон", name_short="Откр",
        duration_default=60, requires_health_check=False,
    )
    salon = _salon_service(specialist, salon_category, template=open_template)
    _bookable(specialist, salon)

    result = CreateBookingService().execute(
        _dto(client_user, specialist, salon.id)
    )

    assert result is not None


def test_the_time_override_does_not_lift_the_health_gate(
    client_user, specialist, salon_category,
):
    """Административный override снимает ВРЕМЕННЫЕ правила — и только их.

    DRF-1545 удалил единственный механизм, которым этот гейт открывался
    по площадке: «требование расспросить человека принадлежит процедуре,
    а не площадке». Если бы override его снимал, дыра вернулась бы под
    другим именем и без строки в реестре решений — поэтому сторож стоит
    ВНЕ ветки override, и вот доказательство.
    """
    salon = _salon_service(specialist, salon_category, template=None)
    _bookable(specialist, salon)

    with pytest.raises(HealthScreeningRequiredError) as exc:
        CreateBookingService().execute(
            _dto(
                client_user, specialist, salon.id,
                actor_role="salon",
                time_override=True,
                time_override_reason="клиент стоит на ресепшене",
            )
        )

    assert exc.value.reason == HealthScreeningRequiredError.UNKNOWN
