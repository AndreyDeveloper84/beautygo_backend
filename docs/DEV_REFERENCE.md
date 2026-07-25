# Dev Reference (Ayla backend)

> Вынесено из `CLAUDE.md` (chore/slim-claude-md). Здесь — детальный справочник по подсистемам: структура репо, диаграммы, code-примеры сервисов. `CLAUDE.md` держит только правила/гейтчи + указатели сюда. Ничего из старого `CLAUDE.md` не потеряно — оно здесь, в `docs/coding-standards.md` и `docs/testing.md`.

## Оглавление
- [Project Structure](#project-structure)
- [Mobile Structure](#mobile-structure)
- [Architecture Diagrams](#architecture-diagrams)
- [Database Schema & Enums](#database-schema--enums)
- [AI Assistant Integration (ClaudeService)](#ai-assistant-integration)
- [Personalization Engine (полная версия)](#personalization-engine)
- [Payments Flow & Webhook](#payments-flow--webhook)
- [Notifications](#notifications)
- [Booking Engine (DDD слои)](#booking-engine-ddd)
- [Common Tasks](#common-tasks)
- [External Documentation](#external-documentation)

---

## Project Structure
```
djangoProject/                   # Git root
├── CLAUDE.md
├── manage.py
├── requirements.txt             # Single requirements file
├── Dockerfile / docker-compose.yml
├── .env.example
├── .flake8                      # max-line-length=120
├── pytest.ini
│
├── djangoProject/               # Django project settings
│   ├── settings/
│   │   ├── base.py              # Общие настройки
│   │   ├── dev.py               # DEBUG=True, SQLite
│   │   └── prod.py              # PostgreSQL, Sentry
│   ├── urls.py                  # Root URL routing
│   └── wsgi.py
│
├── users/                       # Auth, Users, Specialists, Social Auth
│   ├── models.py                # User, Profile, SpecialistProfile, OTPCode,
│   │                            # DeviceToken, SocialAccount, AnonymousSession
│   ├── views.py                 # Auth views (OTP, Anonymous, Onboarding, Logout)
│   ├── social_auth.py           # VK/Google/Apple/Yandex OAuth
│   ├── specialists_api.py       # GET /specialists/ (public catalog for client)
│   ├── schedule_api.py          # Working Hours & TimeOff CRUD (specialist)
│   ├── services.py              # AuthService, OTPService, SMSService
│   ├── serializers.py
│   ├── permissions.py           # IsClient, IsSpecialist, IsClientApp, IsProApp
│   ├── response.py              # success_response(), error_response()
│   ├── middleware.py            # AppTypeMiddleware, JWTContextMiddleware
│   ├── admin.py
│   └── tests/
│
├── services/                    # Service & Category models
├── appointments/                # Booking Engine (DDD) — см. раздел Booking Engine
├── reviews/                     # Reviews & Ratings
├── payments/                    # YooKassa Payment Integration
├── search/                      # Global search
├── scripts/                     # Utility scripts (not tracked in CI)
└── .github/workflows/ci.yml    # flake8 → pytest → SSH deploy
```

## Mobile Structure
> React Native проекты в отдельном репо `beautygo-mobile` (rename → `ayla-mobile` pending).

```
beautygo-mobile/
├── packages/
│   └── shared/                  # @beautygo/shared (→ @ayla/shared pending)
│       └── src/
│           ├── api/             # API Client (client.ts + X-App-Type header), models
│           ├── auth/            # Auth service
│           ├── storage/         # Secure storage
│           └── components/      # Base UI components
├── apps/
│   ├── client/                  # 🟢 Ayla — Bundle ru.ayla.client
│   │   └── src/screens/{AIChat, Search, Booking, Profile}
│   └── pro/                     # 🟣 Ayla Pro — Bundle ru.ayla.pro
│       └── src/screens/{Dashboard, Schedule, Services, Clients, Analytics}
└── package.json                 # Yarn workspaces root
```

### API Client с X-App-Type
```typescript
// packages/shared/src/api/client.ts
import axios, { AxiosInstance } from 'axios';
export type AppType = 'client' | 'pro';
export const createApiClient = (appType: AppType): AxiosInstance => {
  const client = axios.create({ baseURL: process.env.API_URL });
  client.interceptors.request.use((config) => {
    config.headers['X-App-Type'] = appType;           // 🔑 обязательный header
    config.headers['Authorization'] = `Bearer ${getToken()}`;
    return config;
  });
  return client;
};
```

## Architecture Diagrams

### High-Level
```
🟢 Ayla (Client, RN)      🟣 Ayla Pro (RN)
        │  X-App-Type: client    │  X-App-Type: pro
        └────────────┬───────────┘
                     ▼
            @beautygo/shared
                     ▼
   Django API (DRF) ──▶ PostgreSQL
        │
   ┌────┼─────────┐
   ▼    ▼         ▼
 Redis Celery  Claude AI
```

### Компоненты
| Компонент | Технология | Назначение |
|-----------|------------|------------|
| API | Django 5.0 + DRF | REST API |
| Database | PostgreSQL 16 | Основное хранилище |
| Cache | Redis 7 | Кеш слотов, сессии |
| Queue | Celery + Redis | Фоновые задачи |
| AI | Claude Sonnet 4 | Чат-бот, рекомендации |
| Payments | YooKassa | Приём платежей |
| SMS | SMS.RU | OTP, уведомления |
| Push | Firebase FCM | Push-уведомления |
| Maps | 2GIS | Геолокация, адреса |

## Database Schema & Enums

### Key Relationships
- `User` → `Profile` (OneToOne, role=client)
- `User` → `SpecialistProfile` (OneToOne, role=specialist)
- `SpecialistProfile` → `Service` (OneToMany)
- `SpecialistProfile` → `SpecialistWorkingHours` (OneToMany, 7 days)
- `SpecialistProfile` → `SpecialistTimeOff` (OneToMany)
- `Appointment` → `User` (client), `SpecialistProfile`, `Service`
- `Appointment` → `Payment` (OneToMany)
- `Review` → `Appointment`, `User`, `SpecialistProfile`

### Enums & Choices
```python
class Role(TextChoices):
    CLIENT = "client"
    SPECIALIST = "specialist"
    ADMIN = "admin"

class AppType(TextChoices):
    CLIENT = "client"       # 🟢 Ayla
    PRO = "pro"             # 🟣 Ayla Pro

class AppointmentStatus(TextChoices):
    PENDING = "pending"
    AWAITING_PAYMENT = "awaiting_payment"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"

class PaymentStatus(TextChoices):
    PENDING = "pending"
    AUTHORIZED = "authorized"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"

class DevicePlatform(TextChoices):
    IOS = "ios"
    ANDROID = "android"
```

### Request/Response Format
Успех:
```json
{ "data": { }, "meta": { "page": 1, "per_page": 20, "total": 100 } }
```
Ошибка:
```json
{ "error": { "code": "VALIDATION_ERROR", "message": "Invalid input",
  "details": { "phone": ["This field is required"] } } }
```

### Authentication
- JWT (Access + Refresh) via `simplejwt`. Access 15 мин, Refresh 90 дней.
- Anonymous JWT: `/auth/anonymous` → `{ access_token, is_anonymous: true }`
- OTP verify: `/auth/verify-otp` → `{ access_token, refresh_token, expires_in, is_new_user, onboarding_completed, user }`
- Header: `Authorization: Bearer <token>`

### Полная URL-карта
```
/api/v1/
├── auth/       register, login, verify-otp, token/refresh, logout, social/{provider}
├── users/      me (GET/PATCH/DELETE), me/appointments
├── specialists/ list, {id}, {id}/services, {id}/slots, {id}/reviews, {id}/favorite
├── services/   categories, search
├── appointments/ create, {id}, {id}/cancel, {id}/reschedule, {id}/complete
├── reviews/    create, {id} (PATCH), {id}/reply
├── payments/   create, {id}, webhook, {id}/refund
├── search/
├── notifications/  🔜 not yet
├── ai/             🔜 not yet
└── health/     /, /ready
```

## AI Assistant Integration
> Реализация/план: `docs/AI_CHAT_PLAN.md`. Shared-оркестрация: `ayla-ai-core`.

```python
# apps/ai_assistant/services.py
from anthropic import Anthropic
from django.conf import settings

class ClaudeService:
    """Service for interacting with Claude AI."""

    def __init__(self):
        self.client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self.model = settings.CLAUDE_MODEL

    async def chat(self, conversation, user_message: str) -> tuple[str, dict | None]:
        """Returns (response_text, action_data). action_data = structured UI data."""
        messages = self._build_messages(conversation, user_message)
        specialist_context = await self._get_specialist_context(conversation)
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=settings.CLAUDE_MAX_TOKENS,
            system=self._get_system_prompt(specialist_context),
            messages=messages,
            tools=self._get_tools(),
        )
        return self._parse_response(response)
```

Tools, которые Claude может вызывать: `show_specialists`, `show_slots`, `confirm_booking`.
System-prompt строится с контекстом (город, предпочтения, история, доступные мастера);
правила: дружелюбно, уточняющие вопросы, конкретные рекомендации, русский язык, кратко.
Промпты — в `apps/ai_assistant/prompts.py`.

## Personalization Engine
> Load-bearing для North Star «AI который помнит. Всегда.». Контракт: `docs/PERSONAL_CONTEXT_INTERNAL_API_CONTRACT.md`. Статус: pilot-память живёт в БОТЕ, Ayla = declared green prefs (memory pivot 2026-07-09).

### Три источника данных
| Источник | Метод | Доля |
|----------|-------|------|
| Явные вопросы | AI задаёт органично, 1 вопрос/сессия, cooldown 24ч | ~30% |
| Поведенческие паттерны | Celery-task раз в сутки анализирует историю | ~50% |
| Контекстуальные сигналы | Claude structured-extraction из чата | ~20% |

### Три уровня деликатности
- 🟢 Зелёная — упоминать открыто (адрес работы, бюджет, любимый мастер, диета)
- 🟡 Жёлтая — использовать молча (наличие детей, занятость, паттерны партнёра)
- 🔴 Красная — только локально, retention 90 дней (беременность, хронические заболевания)

### 8 anti-spam правил
1. Одно поле за сессию; 2. Не на первом взаимодействии (со 2–3 запроса);
3. Cooldown 24ч; 4. Skip 2 раза → пауза 30 дней; 5. Данные есть → молчать;
6. Органично или никак; 7. Объяснять зачем; 8. Skip без наказания.

### Метрики
| Метрика | Цель |
|---------|------|
| Context fill rate (полей за 1-й месяц) | ≥ 5 |
| Question answer rate | ≥ 60% |
| Context usage rate | ≥ 60% |
| Skip rate | ≤ 30% |
| Booking conversion lift при контексте | +15% |

### API endpoints (М3+)
```
GET    /api/v1/users/me/personal-context/
PATCH  /api/v1/users/me/personal-context/
DELETE /api/v1/users/me/personal-context/{field}/
POST   /api/v1/users/me/personal-context/skip/
DELETE /api/v1/users/me/personal-context/         # 152-ФЗ total wipe
```

### 152-ФЗ
Шифрование at-rest; красная зона не в GET по умолчанию; отдельные логи доступа к красной зоне; «Забудь мой адрес работы» + кнопка = удаление поля; «Очистить историю Ayla» = total wipe.

## Payments Flow & Webhook
> Реализовано. Status mapping и env — в `CLAUDE.md` (критичные гейтчи).

Flow: `POST /payments/` → create Payment (pending) → YooKassa payment → confirmation_url →
клиент платит → webhook → update Payment (succeeded/cancelled) → update Appointment → notifications.

Two-stage: hold → capture (при `appointment.completed`). Idempotency: webhook de-dup через
`last_webhook_event_id` + `X-Request-Id`; повторный POST /create возвращает существующий pending.
Split-платёж если `YOOKASSA_AGENT_ID` задан.

```python
# apps/payments/views.py
class YooKassaWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        if not self._verify_signature(request):
            return Response(status=403)
        event = request.data.get("event")
        payment_data = request.data.get("object")
        if event == "payment.succeeded":
            self._handle_success(payment_data)
        elif event == "payment.canceled":
            self._handle_cancelled(payment_data)
        elif event == "refund.succeeded":
            self._handle_refund(payment_data)
        return Response(status=200)
```

## Notifications
> 🔜 Not implemented (Firebase FCM, SMS reminders). Deep links: `ayla-client://` / `ayla-pro://`.

```python
# apps/notifications/templates.py
DEEP_LINKS = {"client": "ayla-client://", "pro": "ayla-pro://"}

TEMPLATES = {
    "appointment_created_client": NotificationTemplate(
        id="appointment_created_client",
        app_type="client",  # 🟢 только Ayla
        channel=Channel.BOTH,
        priority=Priority.HIGH,
        push_title="✅ Запись подтверждена!",
        push_body="{{service_name}} у {{specialist_name}}, {{date_time}}",
        sms_text="Ayla: Вы записаны на {{service_name}} {{date_time}}. {{address}}",
        deep_link="ayla-client://appointment/{{appointment_id}}",
    ),
    "appointment_created_specialist": NotificationTemplate(
        id="appointment_created_specialist",
        app_type="pro",     # 🟣 только Ayla Pro
        channel=Channel.BOTH,
        priority=Priority.HIGH,
        push_title="📅 Новая запись!",
        push_body="{{client_name}} на {{service_name}}, {{date_time}}",
        sms_text="Ayla Pro: Новая запись на {{date_time}}",
        deep_link="ayla-pro://appointment/{{appointment_id}}",
    ),
}
```
Отправка: `NotificationService().send(user=..., template_id=..., context={...})`.

## Booking Engine (DDD)
> Каноническая спецификация жизненного цикла: `docs/04 Domain Models/Booking Lifecycle Specification.md`. Dual-mode: `docs/architecture/booking-source-dual-mode.md`.

### Слои
```
appointments/
├── domain/                  # Чистый Python, без Django
│   ├── value_objects.py     # TimeInterval, BookingStatus, BookingStateMachine, BookingSnapshot
│   ├── exceptions.py        # BookingDomainError → 7 типов
│   └── policies.py          # CommissionPolicy(8%), CancellationPolicy, ReschedulePolicy, BookingWindowPolicy
├── application/
│   ├── dto.py               # Input/Output DTOs (все ID — UUID)
│   └── services/
│       ├── create_booking_service.py    # атомарное создание + idempotency + row locking
│       ├── cancel_reschedule_service.py # отмена/перенос через state machine
│       └── availability_query_service.py # расчёт слотов + cache-aside
├── infrastructure/
│   ├── availability/{providers.py, slot_builder.py}
│   ├── cache/slot_cache.py  # LocMemCache для MVP
│   └── outbox_worker.py     # заглушка для MVP
└── models.py                # Appointment + WorkingHours + TimeOff + Payment + OutboxEvent
```

### Ключевые паттерны
| Паттерн | Где | Зачем |
|---------|-----|-------|
| State Machine | `BookingStateMachine` | явные переходы статусов |
| Idempotency Key | `Appointment.idempotency_key` + `X-Idempotency-Key` | защита от дубликатов при ретраях |
| Snapshot | `Appointment.snapshot_*` | неизменяемая запись финансов на момент брони |
| Strategy/Policy | `domain/policies.py` | подменяемые бизнес-правила |
| Transactional Outbox | `OutboxEvent` | гарантированная доставка событий |
| Row-Level Locking | `select_for_update()` | блокировка записей конкретного мастера |
| Thin Views | `appointments/views.py` | вся логика в application services |

## Common Tasks

### Новый endpoint
1. `models.py` → 2. `make makemigrations` → 3. `make migrate` → 4. `serializers.py` →
5. `views.py` (ViewSet) → 6. URL в `urls.py` → 7. тесты → 8. `make test-app APP=<app>`.

### Celery task
1. task в `tasks.py`; 2. периодический → `config/celery.py` beat_schedule; 3. `make restart`.

### Notification template
1. template в `apps/notifications/templates.py`; 2. event в `events.py`; 3. аналитика в точке отправки.

### Debug
```bash
make logs        # все логи
make logs-api    # только API
make shell       # контейнер
make django-shell  # Django shell (IPython)
make psql        # PostgreSQL shell
```

## External Documentation
- [Django 5.0](https://docs.djangoproject.com/en/5.0/) · [DRF](https://www.django-rest-framework.org/) · [Celery](https://docs.celeryq.dev/)
- [Anthropic Claude](https://docs.anthropic.com/) · [YooKassa](https://yookassa.ru/developers/api) · [SMS.RU](https://sms.ru/api) · [Firebase FCM](https://firebase.google.com/docs/cloud-messaging)
