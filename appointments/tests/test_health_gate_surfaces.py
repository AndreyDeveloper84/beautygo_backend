"""Как исход медицинского гейта выглядит НА ПОВЕРХНОСТЯХ.

Решение владельца (c) от 10.09.2026. Правило гейта закреплено отдельно
(``test_health_screening_gate.py``); здесь закрепляется то, что правило
делает с человеком на другом конце. Разделение не косметическое: гейт
списывался с бота, где срабатывание вызывает ``_handoff`` и передаёт
человека оператору, а первая редакция сторожа отдавала наружу
``BOOKING_ERROR`` — правило перенеслось, поведение нет.

Владелец назвал четыре проверяемых утверждения, и каждое здесь —
отдельный тест, а не пункт списка в чужом:

1. **свой машинный код, а не общий** — ``HEALTH_CHECK_REQUIRED`` и
   ``HEALTH_CHECK_UNKNOWN`` порознь. Наружу поверхность вправе показать
   одну спокойную фразу, внутрь коды обязаны расходиться: очередь
   разметки услуг строится на счётчике UNKNOWN, и слитый с REQUIRED он
   перестаёт говорить, сколько отказов вызвано нашими же данными;
2. **не изображать технической ошибкой** — у ответа есть
   ``details.handoff``, машинный признак «позвать человека», по которому
   поверхность отличает исход от своей ветки ошибок. Проверяется признак,
   а не строка текста: текст правят вёрсткой, признак — контрактом;
3. **не обещать, что запись создана** — в теле нет идентификатора записи
   и в базе нет строки;
4. **никогда 2xx.** Создание отвечает 201. Соблазн отдать 200 с телом
   «это не ошибка» ломается на неподнятом потребителе: клиент, считающий
   2xx успехом, покажет «вы записаны» на ответе без записи. При 4xx
   необновлённая поверхность деградирует в «ошибку» — тон неверный,
   ложного обещания нет. Из двух деградаций выбрана безопасная.

Поверхностей здесь три из четырёх: внутренний REST, админка салона и
AI-чат. Mini App создаёт через Ayla, но разбирает ответ у себя
(``apps/miniapp_api/views.py`` в ai-bot-platform кладёт любой 400 в
``bad_request``) — это срез B-1.3 в другом репозитории, и пока он не
сделан, Mini App показывает исход технической ошибкой.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from appointments.domain.exceptions import HealthScreeningRequiredError
from appointments.models import Appointment
from services.models import (
    SalonService,
    Service,
    ServiceCategory,
    ServiceTemplate,
    SpecialistService,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User

pytestmark = pytest.mark.django_db

SERVICE_TOKEN = "health-gate-surfaces-token"  # pragma: allowlist secret
BOT_EXTERNAL_ID = "bot:max:hcsurface"
INTERNAL_URL = "/api/v1/internal/appointments/"
SALON_URL = "/api/v1/tenants/me/appointments/"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = SERVICE_TOKEN


def _slot(hours: int = 30) -> datetime:
    """Будущее на 30-минутной сетке — сетка и окно не предмет этого файла."""
    base = datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    return base.replace(minute=0, second=0, microsecond=0)


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="hc-surface", name="HC Surface Salon")


@pytest.fixture
def master(salon):
    u = User.objects.create_user(
        username="hc_surface_master", password="x", role="specialist",
        phone="+79995500101",
    )
    u.tenant = salon
    u.save(update_fields=["tenant"])
    p = SpecialistProfile.objects.get(user=u)
    p.display_name = "Ольга"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.tenant = salon
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="HC Cat", slug="hc-surface-cat")


@pytest.fixture
def admin_user(db, salon):
    u = User.objects.create_user(
        username="bot:max:hcadmin", password="x", role="admin",
        phone="+79995500102",
    )
    TenantUserRelationship.objects.create(
        user=u, tenant=salon,
        role=TenantUserRelationship.Role.ADMIN, is_active=True,
    )
    return u


@pytest.fixture
def customer(db, salon):
    u = User.objects.create_user(
        username=BOT_EXTERNAL_ID, password="x", role="client",
        phone="+79995500103", first_name="Анна", is_proxy=True,
    )
    TenantUserRelationship.objects.create(
        user=u, tenant=salon,
        role=TenantUserRelationship.Role.CUSTOMER, is_active=True,
    )
    return u


def _edge(salon, master, category, *, template=None, salon_answer=None):
    """Ребро (услуга × мастер) с ЗАДАННЫМ положением по здоровью.

    Три опоры каскада разведены параметрами намеренно: тест, который
    задаёт исход через шаблон, и тест, который задаёт его ответом салона,
    проверяют разные ветки одного и того же вердикта.
    """
    service = SalonService.objects.create(
        tenant=salon, category=category, template=template,
        name="Услуга под гейтом", duration_minutes=60,
        requires_health_check=salon_answer,
    )
    SpecialistService.objects.create(
        salon_service=service, specialist=master,
        duration_minutes=60, price=Decimal("2000"), is_active=True,
    )
    return service


@pytest.fixture
def gated_template(category):
    return ServiceTemplate.objects.create(
        category=category, name="Гейтед шаблон", name_short="Гейт",
        duration_default=60, requires_health_check=True,
    )


@pytest.fixture
def open_template(category):
    return ServiceTemplate.objects.create(
        category=category, name="Открытый шаблон", name_short="Откр",
        duration_default=60, requires_health_check=False,
    )


def _internal_api() -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {SERVICE_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = BOT_EXTERNAL_ID
    return c


def _salon_api(user, salon) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {SERVICE_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = user.username
    c.defaults["HTTP_X_TENANT"] = salon.slug
    return c


def _internal_create(customer, master, service):
    return _internal_api().post(
        INTERNAL_URL,
        {
            "client_id": str(customer.id),
            "specialist_id": str(master.id),
            "service_id": str(service.id),
            "start_datetime": _slot().isoformat(),
        },
        format="json",
        HTTP_X_IDEMPOTENCY_KEY=str(uuid4()),
    )


def _salon_create(admin_user, salon, customer, master, service):
    return _salon_api(admin_user, salon).post(
        SALON_URL,
        {
            "client_id": str(customer.id),
            "specialist_id": str(master.id),
            "service_id": str(service.id),
            "start_datetime": _slot().isoformat(),
        },
        format="json",
        HTTP_X_IDEMPOTENCY_KEY=str(uuid4()),
    )


def _assert_handoff(response, expected_code: str) -> None:
    """Общая форма исхода — одна на все поверхности, поэтому одна функция.

    Разъехавшиеся поверхности — это разъехавшийся контракт, и увидеть это
    можно только сверяя их с одним образцом, а не каждую со своим.
    """
    assert response.status_code == 422, response.content

    body = response.json()
    assert "error" in body, body
    error = body["error"]

    # 1. Свой код, а не общий.
    assert error["code"] == expected_code
    assert error["code"] != "BOOKING_ERROR"

    # 2. Машинный признак «позвать человека».
    assert error["details"]["handoff"] is True
    assert error["details"]["reason"] == expected_code

    # 3. Текст один на все поверхности и живёт рядом с кодами.
    assert error["message"] == HealthScreeningRequiredError.HANDOFF_TEXT

    # 4. Обещания записи нет — ни в теле, ни в базе.
    assert "id" not in body
    assert "id" not in error
    assert Appointment.objects.count() == 0


class TestInternalRest:
    """Поверхность бота — ``appointments/internal_api.py``."""

    def test_unknown_travels_with_its_own_code(
        self, salon, master, category, customer,
    ):
        """Шаблона нет, салон не отвечал → UNKNOWN, не BOOKING_ERROR.

        Это положение 96 из 387 активных рёбер пилота на 10.09.2026 — и
        95 из 95 у самого пилотного салона.
        """
        service = _edge(salon, master, category)
        _assert_handoff(
            _internal_create(customer, master, service),
            HealthScreeningRequiredError.UNKNOWN,
        )

    def test_required_travels_with_a_different_code(
        self, salon, master, category, customer, gated_template,
    ):
        """Канон сказал «нужен скрининг» → REQUIRED.

        Отдельный код — не украшение: слитые в один счётчик, эти два
        положения перестают говорить, какая часть передач человеку
        вызвана противопоказанием, а какая нашим незнанием.
        """
        service = _edge(salon, master, category, template=gated_template)
        _assert_handoff(
            _internal_create(customer, master, service),
            HealthScreeningRequiredError.REQUIRED,
        )

    def test_a_known_no_still_books(
        self, salon, master, category, customer, open_template,
    ):
        """Положительная стража: гейт закрывает НЕ всё.

        Без неё оба теста выше зеленели бы и на коде, который отказывает
        всегда, — то есть на выключенной записи вместо работающего гейта.
        И ровно здесь видно последнее утверждение владельца с другой
        стороны: создание отвечает 201 и несёт идентификатор.
        """
        service = _edge(salon, master, category, template=open_template)

        response = _internal_create(customer, master, service)

        assert response.status_code == 201, response.content
        payload = response.json()
        assert payload["data"]["id"]
        assert Appointment.objects.count() == 1

    def test_the_salon_own_no_also_books(
        self, salon, master, category, customer,
    ):
        """Ответил не шаблон, а салон — вторая ветка того же «нет».

        После 0018 салон умеет отвечать «нет» отдельно от отсутствия
        ответа, и эта ветка проверяет, что гейт читает именно ответ, а не
        наличие шаблона.
        """
        service = _edge(salon, master, category, salon_answer=False)

        response = _internal_create(customer, master, service)

        assert response.status_code == 201, response.content
        assert Appointment.objects.count() == 1


class TestSalonAdmin:
    """Поверхность администратора салона — ``tenants/appointments_api.py``."""

    def test_unknown_is_a_handoff_not_an_error(
        self, salon, admin_user, master, category, customer,
    ):
        """Администратор у стойки получает тот же исход и тот же код.

        Поверхность другая, человек другой, контракт один: разъехавшись
        здесь, он разъедется молча — у админки своя ветка ошибок, и
        ``BOOKING_ERROR`` в ней выглядит как сбой сервера.
        """
        service = _edge(salon, master, category)
        _assert_handoff(
            _salon_create(admin_user, salon, customer, master, service),
            HealthScreeningRequiredError.UNKNOWN,
        )

    def test_a_known_no_still_books(
        self, salon, admin_user, master, category, customer, open_template,
    ):
        """Положительная стража для этой же поверхности."""
        service = _edge(salon, master, category, template=open_template)

        response = _salon_create(admin_user, salon, customer, master, service)

        assert response.status_code == 201, response.content
        assert Appointment.objects.count() == 1


class TestAiChat:
    """Четвёртая поверхность — ``ai/application/services/action_service.py``.

    Владелец назвал три, но чат — четвёртый вызывающий того же сторожа.
    Без своей ветки исход уезжал бы отсюда как ``BOOKING_FAILED``, то
    есть технической ошибкой, что запрещено прямым текстом.
    """

    def test_the_chat_hands_off_instead_of_reporting_a_failure(
        self, salon, master, category, customer,
    ):
        from ai.application.services.action_service import ActionService
        from ai.models import Conversation

        service = _edge(salon, master, category)
        conversation = Conversation.objects.create(user=customer)

        result = ActionService().execute(
            actor=customer,
            conversation=conversation,
            action_type="confirm_booking",
            confirmed=True,
            data={
                "specialist_id": str(master.id),
                "service_id": str(service.id),
                "datetime": _slot().isoformat(),
            },
            request_tenant_id=salon.id,
        )

        assert result.success is False
        assert result.error_code == HealthScreeningRequiredError.UNKNOWN
        assert result.error_code != "BOOKING_FAILED"
        assert result.error_details["handoff"] is True
        assert (
            result.error_details["message"]
            == HealthScreeningRequiredError.HANDOFF_TEXT
        )
        # Обещания записи нет и здесь.
        assert result.appointment_id is None
        assert Appointment.objects.count() == 0


class TestDeprecatedMarketplacePath:
    """Устаревший путь маркетплейса — своё имя отказа (§100).

    У ``services.Service`` нет колонки под медицинский признак, и добавлять
    её владелец запретил: маркетплейс поедет через DRF-1622, а старый путь
    закрыт до осознанной замены. Значит здесь не «никто не ответил», а
    «отвечать негде» — и это разные состояния для очереди разметки: за
    первым стоит работа, за вторым не стоит ничего.
    """

    @pytest.fixture
    def legacy_service(self, master, category):
        return Service.objects.create(
            specialist=master, category=category, name="Легаси-услуга",
            price=Decimal("2000.00"), duration_minutes=60, is_active=True,
            buffer_after_minutes=0,
        )

    def test_the_legacy_path_refuses_with_its_own_name(
        self, customer, master, legacy_service,
    ):
        """Не ``UNKNOWN``: очередь разметки этот слой видеть не должна."""
        response = _internal_create(customer, master, legacy_service)

        assert response.status_code == 422, response.content
        error = response.json()["error"]
        assert error["code"] == HealthScreeningRequiredError.NOT_APPLICABLE
        assert error["code"] != HealthScreeningRequiredError.UNKNOWN
        assert Appointment.objects.count() == 0

    def test_it_does_not_promise_a_consultation_nobody_will_hold(
        self, customer, master, legacy_service,
    ):
        """Оговорка владельца §100, и она проверяемая, а не про тон.

        Консультацию по этому исходу никто не назначит — назначать её
        некому. Обещать её значило бы отправить человека искать дверь,
        которой нет. Поэтому ``handoff`` здесь ``false``, и текст другой:
        поверхность ведёт себя по признаку, а не по строке.
        """
        response = _internal_create(customer, master, legacy_service)
        error = response.json()["error"]

        assert error["details"]["handoff"] is False
        assert error["message"] == (
            HealthScreeningRequiredError.NOT_APPLICABLE_TEXT
        )
        assert error["message"] != HealthScreeningRequiredError.HANDOFF_TEXT

    def test_the_canonical_layer_still_promises_one(
        self, salon, master, category, customer,
    ):
        """Положительная стража к предыдущему.

        Без неё тест выше зеленел бы и на коде, который снял обещание
        консультации ВЕЗДЕ, — то есть на сломанной передаче человеку
        вместо точного различения двух состояний.
        """
        service = _edge(salon, master, category)
        error = _internal_create(customer, master, service).json()["error"]

        assert error["details"]["handoff"] is True
        assert error["message"] == HealthScreeningRequiredError.HANDOFF_TEXT
