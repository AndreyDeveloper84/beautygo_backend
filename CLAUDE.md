# CLAUDE.md — Ayla Project Intelligence
> **Этот файл — источник истины для AI-ассистентов, работающих с проектом Ayla.**
> Прочитай его полностью перед выполнением любых задач.
---
## 📋 QUICK REFERENCE
```
Проект:     Ayla — AI Life Assistant (beauty as entry, daily-hook через food + memory)
Позиционир: "AI, который помнит. Всегда." (long-term personal memory = главное преимущество)
Архитектура: Two Apps (Ayla 🟢 + Ayla Pro 🟣)
Пилот:      Пенза → Казахстан (Phase 5)
Mobile:     React Native + Shared Package (@beautygo/shared, переименование pending)
Backend:    Python 3.12+ / Django 5.0 + DRF
Database:   PostgreSQL 16
Cache:      Redis 7
AI:         Claude Sonnet 4 (Anthropic) + OpenAI GPT-4 Vision (food scanning)
Запуск:     make up (Docker) или make init (первый раз)
Тесты:      make test
Lint:       make lint
```

> ⚠️ **Ребрендинг 2026-03-30:** BeautyGO → Ayla. Бренд в PRD/Notion = Ayla; код, NPM-пакет (`@beautygo/shared`) и mobile-репо (`beautygo-mobile`) — миграция pending. См. секцию **Brand Migration Status** ниже.
### 🔑 Key Headers
```http
X-App-Type: client   # 🟢 Ayla requests
X-App-Type: pro      # 🟣 Ayla Pro requests
```
---
## 🎯 PROJECT OVERVIEW
### Что это?
**Ayla** — AI-ассистент качества жизни для женщин 20–45, где beauty-запись является точкой входа и основной монетизацией, а **ежедневный retention строится на AI-фичах заботы о себе**: трекинге питания (Food Scanner), анализе внешности (AI-аватар), персональных рекомендациях.

**Ключевая проблема:** 47% клиентов откладывают запись к мастеру из-за «паралича выбора»; одновременно booking-приложения открывают раз в месяц, что не даёт построить ежедневную ценность.

**Решение:** AI-ассистент с **долгосрочной личной памятью** (UserPersonalContext). Каждый следующий разговор умнее предыдущего — пользователь не объясняет дважды. Beauty — первая вертикаль; далее health → fitness → nutrition.

### 🌟 Vision & Killer Scenario
**Vision:** «AI, который помнит. Всегда.»
**Promise:** Стань лучшей версией себя — Ayla помнит тебя и помогает каждый день.

**Killer Scenario v3.0:**
> Утром пользователь фотографирует завтрак → Ayla помнит, что на прошлой неделе был дефицит витамина D → рекомендует массаж с аргановым маслом у Анны (уже знает, что она любимый мастер) → записывается в 1 тап. Вечером обновляет аватар и видит: за месяц кожа стала ровнее. Делится прогрессом в Telegram.

**Long-term Vision:** персональный AI-консьерж качества жизни для женщин СНГ. Beauty — первая вертикаль, доказывающая модель.

### 📍 Pilot & Roadmap (по PRD v3.0)
- **Phase 0 (апрель 2026):** Booking flow в Пензе. 20–50 первых пользователей, 15–20 мастеров.
- **Phase 1 (апрель 2026):** AI Food Scanner. Целевая метрика — ежедневный hook.
- **Phase 2 (май 2026):** AI-аватар + рекомендации. **Pre-deferred** до валидации (см. `docs/HYPOTHESIS_VALIDATION_PLAN_2026-04.md`).
- **Phase 3 (май 2026):** Ayla Pro полнофункциональный + геолокация + реферал.
- **Phase 4 (июнь 2026):** монетизация (Premium ₽299/мес + 8% commission).
- **Phase 5 (июль–август 2026):** Казахстан + Kaspi Pay + investor meeting.
### 📱 Two Apps Architecture
> **⚠️ ВАЖНО**: Проект состоит из ДВУХ отдельных мобильных приложений!
| Приложение | Bundle ID | Аудитория | X-App-Type |
|------------|-----------|-----------|------------|
| 🟢 **Ayla** | `ru.ayla.client` | Клиенты | `client` |
| 🟣 **Ayla Pro** | `ru.ayla.pro` | Мастера | `pro` |
**Почему два приложения:**
- Разные user journeys (поиск vs управление)
- Разный UX (AI-first vs dashboard)
- Независимые релизы и итерации
- Лучшие рейтинги в сторах
**Shared Package** (`@beautygo/shared`):
- API Client + модели (TypeScript)
- Аутентификация
- Базовые UI компоненты
- Локализация (i18n)
### Пользователи
| Роль | Приложение | Описание |
|------|------------|----------|
| **Client** | 🟢 Ayla | Ищет и бронирует услуги через AI |
| **Specialist** | 🟣 Ayla Pro | Управляет расписанием, услугами, клиентами |
| **Admin** | Web Dashboard | Администратор системы |
### Ключевые фичи
**MVP (Phase 0–1):**
1. **AI Chat** — диалог с Claude для подбора мастера + intent parsing
2. **Smart Booking** — бронирование через чат или вручную
3. **Slot Management** — автоматический расчёт доступных слотов
4. **Payments** — онлайн-оплата через YooKassa (8% комиссия)
5. **Notifications** — Push (Firebase) + SMS (SMS.RU)
6. **Reviews** — отзывы и рейтинги
7. **AI Food Scanner** — фото еды → анализ + рекомендации (daily-hook)

**Phase 2+ (после валидации гипотез):**
8. **AI-аватар** — анализ внешности + прогресс-таймлайн (deferred до Test 2 results)
9. **UserPersonalContext (Memory)** — долгосрочная личная память; **load-bearing для North Star**
10. **Геолокация + Яндекс.Маршруты** — «успеешь ли?» + ежедневник
11. **Реферальная программа** — ₽300×2 за приглашение

### 5-tab Bottom Navigation (Ayla Client App, по PRD v3.0)
| # | Таб | Содержание | Частота открытия |
|---|-----|------------|-------------------|
| 1 | 🏠 **Главная** | Каталог мастеров + AI-поиск | При каждом запуске |
| 2 | 🍽️ **Питание** | Food Scanner + дневник (FAB-кнопка в центре) | 3× в день |
| 3 | ✨ **Я** | AI-аватар + рекомендации + прогресс | Несколько раз в неделю |
| 4 | 📅 **День** | Записи + сводка питания + время в пути | Каждое утро |
| 5 | 👤 **Профиль** | Настройки, рефералы, избранные мастера | Редко |

