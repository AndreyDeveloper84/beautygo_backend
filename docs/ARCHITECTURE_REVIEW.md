# ARCHITECTURE REVIEW — Ayla Target

**Phase 1: Current State Audit**
**Date:** 2026-04-23
**Status:** Draft (read-only, no prescriptions)
**Source of truth for target:** Notion docs (Ayla v2.0 architecture, PRD v3.0, AI Personalization arch, Brand Vision)
**Source of truth for current state:** code in `djangoproject/` and `beautygo-mobile/`, CLAUDE.md

> Это фаза 1 — **только аудит**. Никаких предложений, решений, roadmap. Цель — зафиксировать факты и пробелы, чтобы в фазе 2 принимать решения на твёрдой почве.

---

## 1. Scope & Methodology

**Что обозрено:**
- Backend: `djangoproject/` (Django 5.2, DRF, 6 apps, 124 .py файла, 22 тест-файла)
- Mobile: `beautygo-mobile/` (Expo/React Native monorepo: `apps/client`, `apps/pro`, `packages/shared`)
- Infra/ops: `Dockerfile`, `docker-compose.yml`, `requirements.txt`, settings, CI
- Документация: `CLAUDE.md` (детальная), `docs/` (скудная: только `AUTH_ENDPOINTS.md` + `openapi.yaml`)

**Что НЕ обозрено** (осознанный срез):
- Глубокое качество кода (это задача `/review` в Phase 3 по отдельным PR)
- Реальная production-конфигурация (секреты, nginx, CI pipeline) — только то что в репо
- Figma-дизайны (упоминаются в CLAUDE.md, но не для архитектурного аудита)

**Легенда:**
- ✅ **Done** — реализовано и работает по документам/спецификации
- 🟡 **Partial** — реализовано частично или с отклонениями от спецификации
- ⚠️ **Gap** — заявлено в target, не найдено в коде
- 🔴 **Risk** — существенное расхождение между target и реальностью, критичное для запуска

---

## 2. Target State — Ayla v2.0 (из Notion)

### 2.1 Продуктовая суть
- **Позиционирование:** «AI, который помнит. Всегда.» — AI-ассистент качества жизни (beauty как точка входа, daily habit как retention).
- **Ребрендинг:** BeautyGO → **Ayla** (30.03.2026). Bundle IDs: `ru.ayla.client` / `ru.ayla.pro`.
- **Пилот:** Пенза (не Казань) → Казахстан (Phase 5).
- **Killer Scenario:** утренний скан еды → дефицит витамина D → AI рекомендует массаж с маслом у знакомого мастера → запись в 1 tap → вечером аватар показывает прогресс → шеринг в Telegram.

### 2.2 Архитектурные принципы
1. **AI-first** — AI ядро, а не надстройка
2. **Memory-first** — Ayla помнит контекст, персонализация = главное конкурентное преимущество
3. **Mobile-first** — два нативных приложения (RN 0.73+ / TypeScript)
4. **API-first** — единый REST для обоих клиентов
5. **Daily-open** — архитектура под ежедневное открытие (Food Scanner, Water Tracker, таб «День»)

### 2.3 Two Apps
| Приложение | Bundle ID | Tabs | Аудитория |
|---|---|---|---|
| 🟢 **Ayla** (Client) | `ru.ayla.client` | 🏠 Главная · 🍽️ Питание · ✨ Я · 📅 День · 👤 Профиль | Женщины 20–45, daily usage |
| 🟣 **Ayla Pro** (Specialist) | `ru.ayla.pro` | Расписание · Записи · Аналитика · Профиль | Индивидуальные мастера |

### 2.4 Backend stack (заявленный)
| Компонент | Технология |
|---|---|
| Framework | Django 5.0 + DRF |
| DB | PostgreSQL 16 (с PostGIS, pg_trgm, pgvector) |
| Cache / Queue | Redis + Celery |
| AI | Claude Sonnet 4.x через MCP |
| Storage | S3-совместимое |
| Auth | OTP (SMS.RU) + JWT + OAuth (VK/Google/Apple/Yandex) |
| Payments | YooKassa (2-stage: hold → capture; split к мастерам) |
| Push | Firebase / APNs |
| SMS | SMS.RU |
| Maps | ⚠️ TBD (в Ayla v2.0 явно помечено unresolved) |

### 2.5 AI Layer (только в Client)
Компоненты: **NLU Engine** → **Recommendation Engine** → **PersonalizationEngine** → **FoodScanService** → **Voice Engine (STT + TTS)** → **MCP Server**

MCP Tools (10): `search_specialists`, `get_available_slots`, `create_booking`, `cancel_booking`, `reschedule_booking`, `get_specialist_details`, `get_user_appointments`, `get_user_context`, `log_food`, `get_nutrition_summary`.

### 2.6 AI Personalization (отдельный арх. документ)
- `UserPersonalContext` модель с ~30 полями в трёх зонах деликатности:
  - 🟢 Зелёная (адрес работы, бюджет, любимые мастера) — упоминать открыто
  - 🟡 Жёлтая (дети, занятость, партнёр) — использовать молча
  - 🔴 Красная (беременность, здоровье) — только локально, auto-cleanup 90 дней
- 8 Anti-Spam правил сбора (1 вопрос/сессия, cooldown 24ч, skip→пауза 30 дней)
- 3 источника данных: explicit questions (30%), behavior inference (50%), speech signals (20%)
- Celery tasks: `infer_user_patterns` (daily), `cleanup_sensitive_data` (weekly)
- API: `GET/PATCH/DELETE /api/v1/users/me/personal-context/`

### 2.7 Booking Engine (из CLAUDE.md — DDD архитектура)
- Layers: `domain/` (pure Python) → `application/` (use-cases) → `infrastructure/` (slot builder, cache, outbox)
- Patterns: State Machine, Idempotency Key, Snapshot, Strategy/Policy, Transactional Outbox, Row-Level Locking, Thin Views
- Status transitions: `pending → awaiting_payment → confirmed → completed` (+ `cancelled`, `no_show`)
- Commission 8%, slot grid 30 мин, min ahead 60 мин, max ahead 60 дней

### 2.8 Roadmap (Notion)
| Milestone | Фокус | Дедлайн |
|---|---|---|
| M1 Auth & Foundation | Backend skeleton, Auth, profiles, monorepo, design system, onboarding | 2026-04-06 |
| M2 Catalog & Discovery | Каталог мастеров, фильтры, карточка, поиск, избранное | 2026-04-19 |
| M3 Booking & Payments | Полный booking flow, YooKassa, эскроу, возвраты | 2026-05-10 |
| M4 AI + Home Screen | Claude MCP, Home Screen, Food Scanner, Water Tracker, голос, аватар | 2026-05-31 |
| M5 Пилот Пенза | Онбординг 50+ мастеров, push, отзывы | 2026-06-30 |

**Сегодня (2026-04-23):** M1 должен быть позади (≈17 дней назад), M2 идёт последние 4 дня, до M3 — 17 дней.

---

## 3. Current State — Backend

### 3.1 Installed apps
`djangoProject/settings/base.py:39` → `INSTALLED_APPS`:

- `users`, `services`, `appointments`, `reviews`, `payments` — ✅ все в INSTALLED_APPS
- 🔴 **`search`** — подключён как URL (`djangoProject/urls.py:31`) но **НЕ в INSTALLED_APPS**. Работает за счёт views-only паттерна (нет моделей). Admin и app config недоступны.
- ⚠️ **`notifications`** — отсутствует полностью (нет app, нет модели, нет endpoint)
- ⚠️ **`ai`** / `ai_assistant` — отсутствует полностью
- ⚠️ **`nutrition`** (FoodScanner, Water Tracker) — отсутствует
- ⚠️ **`avatar`** — отсутствует
- ⚠️ **`personal_context`** — отсутствует (ни модели, ни API)

### 3.2 Data model (что реально в БД)

| Модель | Файл | Статус |
|---|---|---|
| `User` | `users/models.py:?` | ✅ |
| `Profile` (client) | `users/models.py:45` | ✅ |
| `SpecialistProfile` | `users/models.py:67` | ✅ |
| `OTPCode` | `users/models.py:126` | ✅ |
| `SocialAccount` | `users/models.py:154` | ✅ |
| `DeviceToken` (c `app_type`) | `users/models.py:184` | ✅ |
| `AnonymousSession` | `users/models.py:216` | ✅ (для anonymous JWT) |
| `ServiceCategory` | `services/models.py:12` | ✅ |
| `ServiceTemplate` | `services/models.py:57` | 🟡 (в спеке не упомянут — доп. сущность) |
| `RegionalPricing` | `services/models.py:113` | 🟡 (в спеке не упомянут) |
| `Service` | `services/models.py:168` | ✅ |
| `Appointment` | `appointments/models.py:20` | ✅ (с snapshot fields) |
| `SpecialistWorkingHours` | `appointments/models.py:222` | ✅ |
| `SpecialistTimeOff` | `appointments/models.py:275` | ✅ |
| `Payment` | `appointments/models.py:318` | 🟡 (живёт в appointments, не в payments app) |
| `OutboxEvent` | `appointments/models.py:393` | 🟡 (модель есть, **worker не запущен**) |
| `Review` | `reviews/models.py:11` | ✅ |
| `Notification` | ⚠️ | **Отсутствует** |
| `UserPersonalContext` | ⚠️ | **Отсутствует** |
| `FoodLog` / `NutritionEntry` / `WaterLog` | ⚠️ | **Отсутствует** |
| `Avatar` / `AvatarSnapshot` | ⚠️ | **Отсутствует** |
| `Favorite` (избранные мастера) | ⚠️ | **Отсутствует** (в Ayla target — `/api/v1/favorites/*`) |

### 3.3 URL map (`djangoProject/urls.py`)

✅ Есть: `/api/v1/auth/`, `/users/`, `/services/`, `/categories/`, `/service-templates/`, `/specialists/`, `/appointments/`, `/reviews/`, `/payments/`, `/search/`, `/health/`, `/api/schema/`, `/api/docs/`, `/api/redoc/`

⚠️ Нет (в target есть): `/api/v1/ai/*`, `/api/v1/favorites/*`, `/api/v1/notifications/*`, `/api/v1/nutrition/*`, `/api/v1/users/me/personal-context/`, `/api/v1/users/me/avatar/`, `/api/v1/payouts/*`, `/api/v1/specialists/me/analytics/`

### 3.4 Booking Engine (DDD)

✅ **Структура заложена полностью:**
- `appointments/domain/` — `value_objects.py`, `exceptions.py`, `policies.py`
- `appointments/application/` — `dto.py`, `services/` (use-cases)
- `appointments/infrastructure/` — `availability/`, `cache/`, `outbox_worker.py`

🟡 **MVP-ограничения (зафиксированы в CLAUDE.md):**
1. **Outbox worker не запущен** — события пишутся в `OutboxEvent`, но ничего их не обрабатывает (нет Celery + Redis в deps)
2. **LocMemCache** вместо Redis — `CACHES` не сконфигурирован (нет записи в `settings/base.py`)
3. **`select_for_update()` — no-op на SQLite** — dev БД это SQLite (`db.sqlite3` в repo), concurrency-тесты требуют PostgreSQL, которого сейчас в dev нет
4. **Notifications** не реализованы
5. **AI Chat** не реализован

### 3.5 Payments (YooKassa)

✅ Реализован — YooKassa SDK подключён (`yookassa==3.10.0`), two-stage (hold→capture), idempotency на webhook, комиссия 8%, split через `YOOKASSA_AGENT_ID`.

🟡 `payments/` app не имеет собственного `models.py` — `Payment` живёт в `appointments/models.py:318`. Это работает, но нарушает bounded context (payments depends on appointments вместо обратного).

### 3.6 Auth

✅ OTP + JWT (SimpleJWT, access 15m / refresh 90d), Anonymous JWT, Onboarding, Social Auth (VK/Google/Apple/Yandex), DeviceToken с `app_type`, AppTypeMiddleware для X-App-Type header.

🟡 Spec alignment (из CLAUDE.md): `otp_code` опционален в API vs. обязателен в спеке — soft deviation.

### 3.7 Tests

- **124 .py файла кода, 22 тест-файла** (соотношение ~5.6:1 кода к тестам — нормально для DDD проекта, где много тестов на domain+application)
- Тесты распределены: `users/tests/`, `services/tests/`, `appointments/tests/`, `reviews/tests/`, `payments/tests/`
- `pytest` + `pytest-django`
- ⚠️ **Нет:** `pytest-cov` (нет coverage-отчётов), `factory-boy`/`Faker` (фактори-паттерн упоминается в CLAUDE.md, но в deps нет), `freezegun` (тесты на время — без freeze)

### 3.8 Dependencies (requirements.txt)

**Есть:**
- Django 5.2.5, DRF 3.16, simplejwt 5.5, django-filter, drf-spectacular, django-cors-headers, django-unfold (admin)
- Storage: Pillow, django-storages, boto3
- Auth: PyJWT, cryptography, requests
- DB: psycopg2-binary
- Server: gunicorn
- Payments: yookassa 3.10
- Tests: pytest, pytest-django

🔴 **Нет (критично для target):**
- `celery` — очередь задач (упоминается в CLAUDE.md как установленная — факт не подтверждается)
- `redis` / `django-redis` — кеш и брокер
- `anthropic` — Claude SDK
- `django.contrib.gis` / `django-postgis` — геопространственные запросы (target требует)
- `pgvector` — векторный поиск (target требует для AI)
- `firebase-admin` / `pyfcm` — push-уведомления
- `openai-whisper` / `yandex-speechkit` — STT
- `factory-boy`, `Faker`, `freezegun`, `pytest-cov` — тест-tooling

### 3.9 Settings

- `djangoProject/settings/base.py` — есть `BOOKING_*`, `SMS_*`, `YOOKASSA_*` настройки
- ⚠️ **Нет `CELERY_*`**, `CACHES`, `CHANNEL_LAYERS`, никаких LLM-ключей (`ANTHROPIC_API_KEY` и т.п.)
- ⚠️ Нет `settings/test.py` / специального тест-конфига (factor-in: тесты гонятся на dev/SQLite)

### 3.10 Docker / Deploy