> Табы отсортированы по убыванию частоты слева направо. Таб **«День»** заменяет пассивный список «Записей»: ежедневник, а не reminder раз в месяц.
---
## 🏗️ ARCHITECTURE
### High-Level
```
┌─────────────────┐     ┌─────────────────┐
│  🟢 Ayla    │     │  🟣 Ayla    │
│  (Client App)   │     │     Pro         │
│  React Native   │     │  React Native   │
└────────┬────────┘     └────────┬────────┘
         │                       │
         │  X-App-Type: client   │  X-App-Type: pro
         │                       │
         └───────────┬───────────┘
                     ▼
         ┌───────────────────────┐
         │   @beautygo/shared    │
         │   (Shared Package)    │
         └───────────┬───────────┘
                     ▼
         ┌───────────────────────┐     ┌─────────────────┐
         │     Django API        │────▶│   PostgreSQL    │
         │       (DRF)           │     │                 │
         └───────────┬───────────┘     └─────────────────┘
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
  ┌──────────┐ ┌──────────┐ ┌──────────┐
  │  Redis   │ │  Celery  │ │  Claude  │
  │  Cache   │ │  Workers │ │  AI API  │
  └──────────┘ └──────────┘ └──────────┘
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
---
## 📁 PROJECT STRUCTURE (Actual)
```
djangoProject/                   # Git root
├── CLAUDE.md                    # ← ТЫ ЗДЕСЬ
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
│   ├── middleware.py             # AppTypeMiddleware, JWTContextMiddleware
│   ├── admin.py
│   └── tests/
│       ├── test_views.py        # Auth + profile tests
│       ├── test_auth_v2.py      # Anonymous JWT + onboarding
│       ├── test_social_auth.py  # OAuth tests
│       ├── test_specialists_api.py  # Catalog tests
│       ├── test_schedule_api.py # Working hours + time-off
│       └── test_services.py     # Unit tests for services
│
├── services/                    # Service & Category models
│   ├── models.py                # Service, ServiceCategory
│   ├── views.py                 # Services CRUD (specialist), list (client)
│   └── tests/
│
├── appointments/                # Booking Engine (DDD)
│   ├── models.py                # Appointment, Payment, SpecialistWorkingHours,
│   │                            # SpecialistTimeOff, OutboxEvent
│   ├── views.py                 # Thin views → application services
│   ├── serializers.py
│   ├── domain/                  # Pure Python (no Django)
│   │   ├── value_objects.py     # TimeInterval, BookingStateMachine
│   │   ├── exceptions.py        # BookingDomainError hierarchy
│   │   └── policies.py          # Commission, Cancellation, Reschedule
│   ├── application/             # Use-cases + DTOs
│   │   ├── dto.py
│   │   └── services/            # CreateBooking, CancelReschedule, Availability
│   ├── infrastructure/          # Slot builder, cache, outbox
│   └── tests/
│
├── reviews/                     # Reviews & Ratings
│   ├── models.py                # Review (OneToOne → Appointment)
│   ├── views.py                 # Create, Update, Reply, List
│   ├── serializers.py
│   └── tests/
│
├── payments/                    # YooKassa Payment Integration
│   ├── services.py              # YooKassaService (create, capture, refund)
│   ├── views.py                 # Create, Detail, Webhook, Refund
│   ├── serializers.py           # Spec-aligned (PaymentStatus mapping)
│   └── tests/
│
├── search/                      # Global search
│   └── ...
│
├── scripts/                     # Utility scripts (not tracked in CI)
└── .github/workflows/ci.yml    # flake8 → pytest → SSH deploy
```
---
## 📱 MOBILE STRUCTURE
> React Native проекты в отдельном репозитории `beautygo-mobile`
```
beautygo-mobile/
├── packages/
│   └── shared/                  # 🔄 @beautygo/shared
│       ├── src/
│       │   ├── api/             # API Client, models
│       │   │   ├── client.ts    # Axios + X-App-Type header
│       │   │   └── models/      # Shared TypeScript types
│       │   ├── auth/            # Auth service
│       │   ├── storage/         # Secure storage
│       │   └── components/      # Base UI components
│       ├── package.json
│       └── tsconfig.json
│
├── apps/
│   ├── client/                  # 🟢 Ayla (Client App)
│   │   ├── src/
│   │   │   ├── screens/
│   │   │   │   ├── AIChat/      # AI Assistant UI
│   │   │   │   ├── Search/      # Search & Discovery
│   │   │   │   ├── Booking/     # Booking flow
│   │   │   │   └── Profile/     # Client profile
│   │   │   ├── navigation/
│   │   │   └── App.tsx
│   │   ├── ios/                 # Bundle: ru.ayla.client
│   │   ├── android/             # Package: ru.ayla.client
│   │   └── package.json
│   │
│   └── pro/                     # 🟣 Ayla Pro (Specialist App)
│       ├── src/
│       │   ├── screens/
│       │   │   ├── Dashboard/   # Home dashboard
│       │   │   ├── Schedule/    # Calendar & slots
│       │   │   ├── Services/    # Services CRUD
│       │   │   ├── Clients/     # Client management
│       │   │   └── Analytics/   # Stats & reports
│       │   ├── navigation/
│       │   └── App.tsx
│       ├── ios/                 # Bundle: ru.ayla.pro
│       ├── android/             # Package: ru.ayla.pro
│       └── package.json
│
├── package.json                 # Yarn workspaces root
└── yarn.lock
```
### API Client с X-App-Type
```typescript
// packages/shared/src/api/client.ts
import axios, { AxiosInstance } from 'axios';
export type AppType = 'client' | 'pro';
export const createApiClient = (appType: AppType): AxiosInstance => {
  const client = axios.create({
    baseURL: process.env.API_URL,
  });
  client.interceptors.request.use((config) => {
    // 🔑 Обязательный header для всех запросов
    config.headers['X-App-Type'] = appType;
    config.headers['Authorization'] = `Bearer ${getToken()}`;
    return config;
  });
  return client;
};
// Использование в Ayla (Client)
const api = createApiClient('client');
// Использование в Ayla Pro
const api = createApiClient('pro');
```
---
## 🗄️ DATABASE SCHEMA
### Core Tables
```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│      users       │     │ specialist_      │     │    services      │
├──────────────────┤     │    profiles      │     ├──────────────────┤
│ id (UUID, PK)    │     ├──────────────────┤     │ id (UUID, PK)    │
│ phone (unique)   │◄────┤ user_id (PK, FK) │     │ specialist_id FK │
│ email            │     │ display_name     │     │ category_id FK   │
│ first_name       │     │ bio              │     │ name             │
│ last_name        │     │ rating           │     │ price            │
│ role             │     │ reviews_count    │     │ duration_min     │
│ is_verified      │     │ latitude         │     │ is_active        │
│ ...              │     │ longitude        │     │ ...              │
└──────────────────┘     └──────────────────┘     └──────────────────┘
                                │                        │
                                │                        │
                                ▼                        ▼
                         ┌──────────────────┐     ┌──────────────────┐
                         │   appointments   │     │     reviews      │
                         ├──────────────────┤     ├──────────────────┤
                         │ id (UUID, PK)    │     │ id (UUID, PK)    │
                         │ client_id FK     │     │ appointment_id FK│
                         │ specialist_id FK │     │ client_id FK     │
                         │ service_id FK    │     │ specialist_id FK │
                         │ start_datetime   │     │ rating (1-5)     │
                         │ end_datetime     │     │ text             │
                         │ status           │     │ is_anonymous     │
                         │ price            │     │ ...              │
                         │ ...              │     └──────────────────┘
                         └──────────────────┘