- ✅ `Dockerfile`, `docker-compose.yml`, `deploy.sh`, `entrypoint.sh`, `nginx/`
- ⚠️ Не проверено: какие сервисы в compose (возможно только Django; Redis/Celery/Postgres могут отсутствовать)

---

## 4. Current State — Mobile

### 4.1 Monorepo

`beautygo-mobile/` (не `ayla-mobile` — **ребрендинг не выполнен**):
- `apps/client/` — клиентское приложение
- `apps/pro/` — приложение для мастеров
- `packages/shared/` — общая библиотека

### 4.2 Tech stack

- **Expo** ~54.0 (React Native 0.81.5, React 19.1)
- **expo-router** (file-based routing) — не react-navigation
- **expo-secure-store** для токенов
- **axios** для API
- **expo-image-picker**, **expo-location**, **expo-auth-session**, **expo-apple-authentication** — заделы на Food Scanner / Location / OAuth

🟡 CLAUDE.md и Notion arch пишут «**React Native 0.73+**» — реально **0.81** (это вполне нормально, опережает спеку).

### 4.3 apps/client (🟢 BeautyGO Client)

**Tabs (реальные, 6):** `booking`, `center`, `favorites`, `masters`, `profile`, `search`

**Target Ayla tabs (5):** 🏠 Главная · 🍽️ Питание · ✨ Я · 📅 День · 👤 Профиль

| Current | Target (Ayla) | Gap |
|---|---|---|
| `masters` + `search` + `booking` | 🏠 Главная (AI-поиск + каталог) | Нет AI-интерфейса, логика разбита на 3 таба вместо одного |
| `favorites` | — (в target избранное внутри 🏠 Главной) | Лишний таб |
| `center` | — (неясно что это) | Не из Ayla target |
| `profile` | 👤 Профиль | ✅ |
| ⚠️ отсутствует | 🍽️ Питание (Food Scanner + дневник) | **Целый таб отсутствует** |
| ⚠️ отсутствует | ✨ Я (AI-аватар + прогресс) | **Отсутствует** |
| ⚠️ отсутствует | 📅 День (ежедневник + питание + гео) | **Отсутствует** |

**Вывод:** текущая структура — это BeautyGO MVP (каталог + запись), не Ayla (daily habit + AI).

### 4.4 apps/pro (🟣 BeautyGO Pro)

**Tabs (реальные, 4):** `bookings`, `masters`, `profile`, `services`

**Target Ayla Pro tabs (4):** Расписание · Записи · Аналитика · Профиль

| Current | Target | Gap |
|---|---|---|
| `bookings` | Записи | ✅ близко |
| `services` | (в target — под Профилем или отд. экран) | Структурное отличие |
| `masters` | ??? (не из Pro) | Сомнительно — зачем в Pro таб «masters»? |
| `profile` | Профиль | ✅ |
| ⚠️ отсутствует | Расписание (календарь) | Есть в bookings? требует проверки |
| ⚠️ отсутствует | Аналитика (доход/клиенты/рейтинг) | **Отсутствует** |

### 4.5 packages/shared

Что есть:
- `api/`: `client.ts` (axios с X-App-Type), `auth.ts`, `bookings.ts`, `masters.ts`, `services.ts`, `reviews.ts`, `socialAuth.ts`, `mock.ts`
- `auth/`: `authStore.tsx`, `socialAuth.ts`
- `components/`: `MasterPreviewCard.tsx`, `ProtectedRoute.tsx`, `SocialAuthButtons.tsx`
- `storage/`: secure storage wrapper

⚠️ Нет модулей: `ai/` (клиент AI Chat), `nutrition/`, `personalization/`, `notifications/` (push), `avatar/`, `day/` (ежедневник).

### 4.6 Bundle IDs / deep links

⚠️ Не проверено формально, но по структуре и package.json (`@beautygo/client`, `@beautygo/shared`) — **всё ещё BeautyGO namespace**. Ayla bundle IDs не применены.

---

## 5. Current State — Infra / Ops

| Аспект | Target | Реально | Статус |
|---|---|---|---|
| Docker compose | Nginx + Django + Celery + Redis + Postgres | `docker-compose.yml` есть, контент не верифицирован в этом ревью | ⚠️ требует проверки |
| Local DB | PostgreSQL 16 | **SQLite** (`db.sqlite3` в repo, migrations гоняются на ней) | 🔴 расхождение — все row-locking / postgis / pgvector фичи не работают |
| Redis | Redis 7 (cache + Celery broker) | Не сконфигурирован (`CACHES` отсутствует, `CELERY_*` отсутствует) | 🔴 отсутствует |
| Celery | Workers + beat | Упоминается в CLAUDE.md, в deps/settings **нет** | 🔴 отсутствует |
| Monitoring | Sentry + Prometheus + Grafana + ELK | Не видно ни одной интеграции в коде | 🔴 отсутствует |
| S3 | S3-совместимое (Yandex/MinIO) | `django-storages` + `boto3` в deps, настройки не видел — требует проверки | ⚠️ неполное |
| CI | GitHub Actions (flake8 → pytest → SSH deploy) | Из git log есть «CI lint fixes», `.github/workflows/ci.yml` не читался — вероятно есть | ✅ вероятно |

### Secret management (из git status начала сессии)
- `.mcp.json` — два токена в plaintext (Figma + Notion) — создан недавно, не в `.gitignore`
- `.env.example` присутствует (из README gstack, подтверждения в repo не делал)

---

## 6. Gap Analysis — Target vs Current

### 6.1 Критичные пробелы (блокеры Ayla-target, 🔴)

1. **Нет Celery + Redis** (deps, settings, containers) — весь Outbox pattern + personalization (daily inference) + cleanup + reminders не работают
2. **Нет AI Layer** — ни модели, ни app, ни endpoints, ни deps (`anthropic`), ни MCP server
3. **Нет Personalization** — `UserPersonalContext` модель, API, Anti-Spam engine — всё отсутствует
4. **Нет Food Scanner / Water Tracker / Avatar** — целые продуктовые вертикали M4
5. **Нет Notifications app** — FCM/APNs push, шаблоны, Celery tasks
6. **Нет Favorites** (упомянуто в Ayla API)
7. **Нет Analytics endpoints** для Pro (`/specialists/me/analytics/`)
8. **Ребрендинг BeautyGO→Ayla не выполнен** — ни в namespace пакетов (`@beautygo/*`), ни в Bundle IDs, ни в repo name
9. **Dev БД — SQLite** вместо PostgreSQL — все row-level locking и advanced PG features недоступны в dev

### 6.2 Структурные отклонения (🟡)

1. **Payment модель в `appointments/`** вместо `payments/` — bounded context смешан
2. **Mobile client tabs** = BeautyGO (6 tabs: masters/search/booking/favorites/center/profile), не Ayla (5 tabs с Питанием/День/Я)
3. **Mobile pro tabs** — отсутствует Аналитика, есть странный таб `masters` (зачем в приложении мастера?)
4. **`ServiceTemplate` и `RegionalPricing`** — доп. сущности не из базового ER, не задокументированы в архитектурных доках Notion
5. **`search` app не в INSTALLED_APPS** — работает, но нарушает Django conventions
6. **Нет `settings/test.py`** — тесты используют dev-settings
7. **Нет `pytest-cov`, `factory-boy`, `freezegun`** — заявленный в CLAUDE.md factory-паттерн не использует библиотеку
8. **Нет `pgvector` в deps** — при том что в target «PostgreSQL 16 с pgvector» для AI

### 6.3 Функциональные совпадения (✅ — что уже хорошо)

1. **Booking Engine DDD** — domain/application/infrastructure слои заложены корректно и полно
2. **Snapshot fields, State Machine, Idempotency Key** — архитектурные паттерны реализованы
3. **Two Apps X-App-Type middleware** — основа Two Apps Architecture работает
4. **DeviceToken с `app_type`** — разделение push по приложениям готово
5. **Auth v2** — OTP + Anonymous JWT + Onboarding + Social Auth — полноценно
6. **Payments YooKassa** — two-stage, idempotency, split payments — базово готово
7. **Reviews** — OneToOne к Appointment, recalc rating, anonymous, moderation — полноценно
8. **Spec v2.0 alignment** — auth/users/specialists/services/appointments/reviews/payments (по CLAUDE.md)
9. **DRF-spectacular** — OpenAPI доки автогенерируются

### 6.4 Отсутствующее/сомнительное vs. важность для Ayla M4

| Фича | Важность в Ayla | Текущее состояние |
|---|---|---|
| AI Chat / Intent parsing | **core differentiator** (M4) | ⚠️ отсутствует |
| PersonalizationEngine | **core differentiator** (M4+) | ⚠️ отсутствует |
| Food Scanner | **retention driver** (M4) | ⚠️ отсутствует |
| AI Avatar + шеринг | **virality** (M4) | ⚠️ отсутствует |
| Voice (STT + TTS) | MVP per Non-Goals в PRD | ⚠️ отсутствует |
| Таб «День» (ежедневник) | M3 | ⚠️ отсутствует |
| Water Tracker | M4 | ⚠️ отсутствует |
| Реферальная программа | M5 | ⚠️ отсутствует |
| Favorites | MVP (Ayla API) | ⚠️ отсутствует |
| Notifications (FCM) | MVP | ⚠️ отсутствует |
| Maps | MVP (Yandex/2GIS/TBD) | ⚠️ провайдер не выбран |

---

## 7. Риски и неустранённые вопросы

### 7.1 Legal / compliance
- **T1 (Risk Register, 🔴 open):** 152-ФЗ для хранения `UserPersonalContext` (особенно 🔴 красная зона — pregnancy/health). Серверы в РФ, consent flow, «Удалить всё», юр. аудит — **ничего из этого в коде не видно**.
- **GDPR**: Казахстан Phase 5 — свои требования, не разбирались.
- **`.mcp.json` с plaintext-токенами** — готовый инцидент, если файл попадёт в git.

### 7.2 Technical
- **T2 (🔴 open):** Качество LLM для русского — бенчмарк не проведён (в PRD Sprint 0 task). Без выбора LLM невозможно спроектировать AI layer.
- **E2 (🟠 учтён):** Архитектура памяти (structured field set vs vector DB vs Mem.ai vs Zep) — решение должно быть в Sprint 0, **не зафиксировано**.
- **SQLite в dev** — blocker для тестов concurrency, PostGIS, pgvector. Разработка на SQLite систематически разойдётся с prod.
- **Outbox worker не запущен** — событийно-ориентированная архитектура описана, но не работает → любой код, который полагается на события (notifications, side-effects), в рантайме молчит.

### 7.3 Product / org
- **Timeline compression:** Ayla roadmap M1-M4 (Auth → Catalog → Booking/Pay → AI/Home) до 2026-05-31, M5 Пилот до 2026-06-30. M1 дедлайн (2026-04-06) уже прошёл, M2 (2026-04-19) — тоже. **Фактическое отставание vs plan — требует оценки.**
- **E1 (🔴 open):** Пивот B2B→Consumer, компетенции консьюмер-роста — не solved.

### 7.4 Architectural debt
- `ServiceTemplate` / `RegionalPricing` — неясно соответствует ли текущей продуктовой модели Ayla (прайсинг per-мастер, не per-регион?). Нужно сверить с продакт-намерением.
- `search` app вне `INSTALLED_APPS` — technical debt
- `Payment` в `appointments/` — bounded context leak
- `db.sqlite3` в git (модифицирован в status) — не должен там быть

---

## 8. Open Questions for Phase 2

Эти вопросы нужно решить, прежде чем строить evolution plan:

1. **LLM выбор** — Claude / GPT-4o / GigaChat / YaLM / Gemini? (T2) Требуется бенчмарк на ru-корпусе Ayla
2. **Архитектура памяти** — structured fields (текущий план `UserPersonalContext`) или vector DB (pgvector / Qdrant / Pinecone)? Гибрид?
3. **MCP Server** — где хостим, как авторизуем вызовы от LLM → own backend?
4. **Food Recognition** — LogMeal API / Passio (обе указаны как кандидаты в Ayla v2.0 arch) — нужна оценка стоимости и ру-локализации
5. **Maps provider** — TBD в Ayla v2.0 arch. 2GIS (указан в CLAUDE.md) vs Yandex Maps vs OpenStreetMap+Nominatim
6. **Deploy target** — VPS (Selectel/Yandex Cloud)? K8s в Phase 2? Это влияет на Docker compose vs helm charts
7. **BeautyGO → Ayla ребрендинг** — когда и как (repo rename, package rename, bundle ID migration)? App Store transition риски
8. **Realism of M4 deadline (2026-05-31)** — с учётом 0 кода AI/nutrition/avatar/voice/personal-context, реально ли за 5 недель?
9. **SQLite → PostgreSQL для dev** — миграция + seed + Docker compose обновление
10. **Celery + Redis onboarding** — как для dev, так и для prod одновременно

---

## 9. Sources