```
### Key Relationships
- `User` → `Profile` (OneToOne, role=client)
- `User` → `SpecialistProfile` (OneToOne, role=specialist)
- `SpecialistProfile` → `Service` (OneToMany)
- `SpecialistProfile` → `SpecialistWorkingHours` (OneToMany, 7 days)
- `SpecialistProfile` → `SpecialistTimeOff` (OneToMany)
- `Appointment` → `User` (client), `SpecialistProfile`, `Service`
- `Appointment` → `Payment` (OneToMany)
- `Review` → `Appointment`, `User`, `SpecialistProfile` (planned)
### Enums & Choices
```python
# User roles
class Role(TextChoices):
    CLIENT = "client"
    SPECIALIST = "specialist"
    ADMIN = "admin"
# App type (для X-App-Type header и DeviceToken)
class AppType(TextChoices):
    CLIENT = "client"       # 🟢 Ayla
    PRO = "pro"             # 🟣 Ayla Pro
# Appointment status (Booking Engine state machine)
class AppointmentStatus(TextChoices):
    PENDING = "pending"                   # Ожидает подтверждения
    AWAITING_PAYMENT = "awaiting_payment" # Ожидает оплаты (NEW)
    CONFIRMED = "confirmed"               # Подтверждена
    IN_PROGRESS = "in_progress"           # В процессе
    COMPLETED = "completed"               # Завершена
    CANCELLED = "cancelled"               # Отменена
    NO_SHOW = "no_show"                   # Клиент не пришёл
# Payment status
class PaymentStatus(TextChoices):
    PENDING = "pending"
    AUTHORIZED = "authorized"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"
# Device platform
class DevicePlatform(TextChoices):
    IOS = "ios"
    ANDROID = "android"
```
---
## 🔌 API DESIGN
### X-App-Type Header
> **ОБЯЗАТЕЛЬНО** для всех запросов!
```http
X-App-Type: client   # Ayla (клиентское приложение)
X-App-Type: pro      # Ayla Pro (приложение мастера)
```
**Middleware проверяет**:
- Наличие заголовка (403 если отсутствует)
- Соответствие endpoint ↔ app_type
- Ошибка: `403 WRONG_APP_TYPE`
### API Маркировка
| Маркер | Приложение | Endpoints |
|--------|------------|-----------|
| 🟢 | Ayla (Client) | AI Chat, Search, Booking (client), Payments, Reviews |
| 🟣 | Ayla Pro | Schedule, Services CRUD, Analytics, Manual Booking |
| ⚪ | Shared | Auth, Profile, Notifications, Appointments (read) |
### URL Structure
```
/api/v1/
├── auth/                         ⚪ Shared
│   ├── POST   /register/              # Регистрация
│   ├── POST   /login/                 # Вход (получить OTP)
│   ├── POST   /verify-otp/            # Подтвердить OTP
│   ├── POST   /token/refresh/         # Обновить JWT
│   ├── POST   /logout/                # Выход
│   └── POST   /social/{provider}/     # OAuth (vk, google, apple, yandex)
│
├── users/
│   ├── GET    /me/                    # Текущий пользователь
│   ├── PATCH  /me/                    # Обновить профиль
│   ├── DELETE /me/                    # Удалить аккаунт
│   └── GET    /me/appointments/       # Мои записи
│
├── specialists/
│   ├── GET    /                       # Список мастеров (с фильтрами)
│   ├── GET    /{id}/                  # Профиль мастера
│   ├── GET    /{id}/services/         # Услуги мастера
│   ├── GET    /{id}/slots/            # Доступные слоты
│   ├── GET    /{id}/reviews/          # Отзывы
│   ├── POST   /{id}/favorite/         # Добавить в избранное
│   └── DELETE /{id}/favorite/         # Удалить из избранного
│
├── services/
│   ├── GET    /categories/            # Категории услуг
│   └── GET    /                       # Поиск услуг
│
├── appointments/
│   ├── POST   /                       # Создать запись
│   ├── GET    /{id}/                  # Детали записи
│   ├── POST   /{id}/cancel/           # Отменить
│   ├── POST   /{id}/reschedule/       # Перенести
│   └── POST   /{id}/complete/         # Завершить (мастер)
│
├── reviews/
│   ├── POST   /                       # Оставить отзыв 🟢
│   ├── PATCH  /{id}/                  # Редактировать отзыв 🟢
│   └── POST   /{id}/reply/            # Ответ мастера на отзыв 🟣
│
├── payments/
│   ├── POST   /create/                # Создать платёж (YooKassa) 🟢
│   ├── GET    /{id}/                  # Статус платежа
│   ├── POST   /webhook/               # YooKassa webhook (AllowAny)
│   └── POST   /{id}/refund/           # Возврат платежа 🟢
│
├── search/
│   └── GET    /                       # Глобальный поиск 🟢
│
├── notifications/                     # 🔜 NOT YET IMPLEMENTED
├── ai/                                # 🔜 NOT YET IMPLEMENTED
│
└── health/
    ├── GET    /                       # Health check
    └── GET    /ready/                 # Readiness check
```
### Request/Response Format
**Успешный ответ:**
```json
{
  "data": { ... },
  "meta": {
    "page": 1,
    "per_page": 20,
    "total": 100
  }
}
```
**Ошибка:**
```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid input",
    "details": {
      "phone": ["This field is required"]
    }
  }
}
```
### Authentication
- **JWT** (Access + Refresh tokens) via `simplejwt`
- Access token: 15 минут (`SIMPLE_JWT.ACCESS_TOKEN_LIFETIME`)
- Refresh token: 90 дней
- **Anonymous JWT**: `/auth/anonymous` → `{ access_token, is_anonymous: true }`
- **OTP verify**: `/auth/verify-otp` → `{ access_token, refresh_token, expires_in, is_new_user, onboarding_completed, user }`
- Header: `Authorization: Bearer <token>`

### API Response Field Names (Spec v2.0)
> **ВАЖНО:** JWT token fields в ответах — `access_token` / `refresh_token` (не `access` / `refresh`)
> Payment status mapping: internal `paid` → API `succeeded`, internal `authorized` → API `pending`
> Payment field: `external_id` (не `provider_payment_id`)
---
## 📝 CODING STANDARDS
### Python Style
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
        # Validate slot availability
        if not self._is_slot_available(specialist_id, start_datetime):
            raise SlotNotAvailableError("Slot is not available")

        # Create appointment
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
        # no validation
        a = Appointment(client_id=c, specialist_id=s)
        a.save()
        return a
```
### Naming Conventions
| Тип | Стиль | Пример |
|-----|-------|--------|
| Класс | PascalCase | `AppointmentService` |
| Функция/метод | snake_case | `create_appointment` |
| Переменная | snake_case | `user_id` |
| Константа | SCREAMING_SNAKE | `MAX_SLOTS_PER_DAY` |
| Модуль | snake_case | `appointment_service.py` |
| URL path | kebab-case | `/api/v1/appointments/` |
### File Organization
```python
# models.py — порядок
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
### Serializers
```python
# ✅ Используй отдельные serializers для разных операций
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
### Views
```python
# ✅ Используй ViewSets для CRUD
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
        # Filter by current user
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
### Services (Business Logic)
```python
# ✅ Выноси бизнес-логику в services
# apps/appointments/services.py
class SlotCalculator:
    """Calculate available booking slots."""

    SLOT_INTERVAL_MINUTES = 30
    MIN_BOOKING_NOTICE_HOURS = 1

    def __init__(self, specialist: SpecialistProfile):
        self.specialist = specialist
        self._cache = caches["default"]

    def get_available_slots(
        self,
        date: date,
        service: Service,
    ) -> list[datetime]:
        """Get available slots for a specific date and service."""
        cache_key = f"slots:{self.specialist.pk}:{date}:{service.pk}"

        # Try cache first
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Calculate slots
        slots = self._calculate_slots(date, service)

        # Cache for 60 seconds
        self._cache.set(cache_key, slots, timeout=60)

        return slots

    def _calculate_slots(self, date: date, service: Service) -> list[datetime]:
        # 1. Get working hours for this day
        schedule = self._get_schedule(date)
        if not schedule:
            return []

        # 2. Generate all possible slots
        all_slots = self._generate_slots(
            date,
            schedule.start_time,
            schedule.end_time,
            service.duration,
        )

        # 3. Filter out blocked time
        available = self._filter_blocked(all_slots, date)

        # 4. Filter out booked slots
        available = self._filter_booked(available, service.duration)

        # 5. Filter out past slots
        available = self._filter_past(available)

        return available
```
### Celery Tasks
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
---
## 🧪 TESTING
### Test Structure
```
apps/users/
├── tests/
│   ├── __init__.py
│   ├── conftest.py              # Fixtures for this app
│   ├── test_models.py           # Model tests
│   ├── test_serializers.py      # Serializer tests
│   ├── test_views.py            # API endpoint tests
│   ├── test_services.py         # Business logic tests
│   └── factories.py             # Model factories
```
### Fixtures (conftest.py)
```python
# apps/users/tests/conftest.py
import pytest
from rest_framework.test import APIClient
from apps.users.tests.factories import UserFactory, ClientProfileFactory
@pytest.fixture
def api_client():
    return APIClient()
@pytest.fixture
def user():
    return UserFactory()
@pytest.fixture
def client_user():
    user = UserFactory(role="client")
    ClientProfileFactory(user=user)
    return user
@pytest.fixture
def authenticated_client(api_client, client_user):
    api_client.force_authenticate(user=client_user)
    return api_client
```
### Factories
```python
# apps/users/tests/factories.py
import factory
from factory.django import DjangoModelFactory
from apps.users.models import User, ClientProfile
class UserFactory(DjangoModelFactory):
    class Meta:
        model = User

    phone = factory.Sequence(lambda n: f"+7900000{n:04d}")
    first_name = factory.Faker("first_name", locale="ru_RU")
    last_name = factory.Faker("last_name", locale="ru_RU")
    role = "client"
    is_active = True
    is_verified = True
class ClientProfileFactory(DjangoModelFactory):
    class Meta:
        model = ClientProfile

    user = factory.SubFactory(UserFactory, role="client")