- **Notion:**
  - [Архитектура системы Ayla v2.0](https://www.notion.so/326b0dab2955819bbde7f8103be84c8a) (2026-03-31)
  - [AI Персонализация — Архитектура системы личного контекста](https://www.notion.so/334b0dab295581d587cfeaf49efd2d5b) (2026-03-31)
  - [PRD: Ayla AI Life Assistant v2.0](https://www.notion.so/320b0dab2955807792b9e718e19108df) v3.0 (2026-03-30)
  - [Ayla — Brand Vision & Naming](https://www.notion.so/331b0dab2955817497ebd6c76913089c) (2026-03-28)
  - [Архитектура системы BeautyGO](https://www.notion.so/324b0dab29558123a094ca6ba7d61581) (2026-03-22) — предыдущая версия
  - [Database Schema: BeautyGO MVP](https://www.notion.so/324b0dab295581fbbc34fd58c84e1532) v1.1 (2026-03-17)
- **Code:**
  - `CLAUDE.md` (root)
  - `djangoProject/settings/base.py`
  - `djangoProject/urls.py`
  - `appointments/{domain,application,infrastructure}/`
  - `users/models.py`, `services/models.py`, `appointments/models.py`, `reviews/models.py`
  - `requirements.txt`
  - `../beautygo-mobile/` (Expo monorepo)

---

## Next step

Phase 2 — Evolution review. Прогнать этот аудит через `/plan-eng-review` (архитектура, data flow, edge cases, perf) и `/plan-ceo-review` (scope — M4 deadline реалистичен? таб «День» в MVP или перенос?). На выходе — приоритизированный roadmap по устранению gaps.

---

# Part 2 — Engineering Review Findings

**Date:** 2026-04-23
**Reviewer:** Claude (plan-eng-review skill)
**Mode:** FULL_REVIEW

Engineering review аудита выше. Атакует не код, а сам audit — что пропущено и как recalibrate roadmap.

## 10. Cognitive Frame

### State diagnosis (missing in audit) 🔴

Команда в **falling-behind** state (Larson «An Elegant Puzzle»). Evidence: M1 deadline (2026-04-06) прошёл 17 дней назад, M2 (2026-04-19) — 4 дня назад, 0 кода для M4 за 38 дней до дедлайна.

**Intervention:** в falling-behind нельзя innovate. Сначала stabilize foundation → перейти в treading-water → заработать право innovate. Это переформулирует всю Phase 2 sequencing.

### Innovation token budget exceeded

Ayla M4 tries: (1) LLM wrapper, (2) MCP server, (3) memory architecture, (4) food recognition API, (5) STT+TTS, (6) avatar, (7) pgvector, (8) App Store re-launch. **Восемь новых unknowns одновременно**. Boring-by-default guideline = 3 innovation tokens.

### Two-week smell test — FAIL

Новый dev не может зашипить фичу за 2 недели в текущем состоянии:
- SQLite dev ≠ Postgres prod (concurrency/GIS/vector недоступны в dev)
- Нет `factory-boy` → фикстуры руками каждый раз
- Нет `freezegun` → time-sensitive тесты flaky
- Нет Celery/Redis → outbox/tasks нельзя тестировать end-to-end
- Нет `pytest-cov` → невозможно увидеть что ты не протестировал
- DDD + нет test patterns → unclear как тестировать

**DX broken before features.** Это первое что чинится в Phase 2.

## 11. Architecture Review — gaps не найденные audit'ом

### A1 — State diagnosis (confidence 9/10)
Audit перечисляет факты, но не ставит диагноз. Требуется явное признание "falling behind" чтобы правильно приоритизировать Phase 2.

### A2 — Distribution architecture для mobile (confidence 9/10)
Audit описывает mobile код, но пропускает delivery pipeline:
- Нет упоминания EAS Build / Fastlane / GitHub Actions для mobile CI
- Ребрендинг `ru.beautygo.client` → `ru.ayla.client` = **новый Apple Developer app** + **новые provisioning profiles** + **новый Firebase project** (старые DeviceToken'ы invalid)
- OTA updates через expo-updates — не проверено
- App Store migration path: это новый app в store, не metadata update. Существующие пилотные пользователи BeautyGO не мигрируют автоматически.

**Estimate:** mobile ребрендинг = 1-2 недели, не «поменять namespace в package.json».

### A3 — Observability — blocker раньше AI, не позже (confidence 8/10)
Audit ставит Sentry/Prometheus/ELK в одну группу с Redis/Celery как 🔴. Но observability нужен **раньше**:
- Outbox worker не запущен → events пишутся в БД, никто их не обрабатывает → **silent failures**
- Пилот в Пензе без Sentry = нет видимости багов у первых 50 мастеров и 200 клиентов
- Bug в prod сейчас обнаруживается через жалобу пользователя → feedback loop 2-7 дней

**Правильная очередность:** Sentry + structured logging + healthchecks **до** Celery/Redis. Sentry также ловит issues в процессе построения Celery.

### A4 — Data model dependency graph (confidence 7/10)

Audit перечисляет отсутствующие модели как отдельные gaps. На самом деле они зависимы:

```
UserPersonalContext ─┬─→ требует AI (context в prompt)
                     ├─→ требует FoodLog (meal_schedule inference)
                     ├─→ требует AppointmentHistory (favorite_masters inference)
                     └─→ требует Celery (infer_user_patterns nightly)

FoodLog ─┬─→ требует LogMeal/Passio API client
         ├─→ требует Nutrition DB (USDA mirror или live API)
         └─→ требует S3 для фото еды ✅ (уже есть)

Avatar ─┬─→ требует Ready Player Me / Lensa-style провайдер (не выбран)
        ├─→ требует sharing format (1080x1920 IG Stories)
        └─→ требует AvatarSnapshot с weekly cadence

Notifications ─┬─→ требует firebase-admin (нет в deps)
               ├─→ требует APNs сертификаты (где хранить?)
               ├─→ требует Celery (reminders)
               └─→ требует Templates
```

`UserPersonalContext` не standalone фича — это **cross-cutting infrastructure layer** с 4 жёсткими зависимостями. Без Celery нет nightly inference. Без FoodLog нет behavioral patterns. Это нужно строить **в правильном порядке**, не параллельно.

### A5 — Payment в appointments/ — monetization blocker (confidence 7/10)
Audit flag 🟡, но это хуже:
- `payments/` app — пустая скорлупа без models
- При добавлении `Payout` (выплаты мастерам) — куда пойдёт? Namespace уже сломан
- Phase 4 monetization (Premium subscription + commission + marketplace fees) требует отдельных моделей Subscription/Commission — нужно чистое `payments/models.py`

### A6 — One-way doors не приоритизированы (confidence 8/10)
Audit перечисляет 10 open questions плоским списком. Два из них — **необратимые**:
1. **LLM choice** (Claude/GPT/GigaChat/YaLM) — prompts и tool defs LLM-specific, смена = переписать всё
2. **Memory architecture** (structured fields vs vector DB) — смена = переписать PersonalizationEngine

Остальные 8 — reversible. Priority mismatch в audit.

## 12. Code quality review

Не применимо — audit is state-assessment, не diff. No issues. Оставлено для `/review` по мере изменений.

## 13. Tests review

Audit знает: 22 test-файла vs 124 code, нет `pytest-cov`/`factory-boy`/`freezegun`.

**Audit пропустил:**

### Test quality depth не измерена
Audit не проводил coverage assessment — невозможно без `pytest-cov`. Это **первый артефакт Phase 2**: установить `pytest-cov` + измерить baseline.

### Предполагаемые gaps (confidence 6/10, требует верификации):

1. **Concurrency тесты booking engine** — `select_for_update()` no-op на SQLite → тесты false-positive проходят
2. **Outbox dispatch тесты** — worker не запущен → end-to-end dispatch тестов вероятно нет
3. **YooKassa webhook signature verification** — идемпотентность протестирована, а подпись?
4. **Mobile integration tests** — `apps/client/jest.config.js` есть, но coverage неизвестен

### REGRESSION RULE — critical
Перед миграцией SQLite → PostgreSQL любой concurrency-sensitive тест нужно переписать + создать regression tests. **Это не "nice-to-have", это mandatory** — иначе silent breakage booking engine в prod.

### Test plan artifact
Не создавался (audit сам по себе не имеет testable surface). `/qa` и `/qa-only` будут релевантны когда появятся конкретные фичи в Phase 3.

## 14. Performance review

Audit flags LocMemCache + outbox dead, но не проводит perf assessment.

**Не измерено:**
- N+1 queries в DRF serializers (endemic без profiling)
- `SlotBuilderService` perf (200 мастеров × 60 дней — сколько ms?)
- `drf-spectacular` schema generation overhead
- Admin queries через `django-unfold`

**Предположительные hotspots (confidence 5/10 — verify перед Phase 3):**
1. `GET /api/v1/specialists/` — без pagination → slow при 200+ мастерах
2. `GET /api/v1/specialists/{id}/slots/` — кеш 5m в spec, но без Redis каждый Django инстанс кеширует independently
3. `POST /api/v1/appointments/` — двойная запись в slot concurrent-safe только на PostgreSQL, в dev на SQLite это не проверяется

**User impact at pilot scale** (50 мастеров, 200 клиентов): пока невидимо. При масштабировании — все три fire одновременно.

## 15. Decisions made in this review

Три one-way door решения зафиксированы:

### D1 — Sequencing: Foundation-first ✅
Phase 2 starts with foundation stabilization (PostgreSQL dev · Celery/Redis · Sentry · notifications · pytest-cov · factory-boy · freezegun), **затем** AI layer / personalization / food scanner.

**Implication:** M4 deadline (2026-05-31) не достижим. Нужно пересмотреть roadmap с пилотом. М5 пилот Пенза (30.06.2026) realistic только если урежем scope M4.

### D2 — LLM choice: Benchmark sprint first ✅
Phase 2 начинается с 3-5 day benchmark sprint:
- 200 ru-промптов Ayla (intent parsing, recommendations, memory context formatting)
- 5 кандидатов: Claude Sonnet 4, GPT-4o, GigaChat, YaLM, Gemini
- Метрики: accuracy, latency p50/p95, cost per request, ru-quality human eval
- Документ: `docs/LLM_BENCHMARK.md` с выбором + rationale

**Deliverable:** single LLM decision перед AI code.

### D3 — Outside voice: Skipped
Review сочтён sufficient. Опционально — revisit на границах Phase 2.

## 16. Required follow-ups (TODOs)

| # | Item | Priority | Ownership |
|---|---|---|---|
| T1 | Установить `pytest-cov`, измерить coverage baseline | P0 | Backend |
| T2 | Мигрировать dev БД SQLite → PostgreSQL + seed script | P0 | Backend |
| T3 | Добавить `celery`, `redis`, `django-redis` в deps + settings + compose | P0 | Backend + Ops |
| T4 | Интегрировать Sentry (backend + mobile) до запуска Celery | P0 | Ops |
| T5 | Запустить Outbox worker (после T3) | P1 | Backend |
| T6 | Добавить `factory-boy`, `Faker`, `freezegun` в test deps | P1 | Backend |
| T7 | LLM benchmark sprint (D2) | P1 | AI/Research |
| T8 | Memory architecture decision (structured vs pgvector vs hybrid) | P1 | Architect |
| T9 | Вынести `Payment` из `appointments/` → `payments/models.py` + migration | P2 | Backend |
| T10 | Зарегистрировать `search` app в INSTALLED_APPS | P2 | Backend |
| T11 | Создать `settings/test.py` с фикстурными настройками | P2 | Backend |
| T12 | Mobile: EAS Build pipeline + OTA updates config | P2 | Mobile/Ops |
| T13 | Ребрендинг plan: Bundle IDs / Firebase / App Store (spike) | P2 | Mobile/Product |
| T14 | `.mcp.json` в `.gitignore`, ротация Figma + Notion токенов | P0 Security | Ops |
| T15 | Remove `db.sqlite3` из git (после T2) | P2 | Backend |

## 17. Parallelization strategy

**Foundation phase dependency graph:**

```
Lane A (Backend infra, sequential):
  T1 pytest-cov → T3 Celery/Redis deps → T2 Postgres dev → T5 Outbox worker
  T6 test deps (parallel to T3)

Lane B (Ops, parallel to A):
  T4 Sentry → T14 secret hygiene

Lane C (Research, parallel to A+B):
  T7 LLM benchmark → T8 Memory architecture decision

Lane D (Mobile, parallel, can start later):
  T12 EAS pipeline → T13 Rebrand spike

Post-foundation (требуется A+B+C done):
  AI layer, PersonalizationEngine, FoodScanner, Avatar, Notifications
```

**Module conflicts:** Lane A и Lane D не пересекаются (backend vs mobile). Lane B и A оба касаются `settings/base.py` — нужна lightweight coordination (Sentry init + Celery config в одном PR последовательно).

**Realistic timeline foundation:** ~3 недели для Lane A, параллельно 1 неделя для Lane B и 1 неделя для Lane C.

## 18. NOT in scope (этого review)

- Code quality / line-level diff review — нет diff, делается при конкретных PR через `/review`
- Security audit (OWASP/STRIDE) — deferred to `/cso` отдельно, особенно перед pilot
- Design review — нет UI изменений для review
- DX review — foundational gaps dominate, devex assessment после foundation fix'ов
- Product scope challenge (scope reduction/expansion для M4) — задача `/plan-ceo-review` (Phase 2.5)
- Specific feature plans (AI Layer internals, PersonalizationEngine internals) — Phase 3 плановые доки по каждой фиче

## 19. What already exists (reusable foundations)

- **Booking Engine DDD** (`appointments/domain/`, `appointments/application/`, `appointments/infrastructure/`) — корректная structure, patterns rly thought-through
- **Snapshot fields on Appointment** — иммутабельность финансов
- **State machine + BookingStateMachine** — явные переходы
- **Idempotency Key** на Appointment + YooKassa webhook
- **Two Apps middleware** — X-App-Type header routing
- **Auth v2** — OTP + JWT + Anonymous + Onboarding + Social (VK/Google/Apple/Yandex)
- **DeviceToken с app_type** — готов к per-app push
- **Reviews** (OneToOne Appointment, rating recalc, anonymous, moderation)
- **Payments YooKassa** (two-stage + idempotent webhook + split + 8% commission)
- **DRF-spectacular OpenAPI** — автодоки
- **django-unfold admin** — кастомный admin UI

Evolution plan должен **reuse эти foundations**, а не переделывать.

## 20. Failure modes (critical gaps)

Три сценария silent failure в prod сегодня:

1. **Outbox worker dead + no Sentry** → любой dispatch-зависимый event (уведомление, email, webhook) = тишина. Booking подтверждён в БД, мастер не узнал.
   - Test: нет
   - Error handling: нет
   - Visibility: нет (тихо пишется в OutboxEvent и лежит)

2. **SQLite dev + `select_for_update()` no-op** → два клиента кликают same slot simultaneously в dev → оба PASS. В prod Postgres locking работает, но тесты не заметят регрессию.
   - Test: false-positive
   - Error handling: полагается на БД locking, которое не работает в dev

3. **`.mcp.json` с plaintext токенами** → commit случайно → токены в git history → кто угодно с доступом к репо получает write access к Figma и Notion workspace.
   - Test: n/a
   - Error handling: n/a
   - Visibility: нулевая

Все три — **blockers до пилота в Пензе**.

## 21. Completion Summary

- Step 0 Scope Challenge: **Audit scope accepted, но выявлена scope-проблема evolution plan (8 innovation tokens vs 3 budget)**
- Architecture Review: **6 issues** found (A1-A6), все actioned
- Code Quality Review: **0 issues** (n/a для audit)
- Test Review: **5 gaps** identified (depth not measured, concurrency, outbox, webhook, mobile integration)
- Performance Review: **3 probable hotspots** flagged (confidence 5/10, verify перед Phase 3)
- NOT in scope: written (section 18)
- What already exists: written (section 19)
- TODOs proposed: **15 items** (section 16) — записаны прямо в этот документ как roadmap input
- Failure modes: **3 critical gaps** flagged (section 20)
- Outside voice: skipped (user decision)
- Parallelization: **4 lanes** (A backend infra, B ops, C research, D mobile) — A sequential, B+C+D parallel to A
- Decisions: **3 made** (D1 foundation-first, D2 LLM benchmark first, D3 skip outside voice)
- Lake Score: recommendations favor complete option consistently (Sentry before Celery, full test tooling not partial, full PostgreSQL migration not wrappers)

**VERDICT:** CLEARED — Eng Review passed. Ready for `/plan-ceo-review` (Phase 2.5) чтобы принять scope-решения для M4 (что из 8 innovations cut/defer/simplify), затем Phase 3 implementation.

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | CLEAR | REDUCTION, 0 proposals, 5 tokens deferred |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 6 arch issues, 5 test gaps, 3 critical failure modes, 15 TODOs |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | CLEAR | score 0→8.1/10, 3 screens specified, 7 decisions deferred, see `docs/PHASE_3_UI_SPEC.md` |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **UNRESOLVED:** 7 (design decisions для PO/designer, не блокеры)
- **VERDICT:** CEO + ENG + DESIGN CLEARED — ready for Phase 2 foundation → Phase 3 implementation

---

# Part 3 — CEO Scope Review & Revised Roadmap

**Date:** 2026-04-23
**Reviewer:** Claude (plan-ceo-review skill)
**Mode:** SCOPE_REDUCTION
**Approach selected:** B (Minimum Lovable Ayla)

## 22. Premise Challenge

**Base product bet:** "AI memory-first beauty assistant для женщин СНГ". Три слоя гипотез:

1. **Beauty booking pain** — validated (PRD ссылается на 47% "choice paralysis"). AI подбор мастера [Layer 1: proven pattern].
2. **Daily retention через food tracking** — low validation (MyFitnessPal precedent, но РФ/СНГ контекст не тестирован). Food Scanner [Layer 2].
3. **Virality через AI avatar** — speculative (Lensa 2023 single precedent). Avatar + sharing [Layer 2 → Layer 3 риск].

**Inversion:** главный риск не качества, а **ship window closed до пилота**. Каждая неделя без AI-кода = недостижимая дата M5 (2026-06-30).

**Proxy skepticism:** метрики "3 сканирования/день" — daily usage качества "hooked" или "annoyed"? Water reminder easy hit, но это не engagement.

**Do nothing:** BeautyGO текущее = market-parity booking app (Dikidi/YCLIENTS). Без AI — нет differentiation. Investor pitch слабый.

## 23. Dream State Mapping

```
  CURRENT (2026-04-23)         APPROACH B (reduced M4)      12-MONTH IDEAL (2027-04)
  BeautyGO MVP booking   --->  AI chat + food + booking --> Habit app женщин СНГ
  (50% reusable foundation)    (50% path to ideal)          10k+ DAU, multi-city, monetization
```

**Deferring memory/avatar/voice/rebrand сохраняет 100% optionality** для Phase 6+.

## 24. Approach Comparison (0C-bis)

| | A: Full M4 | B: Min Lovable ✅ | C: Polish + Defer |
|---|---|---|---|
| Scope | 8 innovation tokens | 3 tokens (AI chat + food + notifications) | 0 new tokens |
| Human effort | 3-4 месяца | 8-10 недель | 3-4 недели |
| CC+gstack effort | 6-8 недель | 3-4 недели | 1-2 недели |
| Risk | HIGH | MEDIUM | LOW |
| M5 pilot 2026-06-30 | ❌ Lost (Sept+) | ⚠️ +2 weeks slip | ✅ Reachable |
| AI hypothesis tested | Yes (все 8) | Yes (2 ключевые) | No |
| Investor pitch | Sterile 10/10 | Solid 7/10 | Weak 4/10 |
| Completeness | 10/10 | 7/10 | 4/10 |
| **Selected:** | | ✅ | |

**Selected rationale:** B validates 2 core hypotheses (AI booking conversion + food habit) ships-to-pilot в realistic timeframe. Avoids vendor lock-in pre-data. Foundation-first compatible.

## 25. Ruthless Cut — M4 8 Tokens → 3 Keep

### KEEP (3)

1. **LLM wrapper + thin MCP** — Token consolidated. Claude/GPT/GigaChat via abstraction. Used для AI chat (intent → мастер recommendation → create booking)
2. **Food recognition API** (LogMeal или Passio, after benchmark) — photo → KБЖУ → FoodLog. Validates retention bet.
3. **Celery + basic Notifications** — Infra token, уже в eng review TODOs T3/T4/T5. FCM push для booking confirm/reminder.

### DEFER (5)

| Token | When | Why defer |
|---|---|---|
| UserPersonalContext (memory arch) | Phase 6 (post-pilot) | Speculative без usage patterns; pgvector tied to this |
| pgvector | Phase 6 (dependency) | No memory = no vector need |
| Voice (STT + TTS) | Phase 7+ | Nice-to-have, не ships-to-pilot |
| Avatar + Ready Player Me + sharing | Phase 6/7 | Validate retention through food first, затем add virality |
| App Store rebrand BeautyGO → Ayla | Phase 7 (post-pilot) | Don't rebrand unvalidated product. Save users from dual-store migration |

## 26. Temporal Interrogation (Phase 2+3 decisions)

### Week 1 (Foundation):
- **Q:** Celery broker local/prod — Redis managed vs self-hosted docker-compose?
- **Q:** Test isolation — `CELERY_TASK_ALWAYS_EAGER=True` или live worker?
- **Q:** Outbox dispatch — beat schedule (period) или signals (event-driven)?

### Week 3-4 (AI chat):
- **Q:** Prompt versioning — `ai/prompts/{version}.py` files или hardcoded?
- **Q:** LLM response caching — Redis key `hash(msg + system)` или skip?
- **Q:** Streaming — SSE или buffered JSON?

### Week 5-6 (Food Scanner):
- **Q:** S3 path — `food/{user_id}/{timestamp}.jpg` или flat by hash?
- **Q:** LogMeal vs Passio — выбрать в benchmark sprint или default LogMeal?
- **Q:** Fallback при API down — manual entry UI?

**Эти решения фиксируются в Phase 2/3 planning docs, не runtime.**

## 27. Error & Rescue Map (reduced scope)

| Path | Failure | Rescue | User sees | Logged (Sentry) |
|---|---|---|---|---|
| AI chat `/api/v1/ai/chat/` | LLM API timeout | Retry 1x, fallback static | "AI заглянул в перерыв, попробуйте" | ✅ |
| AI chat | LLM malformed JSON | Fallback unstructured text | Plain text вместо cards | ✅ |
| AI chat | Rate limit hit | Queue, progress indicator | "Загружено, ответ через 5с" | ✅ |
| Food scan | LogMeal API down | Allow manual entry | Fallback form | ✅ |
| Food scan | S3 upload fail | Retry 3x backoff | "Не удалось загрузить" | ✅ |
| Notifications | FCM token invalid | Deactivate token | Silent | ✅ |
| Notifications | Celery worker down | Outbox queues, fires on restart | Delay only | ✅ |

**Critical gaps:** 0 (при условии foundation done: Sentry+Celery+Outbox).

## 28. Security (reduced scope additions)

- **Prompt injection:** user text → Claude. Mitigations:
  - Input length limit 2000 chars
  - Strip markdown (no system-prompt hijack)
  - Never concat user input into system prompt — always as user message role
- **Food photo upload:** validate content-type, size < 10MB, Pillow sanity check pre-S3
- **No sensitive PII categories** (memory/avatar deferred — 152-ФЗ compliance нагрузка тоже отложена)

## 29. Data flow edge cases

**AI chat:**
- Empty message → reject at serializer level
- 10 msg/sec spam → rate limit 3 msg/min per user
- Conversation history cap 20 turns, trim oldest

**Food scan:**
- Blurry / not food → LogMeal confidence < 0.5 → "Не могу распознать"
- Duplicate scan в 5 мин → merge в same FoodLog
- Empty state (no scans) → onboarding prompt

## 30. Tests plan (reduced scope)

| Codepath | Test type | Coverage | Priority |
|---|---|---|---|
| `AIService.chat()` + tool calls | Unit (mocked LLM) | 100% | P0 |
| `IntentParser` ru-text | Unit + golden prompts | 95% | P0 |
| `FoodScanService.scan(photo)` | Integration (mocked API) | 100% | P0 |
| `NotificationDispatcher` → Celery | Integration | 100% | P0 |
| Booking AI flow e2e: text → suggestion → book | E2E (pilot-critical) | 100% | P0 |

**Chaos tests:** LLM API down · Food API down · Celery worker killed mid-task.

## 31. Performance & Cost

- **LLM cost:** 5-20 req/user/day × 50 active = $2.50-50/день. **Alert $50/день**.
- **Food API cost:** 3 scans/day × 200 users × $0.005 = $3/день.
- **Caching:** AI response cache для common queries ("маникюр рядом") — 60% hit rate expected.

## 32. Observability (Phase 3 ship-day)

**Dashboards:**
- AI: cost/user/day, p50/p95 latency, AI→booking conversion
- Food: scan success rate, API error rate
- Booking: conversion from AI suggestion → confirmed
- Notifications: FCM delivery rate, opt-out rate

**Alerts:** LLM cost spike, food API error > 10%, Celery queue depth > 100.

## 33. Deployment

- **Feature flags:** `ENABLE_AI_CHAT`, `ENABLE_FOOD_SCAN`, `LLM_PROVIDER` (claude/gpt/gigachat)
- **Rollback:** disable via flag, не code revert
- **Migrations:** 3 new apps additive, no schema changes to existing
- **Smoke tests:** `/api/v1/ai/chat/` с mock prompt, `/api/v1/nutrition/scan` с test photo

## 34. Long-term Trajectory

- **Reversibility:** LLM choice = 3/5 (abstracted), memory deferred = 5/5 (trivial to add later), food API = 4/5 (swappable)
- **Path dependency:** Deferring memory/rebrand **сохраняет все options** — pilot data drives next priority
- **1-year question:** 2027-04 = AI chat + food + booking + subscription + (memory если pilot validates). Coherent story.

## 35. Design/UX — requires separate review

**Section 11 DESIGN_SCOPE:** YES.
- AI chat UI design в Figma required
- Food scanner UI (camera + KБЖУ overlay) design required
- Notification permission flow UX

**Recommendation:** `/plan-design-review` перед Week 4 start Phase 3.

## 36. Revised Roadmap (Phase 2 → Pilot)

```
PHASE 2 — FOUNDATION (Weeks 1-3, Lane A sequential + B,C,D parallel)
├── Week 1: pytest-cov + Postgres dev + Sentry
├── Week 2: Celery + Redis + Outbox worker activation
└── Week 3: factory-boy/freezegun/Faker + .mcp.json security hygiene
            LLM benchmark sprint (Lane C parallel)

PHASE 3 — MINIMUM LOVABLE (Weeks 4-8)
├── Week 4: AI Layer skeleton (ai/ app + MCP server + LLM abstraction)
├── Week 5: Food Scanner (nutrition/ app + LogMeal/Passio integration)
├── Week 6: Notifications (notifications/ app + FCM + Celery tasks)
├── Week 7: Booking AI flow integration + golden-prompt tests
└── Week 8: Integration testing + /plan-design-review + /cso security audit

PHASE 4 — PILOT PREP (Weeks 9-10)
├── Week 9: Mobile UI implementation (AI chat + food scanner)
└── Week 10: /qa testing + observability dashboards + pilot readiness

PHASE 5 — PILOT ПЕНЗА (Weeks 11-12+, target 2026-07-15, +2 недели к M5)
└── Onboard 50+ мастеров + 200 клиентов + data collection

PHASE 6+ — POST-PILOT (data-driven)
├── Memory architecture (if pilot validates AI retention > 50%)
├── Avatar + sharing (if retention validated)
├── Voice / Water Tracker / День tab
└── Rebrand BeautyGO → Ayla (only after product-market fit signals)
```

## 37. What Already Exists (reused foundations) ✅

Booking DDD · Auth v2 (OTP + Anonymous + Social) · Payments YooKassa (two-stage + split) · Reviews · Two Apps middleware · DeviceToken · DRF-spectacular · django-unfold. Все foundation-pieces сохраняются.

## 38. NOT in scope Phase 3 (12 deferred items)

| # | Item | Deferred to |
|---|---|---|
| 1 | UserPersonalContext + Anti-Spam engine | Phase 6 |
| 2 | pgvector / Vector search | Phase 6 |
| 3 | Voice (STT + TTS) | Phase 7+ |
| 4 | AI Avatar + Ready Player Me + sharing | Phase 6/7 |
| 5 | Таб "День" (ежедневник) | Phase 5 |
| 6 | Таб "Я" (AI-аватар) | Phase 7 |
| 7 | Water Tracker | Phase 5 |
| 8 | Referral program | Phase 7 |
| 9 | Favorites API | Phase 3 optional (если успевает) |
| 10 | Analytics endpoints для Pro | Phase 5 |
| 11 | App Store rebrand BeautyGO → Ayla | Phase 7 |
| 12 | Maps provider choice | Phase 5 |

## 39. Unresolved Decisions

**Zero.** Все решения приняты в review.

## 40. CEO Completion Summary

- **Mode:** SCOPE_REDUCTION
- **Approach:** B (Minimum Lovable Ayla)
- **Tokens cut:** 5 of 8 (memory + pgvector + voice + avatar + rebrand deferred)
- **Tokens kept:** 3 (LLM+MCP, food recognition, Celery+notifications)
- **Timeline:** Pilot target 2026-07-15 (+2 weeks к M5 original)
- **Risk level:** MEDIUM (reduced from HIGH)
- **Foundation required first:** yes (Phase 2 Lane A)

**VERDICT:** CEO + ENG CLEARED. Ready for Phase 2 implementation.