```
### Test Examples
```python
# test_views.py
import pytest
from rest_framework import status
from apps.appointments.models import Appointment
@pytest.mark.django_db
class TestAppointmentCreate:
    """Test POST /api/v1/appointments/"""

    def test_create_appointment_success(
        self,
        authenticated_client,
        specialist,
        service,
    ):
        """Client can create appointment."""
        response = authenticated_client.post(
            "/api/v1/appointments/",
            data={
                "specialist_id": str(specialist.pk),
                "service_id": str(service.pk),
                "start_datetime": "2026-03-20T14:00:00Z",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert Appointment.objects.count() == 1

        appointment = Appointment.objects.first()
        assert appointment.status == "pending"
        assert appointment.specialist == specialist

    def test_create_appointment_slot_taken(
        self,
        authenticated_client,
        existing_appointment,  # fixture that creates appointment
    ):
        """Cannot book already taken slot."""
        response = authenticated_client.post(
            "/api/v1/appointments/",
            data={
                "specialist_id": str(existing_appointment.specialist_id),
                "service_id": str(existing_appointment.service_id),
                "start_datetime": existing_appointment.start_datetime.isoformat(),
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["error"]["code"] == "SLOT_NOT_AVAILABLE"

    def test_create_appointment_unauthenticated(self, api_client):
        """Unauthenticated user cannot create appointment."""
        response = api_client.post("/api/v1/appointments/", data={})
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
```
### Running Tests
```bash
make test              # All tests
make test-cov          # With coverage report
make test-fast         # Parallel execution
make test-app APP=users  # Single app
```
---
## 🤖 AI ASSISTANT INTEGRATION
### Claude Service
```python
# apps/ai_assistant/services.py
from anthropic import Anthropic
from django.conf import settings
class ClaudeService:
    """Service for interacting with Claude AI."""

    def __init__(self):
        self.client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self.model = settings.CLAUDE_MODEL

    async def chat(
        self,
        conversation: Conversation,
        user_message: str,
    ) -> tuple[str, dict | None]:
        """
        Send message to Claude and get response.

        Returns:
            Tuple of (response_text, action_data)
            action_data contains structured data for UI (specialists, slots, etc.)
        """
        # Build messages history
        messages = self._build_messages(conversation, user_message)

        # Get specialist context (for recommendations)
        specialist_context = await self._get_specialist_context(conversation)

        # Call Claude
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=settings.CLAUDE_MAX_TOKENS,
            system=self._get_system_prompt(specialist_context),
            messages=messages,
            tools=self._get_tools(),
        )

        # Parse response
        return self._parse_response(response)

    def _get_system_prompt(self, context: dict) -> str:
        """Build system prompt with context."""
        return f"""Ты — AI-ассистент Ayla, помогаешь клиентам найти идеального мастера красоты.
КОНТЕКСТ:
- Город клиента: {context.get('city', 'Не указан')}
- Предпочтения: {context.get('preferences', 'Не указаны')}
- История записей: {context.get('history_summary', 'Новый клиент')}
ДОСТУПНЫЕ МАСТЕРА:
{context.get('specialists_summary', 'Загрузка...')}
ПРАВИЛА:
1. Будь дружелюбным и профессиональным
2. Задавай уточняющие вопросы, чтобы понять потребности
3. Рекомендуй конкретных мастеров с объяснением почему
4. Используй tool 'show_specialists' для показа списка мастеров
5. Используй tool 'show_slots' для показа доступных слотов
6. Используй tool 'confirm_booking' для завершения бронирования
7. Отвечай на русском языке
8. Будь кратким, не больше 2-3 предложений за раз
"""

    def _get_tools(self) -> list[dict]:
        """Define tools Claude can use."""
        return [
            {
                "name": "show_specialists",
                "description": "Show recommended specialists to the user",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "specialist_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of specialist UUIDs to show",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Why these specialists are recommended",
                        },
                    },
                    "required": ["specialist_ids", "reason"],
                },
            },
            {
                "name": "show_slots",
                "description": "Show available time slots for a specialist",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "specialist_id": {"type": "string"},
                        "service_id": {"type": "string"},
                        "date": {"type": "string", "format": "date"},
                    },
                    "required": ["specialist_id", "service_id"],
                },
            },
            {
                "name": "confirm_booking",
                "description": "Confirm and create the booking",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "specialist_id": {"type": "string"},
                        "service_id": {"type": "string"},
                        "slot_datetime": {"type": "string", "format": "date-time"},
                    },
                    "required": ["specialist_id", "service_id", "slot_datetime"],
                },
            },
        ]
```
### Prompts File
```python
# apps/ai_assistant/prompts.py
SYSTEM_PROMPT_TEMPLATE = """..."""
RECOMMENDATION_PROMPT = """
На основе запроса клиента: "{query}"
Выбери топ-3 подходящих мастеров из списка и объясни почему:
{specialists_data}
Учитывай:
- Соответствие специализации
- Рейтинг и отзывы
- Расстояние от клиента
- Доступность слотов
"""
```
---
## 🧠 PERSONALIZATION ENGINE (UserPersonalContext)

> **Load-bearing для North Star «AI который помнит. Всегда.»** Без памяти Ayla = обычный booking + AI search → недифференцируемый продукт. Полная архитектура: Notion `334b0dab295581d587cfeaf49efd2d5b` или `docs/PRODUCT_AUDIT_2026-04.md` Section 1.9/1.11.

### Три источника данных
| Источник | Метод | Доля данных |
|----------|-------|-------------|
| **Явные вопросы** | AI задаёт органично, 1 вопрос/сессия, cooldown 24ч | ~30% |
| **Поведенческие паттерны** | Celery-task раз в сутки анализирует историю | ~50% |
| **Контекстуальные сигналы** | Claude structured-extraction из текста чата | ~20% |

### Три уровня деликатности
- 🟢 **Зелёная зона** — упоминать открыто (адрес работы, бюджет, любимый мастер, диета);
- 🟡 **Жёлтая зона** — использовать молча, не называть источник (наличие детей, занятость, паттерны партнёра);
- 🔴 **Красная зона** — только локально, retention 90 дней (беременность, хронические заболевания).

### 8 anti-spam правил
1. Одно поле за сессию;  2. Не на первом взаимодействии (только со 2–3 запроса);
3. Cooldown 24ч;  4. Skip 2 раза → пауза 30 дней;
5. Данные уже есть → молчать;  6. Органично или никак;
7. Объяснять зачем («подберу рядом с офисом — где работаешь?»);  8. Skip без наказания.

### Метрики персонализации
| Метрика | Цель |
|---------|------|
| Context fill rate (полей за первый месяц) | ≥ 5 |
| Question answer rate | ≥ 60% |
| Context usage rate (запросов с применением контекста) | ≥ 60% |
| Skip rate | ≤ 30% |
| Booking conversion lift при наличии контекста | +15% |

### API endpoints (М3+)
```
GET    /api/v1/users/me/personal-context/         # Получить весь контекст
PATCH  /api/v1/users/me/personal-context/         # Обновить поля
DELETE /api/v1/users/me/personal-context/{field}/ # Удалить поле
POST   /api/v1/users/me/personal-context/skip/    # Пропустить вопрос
DELETE /api/v1/users/me/personal-context/         # Очистить весь контекст (152-ФЗ право)
```

### 152-ФЗ соответствие
- Все поля шифруются at-rest;
- Красная зона не возвращается в GET по умолчанию (требует явного подтверждения);
- Логи доступа к красной зоне ведутся отдельно;
- Команда «Забудь мой адрес работы» + кнопка в профиле = полное удаление поля;
- Кнопка «Очистить историю Ayla» = total wipe.

### Status (2026-04-27)
🔴 **Не реализовано.** Перенесено в M3 P0 после PM-аудита 2026-04-27 — **load-bearing для всей стратегии Ayla.** Без UserPersonalContext к M4-pilot vision не доедет.

---

## 🍽️ AI FOOD SCANNER

> **Daily-hook для retention.** Гипотеза H1: «≥2.5 фото-сканов еды/день у активного пользователя в дни 8–14». Валидируется в Test 1 cheap-validation (`docs/HYPOTHESIS_VALIDATION_PLAN_2026-04.md`), результат к 2026-05-13.

**Vendor decision:** multi-vendor (OpenAI Vision + Yandex Vision) на M5, self-host ViT в Phase 6. Slice 1 готов (memory: `project_food_scanner_decision`).

**Метрики MVP:**
- ≥1 скан/день минимум, 2.5 average (после валидации H1);
- Точность распознавания русских блюд (борщ, винегрет, плов) — критическая для retention;
- Витаминные инсайты + рекомендации связанные с beauty (дефицит витамина D → рекомендация мастера с аргановым массажем).

---

## 💰 PAYMENTS (YooKassa)
### Payment Flow
```
1. Client → POST /api/v1/payments/
   ↓
2. Create Payment in DB (status=pending)
   ↓
3. Create YooKassa payment
   ↓
4. Return confirmation_url to client
   ↓
5. Client pays in YooKassa widget
   ↓
6. YooKassa → POST /api/v1/payments/webhook/
   ↓
7. Update Payment status (succeeded/cancelled)
   ↓
8. Update Appointment status
   ↓
9. Send notifications
```
### Webhook Handler
```python
# apps/payments/views.py
class YooKassaWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        # Verify webhook signature
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
---
## 📱 NOTIFICATIONS
### Template Registry
```python
# apps/notifications/templates.py
# Deep Links для разных приложений
DEEP_LINKS = {
    "client": "ayla-client://",   # 🟢 Ayla
    "pro": "ayla-pro://",         # 🟣 Ayla Pro
}
TEMPLATES = {
    "appointment_created_client": NotificationTemplate(
        id="appointment_created_client",
        app_type="client",  # 🟢 Только в Ayla
        channel=Channel.BOTH,
        priority=Priority.HIGH,
        push_title="✅ Запись подтверждена!",
        push_body="{{service_name}} у {{specialist_name}}, {{date_time}}",
        sms_text="Ayla: Вы записаны на {{service_name}} {{date_time}}. {{address}}",
        deep_link="ayla-client://appointment/{{appointment_id}}",
    ),
    "appointment_created_specialist": NotificationTemplate(
        id="appointment_created_specialist",
        app_type="pro",  # 🟣 Только в Ayla Pro
        channel=Channel.BOTH,
        priority=Priority.HIGH,
        push_title="📅 Новая запись!",
        push_body="{{client_name}} на {{service_name}}, {{date_time}}",
        sms_text="Ayla Pro: Новая запись на {{date_time}}",
        deep_link="ayla-pro://appointment/{{appointment_id}}",
    ),
    # ... other templates
}
```
### Sending Notifications
```python
# Usage
notification_service = NotificationService()
notification_service.send(
    user=appointment.client,
    template_id="appointment_created_client",
    context={
        "service_name": "Маникюр",
        "specialist_name": "Елена",
        "date_time": "15 марта в 14:00",
        "address": "ул. Пушкина, 10",
        "appointment_id": str(appointment.id),
    },
)
```
---
## 🔧 COMMON TASKS
### Добавление нового endpoint
1. Создай/обнови `models.py`
2. Создай миграцию: `make makemigrations`
3. Примени миграцию: `make migrate`
4. Создай `serializers.py`
5. Создай `views.py` (ViewSet)
6. Добавь URL в `urls.py`
7. Напиши тесты
8. Запусти тесты: `make test-app APP=<app_name>`
### Добавление Celery task
1. Создай task в `tasks.py`
2. Если периодический — добавь в `config/celery.py` beat_schedule
3. Перезапусти Celery: `make restart`
### Добавление нового notification template
1. Добавь template в `apps/notifications/templates.py`
2. Добавь event в `apps/notifications/events.py`
3. Добавь аналитику в точке отправки
### Debug проблемы
```bash
make logs              # Все логи
make logs-api          # Только API
make shell             # Зайти в контейнер
make django-shell      # Django shell (IPython)
make psql              # PostgreSQL shell
```
---
## 🏗️ BOOKING ENGINE (DDD Architecture)

> **Добавлен 2026-03-26.** Полноценный booking engine с DDD-архитектурой внутри `appointments/`.

### Архитектура слоёв
```
appointments/
├── domain/                  # Чистый Python, без Django
│   ├── value_objects.py     # TimeInterval, BookingStatus, BookingStateMachine, BookingSnapshot
│   ├── exceptions.py        # BookingDomainError → 7 типов исключений
│   └── policies.py          # CommissionPolicy(8%), CancellationPolicy, ReschedulePolicy, BookingWindowPolicy
├── application/             # Use-cases, DTOs
│   ├── dto.py               # Input/Output DTOs (все ID — UUID)
│   └── services/
│       ├── create_booking_service.py    # Атомарное создание + idempotency + row locking
│       ├── cancel_reschedule_service.py # Отмена/перенос через state machine
│       └── availability_query_service.py # Расчёт слотов + cache-aside
├── infrastructure/
│   ├── availability/
│   │   ├── providers.py     # BusyIntervalProviders (bookings + time-off)
│   │   └── slot_builder.py  # SlotBuilderService (30-мин сетка, ZoneInfo)
│   ├── cache/
│   │   └── slot_cache.py    # SlotCacheService (LocMemCache для MVP)
│   └── outbox_worker.py     # Обработчик событий (заглушка для MVP)
└── models.py                # Appointment (upgraded) + WorkingHours + TimeOff + Payment + OutboxEvent
```

### Ключевые паттерны
| Паттерн | Где | Зачем |
|---------|-----|-------|
| **State Machine** | `BookingStateMachine` | Явные переходы статусов (pending→awaiting_payment→confirmed→completed) |
| **Idempotency Key** | `Appointment.idempotency_key` + `X-Idempotency-Key` header | Защита от дубликатов при сетевых ретраях |
| **Snapshot** | `Appointment.snapshot_*` поля | Неизменяемая запись финансов на момент бронирования |
| **Strategy/Policy** | `domain/policies.py` | Подменяемые бизнес-правила (комиссия, отмена, перенос) |
| **Transactional Outbox** | `OutboxEvent` модель | Гарантированная доставка событий (booking.created, booking.cancelled и т.д.) |
| **Row-Level Locking** | `select_for_update()` в CreateBookingService | Блокировка только записей конкретного мастера, не всей таблицы |
| **Thin Views** | `appointments/views.py` | Views только парсят/валидируют, вся логика в application services |

### Статусы бронирования (State Machine)
```
pending → awaiting_payment → confirmed → completed
                          └→ cancelled
confirmed → cancelled | no_show
```
**ACTIVE_BOOKING_STATUSES** (держат слот): `{pending, awaiting_payment, confirmed}`
**Terminal** (нет дальнейших переходов): `{completed, cancelled, no_show}`

### Настройки Booking Engine
```python
BOOKING_COMMISSION_PERCENT = 8.0    # Комиссия платформы (%)
BOOKING_MIN_AHEAD_MINUTES = 60      # Мин. время до записи
BOOKING_MAX_AHEAD_DAYS = 60         # Макс. дней вперёд
BOOKING_SLOT_GRID_MINUTES = 30      # Интервал сетки слотов
```

### MVP-ограничения (активировать позже)
1. **Outbox worker не запущен** — события пишутся в БД, но не обрабатываются (нужен Celery + Redis)
2. **LocMemCache** — заменить на django-redis для production
3. **select_for_update() — no-op на SQLite** — concurrency тесты требуют PostgreSQL
4. **Notifications** — не реализовано (Firebase FCM, SMS reminders)
5. **AI Chat** — не реализовано (Claude integration)

---
## 💳 PAYMENTS (YooKassa) — Реализовано

### Endpoints
```
POST /api/v1/payments/create/     🟢 Создать платёж → confirmation_url
GET  /api/v1/payments/{id}/       ⚪ Статус платежа
POST /api/v1/payments/webhook/    ⚪ YooKassa webhook (AllowAny)
POST /api/v1/payments/{id}/refund/ 🟢 Полный/частичный возврат
```

### Архитектура
- **YooKassaService** (`payments/services.py`) — обёртка над `yookassa` SDK
- **Two-stage payments**: hold → capture (при `appointment.completed`)
- **Idempotency**: webhook de-dup через `last_webhook_event_id` + `X-Request-Id`
- **Idempotency**: повторный POST /create возвращает существующий pending payment
- **Комиссия**: 8% `BOOKING_COMMISSION_PERCENT`, split-платёж если `YOOKASSA_AGENT_ID` задан

### PaymentStatus Mapping (Internal → API Spec)
| Internal | API Response |
|----------|-------------|
| `pending` | `pending` |
| `authorized` | `pending` |
| `paid` | `succeeded` |
| `failed` | `failed` |
| `refunded` | `refunded` |
| `partially_refunded` | `refunded` |

### Env переменные
```
YOOKASSA_SHOP_ID=       # YooKassa Shop ID
YOOKASSA_SECRET_KEY=    # YooKassa Secret Key
YOOKASSA_AGENT_ID=      # Sub-account for split payments (optional)
```

---
## ⭐ REVIEWS & RATINGS — Реализовано

### Endpoints
```
POST  /api/v1/reviews/               🟢 Оставить отзыв (client, completed appointment)
PATCH /api/v1/reviews/{id}/           🟢 Редактировать текст (автор)
POST  /api/v1/reviews/{id}/reply/     🟣 Ответ мастера
GET   /api/v1/specialists/{id}/reviews/ ⚪ Публичный список (AllowAny)
```

### Ключевые правила
- **OneToOne**: один отзыв на одну запись (409 `REVIEW_EXISTS` при дубликате)
- **Rating recalculation**: синхронно в транзакции через `_recalculate_rating(specialist)`
- **Anonymous reviews**: `is_anonymous=true` → `client_name=null` в API
- **Moderation**: `is_hidden=true` → excluded from listing + rating calculation
- **Pagination**: PageNumberPagination, default 20, max 100, `?sort=recent|rating`

---
## ⚠️ IMPORTANT NOTES
### Two Apps Architecture Rules
1. **X-App-Type обязателен** — каждый запрос должен содержать header
2. **Проверяй доступ endpoint** — не все endpoints доступны обоим приложениям
3. **Deep links разные** — `ayla-client://` vs `ayla-pro://`
4. **DeviceToken.app_type** — один токен ≠ оба приложения
5. **Analytics app_type** — передавай со ВСЕМИ событиями
### Что НЕЛЬЗЯ делать
1. **НЕ хардкодь секреты** — используй `.env`
2. **НЕ коммить миграции с данными** — только структура
3. **НЕ используй `Model.objects.create()` в views** — используй services
4. **НЕ используй `print()`** — используй `logging`
5. **НЕ игнорируй type hints** — они обязательны
6. **НЕ пиши бизнес-логику в serializers** — только валидация
### Что НУЖНО делать
1. ✅ Пиши docstrings для всех публичных методов
2. ✅ Добавляй тесты для новой функциональности
3. ✅ Используй transactions для связанных операций
4. ✅ Логируй важные события
5. ✅ Кешируй тяжёлые запросы
6. ✅ Валидируй входные данные
### Критичные бизнес-правила
1. **Слоты**: интервал 30 минут, минимум за 1 час до записи
2. **Отмена**: бесплатно за 24+ часа, 50% за 2-24 часа, 100% за <2 часа
3. **Рейтинг**: обновляется после каждого отзыва (денормализован)
4. **Комиссия**: 8% с онлайн-платежей
---
## 📚 EXTERNAL DOCUMENTATION
- [Django Docs](https://docs.djangoproject.com/en/5.0/)
- [DRF Docs](https://www.django-rest-framework.org/)
- [Celery Docs](https://docs.celeryq.dev/)
- [Anthropic Claude Docs](https://docs.anthropic.com/)
- [YooKassa API](https://yookassa.ru/developers/api)
- [SMS.RU API](https://sms.ru/api)
- [Firebase FCM](https://firebase.google.com/docs/cloud-messaging)
---
## 📞 CONTACTS
- **Project Owner**: Andrey
- **Tech Stack Decision Doc**: Notion
- **PRD**: Notion
- **API Spec**: Notion
- **Database Schema**: Notion
- **Analytics Events**: Notion
---
---
## 📋 SPEC ALIGNMENT STATUS

> Источник: **API Specification v2.0** в Notion + **PRD v3.0 (Ayla)**

| Секция | Статус | Примечания |
|--------|--------|------------|
| Auth (OTP, Anonymous, Social) | ✅ Aligned | `access_token`/`refresh_token` naming |
| Users (/me, profile, delete) | ✅ Aligned | `otp_code` optional (spec says required — soft deviation) |
| Specialists (catalog) | ✅ Implemented | Response wrapping via `success_response()` |
| Services (CRUD + categories) | ✅ Implemented | |
| Appointments (CRUD + state machine) | ✅ Implemented | Extra statuses `awaiting_payment`, `in_progress` beyond spec |
| Working Hours + TimeOff | ✅ Implemented | `PUT/PATCH /specialists/me/schedule/` |
| Reviews (create, edit, reply, list) | ✅ Aligned | |
| Payments (YooKassa) | ✅ Aligned | Status mapping: `paid→succeeded` |
| Search | ✅ Basic | |
| Notifications | ❌ Not implemented | M3 P0 |
| AI Chat (Claude) | ❌ Not implemented | M3 P0; план в `docs/AI_CHAT_PLAN.md` |
| **UserPersonalContext (Memory)** | ❌ **Not implemented** | **M3 P0 — load-bearing для North Star, добавлено после PM-аудита 2026-04-27** |
| Favorites | ❌ Not implemented | |
| Analytics | ❌ Not implemented | |
| Food Scanner / Nutrition | 🟡 Slice 1 done | M3+; см. Test 1 валидация H1 |
| AI-аватар + прогресс | 🟠 Pre-deferred | Phase 2 (Месяц 5+) после Test 2 валидации H5 |

*Last updated: April 27, 2026 — Ayla rebrand + PM Audit Phase 1 + Hypothesis Validation decision*

---

## 🏷️ BRAND MIGRATION STATUS (BeautyGO → Ayla)

> Ребрендинг утверждён 2026-03-30 (Notion: Brand Vision Document). Каноническое название продукта — **Ayla**, но миграция кода/инфры — поэтапная.
>
> **Update 2026-05-21 (Option 1 decision):** DNS rebrand is **deferred indefinitely**. Backend stays on `gobeauty.site` (current production hostname) until `ayla.app` is acquired or a better Ayla domain is chosen Phase 1+. The product-identity rebrand (user-facing strings, OpenAPI titles, admin UI labels) proceeds independently from the DNS rebrand. See ai-bot-platform issue #418 and the Phase 0 sprint plan's domain-decision note.

### ✅ Уже мигрировано (canonical в этом файле)
- Название продукта в документации, PRD, App Store positioning;
- Bundle IDs target: `ru.ayla.client` / `ru.ayla.pro`;
- Deep links target: `ayla-client://` / `ayla-pro://`;
- App Store/Google Play: «Ayla — AI Self-Care» / «Ayla Pro — для мастеров»;
- Vision/positioning: «AI, который помнит. Всегда.»;
- Pilot city: Пенза (был Казань);
- Audience: 20–45 (был 22–40).

### 🟡 Pending миграция (актуальное состояние кода)
| Идентификатор | Текущее значение | Target | Когда мигрировать |
|---------------|------------------|--------|-------------------|
| Mobile репо | `beautygo-mobile` | `ayla-mobile` | Перед M4-pilot launch |
| NPM пакет | `@beautygo/shared` | `@ayla/shared` | Вместе с mobile rename |
| Bundle ID iOS / Android (mobile) | `ru.beautygo.*` | `ru.ayla.*` | До публикации в App Store |
| Deep links в коде | `beautygo-*://` | `ayla-*://` | Вместе с bundle ID |
| Backend репо | `djangoProject` (path: `ayla/djangoproject`) | `ayla-backend` | Низкий приоритет, после launch |
| Notification template strings (`payments/services.py`, `notifications/`) | «BeautyGO: …», «BeautyGO Pro: …» | «Ayla: …», «Ayla Pro: …» | Pre-launch QA сweepa |
| `.env`, settings: `BEAUTYGO_*` env vars (если есть) | префикс BEAUTYGO | префикс AYLA или общий | По мере встречи |

### 📝 Правила в новом коде
1. **Любая новая user-facing строка** (push, SMS, email, UI) — пишется как **«Ayla»** / **«Ayla Pro»**;
2. **Любые новые URL/идентификаторы** — `ayla.*` / `ru.ayla.*` / `ayla-*://`;
3. **Существующий код** не переписываем «по пути» — только в рамках выделенных rebrand-тикетов;
4. **PR-ревью**: блокировать введение нового кода с «BeautyGO» бренд-строками или `beautygo` URL/identifiers;
5. **Commits**: сообщения коммитов на английском, нейтрально (`feat(ai): add personal context`), без «BeautyGO» в новых коммитах.

### 📌 Что не нужно мигрировать
- Имена тестовых файлов / fixtures (если они не user-facing);
- Исторические commits (history immutable);
- Имя Linear-проекта пока решает PO — обновляется отдельно.

---

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. The
skill has multi-step workflows, checklists, and quality gates that produce better
results than an ad-hoc answer. When in doubt, invoke the skill. A false positive is
cheaper than a false negative.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke /office-hours
- Strategy, scope, "think bigger", "what should we build" → invoke /plan-ceo-review
- Architecture, "does this design make sense" → invoke /plan-eng-review
- Design system, brand, "how should this look" → invoke /design-consultation
- Design review of a plan → invoke /plan-design-review
- Developer experience of a plan → invoke /plan-devex-review
- "Review everything", full review pipeline → invoke /autoplan
- Bugs, errors, "why is this broken", "wtf", "this doesn't work" → invoke /investigate
- Test the site, find bugs, "does this work" → invoke /qa (or /qa-only for report only)
- Code review, check the diff, "look at my changes" → invoke /review
- Visual polish, design audit, "this looks off" → invoke /design-review
- Developer experience audit, try onboarding → invoke /devex-review
- Ship, deploy, create a PR, "send it" → invoke /ship
- Merge + deploy + verify → invoke /land-and-deploy
- Configure deployment → invoke /setup-deploy
- Post-deploy monitoring → invoke /canary
- Update docs after shipping → invoke /document-release
- Weekly retro, "how'd we do" → invoke /retro
- Second opinion, codex review → invoke /codex
- Safety mode, careful mode, lock it down → invoke /careful or /guard
- Restrict edits to a directory → invoke /freeze or /unfreeze
- Upgrade gstack → invoke /gstack-upgrade
- Save progress, "save my work" → invoke /context-save
- Resume, restore, "where was I" → invoke /context-restore
- Security audit, OWASP, "is this secure" → invoke /cso
- Make a PDF, document, publication → invoke /make-pdf
- Launch real browser for QA → invoke /open-gstack-browser
- Import cookies for authenticated testing → invoke /setup-browser-cookies
- Performance regression, page speed, benchmarks → invoke /benchmark
- Review what gstack has learned → invoke /learn
- Tune question sensitivity → invoke /plan-tune
- Code quality dashboard → invoke /health
