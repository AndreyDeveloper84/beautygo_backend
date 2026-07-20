# Спринт 2 — Staging Setup: провижининг и чеклисты (v2)

Приложение к `docs/SPRINT_2_STAGING_INTEGRATION.md` (v2). Переработано по review:
устранён конфликт портов, одна модель управления, security baseline, размеры,
домены, Mini App deploy, политика ПДн. Дата: 2026-07-20.

## 1. Провижининг staging (S0)

### 1.1. Порты и процессы (без конфликтов)

| Сервис | В контейнере | Host binding |
|---|---|---|
| Ayla web | 8000 | 127.0.0.1:8000 |
| Bot web | 8000 | 127.0.0.1:8001 |
| ChromaDB | 8000 | 127.0.0.1:8002 (или не публиковать) |
| Bot PostgreSQL | 5432 | не публиковать (или 127.0.0.1:5433) |
| Bot Redis | 6379 | не публиковать (или 127.0.0.1:6380) |
| MinIO API | 9000 | 127.0.0.1:9002 (только если нужен host) |
| MinIO Console | 9001 | 127.0.0.1:9003 |

PostgreSQL, Redis, Chroma, MinIO — никогда наружу через 0.0.0.0.

**Модель управления — одна:** `systemd → docker compose up/down` всего проекта.
Никаких отдельных systemd-юнитов для web/worker/beat параллельно с compose
(иначе двойные задачи/напоминания/списания).

### 1.2. docker-compose.staging.yml (обязателен)

Ключ `-p` не изолирует: фиксированные `container_name`, host-порты, внешние
networks, именованные volumes, общий `.env`. Нужен override-файл:

```bash
docker compose -p ayla-bot-staging \
  -f docker-compose.yml -f docker-compose.staging.yml up -d
# Перед запуском — проверка итоговой конфигурации:
docker compose -f docker-compose.yml -f docker-compose.staging.yml config
```

В override: уникальные container_name/volumes/networks, порты из §1.1,
staging env-файл (не общий `.env`).

### 1.3. Размеры хоста

Минимум для стабильного стенда (2 backend + 2 PG + 2 Redis + MinIO + Chroma +
workers + beat + nginx): **4 CPU / 8 GB RAM / 60–80 GB SSD / swap 2–4 GB**
(страховка, не основная память). Если компонент не нужен сценариям спринта
(Chroma/MinIO), — не поднимать.

### 1.4. Домены и маршрутизация

Рекомендуемые имена (иначе — явная routing-таблица по существующим):

```
api-staging.gobeauty.site → Ayla web (127.0.0.1:8000)
bot-staging.gobeauty.site → bot web (127.0.0.1:8001)
app-staging.gobeauty.site → Mini App статика
```

### 1.5. Mini App deploy (S0.4)

`npm ci` → `npm run build` → `dist/` → nginx статика: cache headers (immutable
для хэш-ассетов), без source maps в проде, CSP по чеклисту §4.3, переменные
сборки (`VITE_API_BASE_URL`, `VITE_SUPPORT_DEEPLINK`), проверка авторизации
MAX launch context на app-staging.

## 2. Чеклист E2 — тестовый магазин ЮKassa

1. Тестовый магазин: `shopId` + `secret key` (test) — в env staging, не в код.
2. Webhook URLs:
   - payments: `https://api-staging…/api/v1/payments/webhook/`
   - billing: `https://api-staging…/api/v1/internal/billing/webhook/` (AMD-014;
     **оба пути — AppType-exempt, защита = IP allowlist + Basic, НЕ Bearer**).
3. События webhook: payment.succeeded / canceled / waiting_for_capture,
   refund.succeeded.
4. Тестовые карты (доки ЮKassa): успешная, отказ, без 3DS (для автоплатежа).
5. Split: проверить, что тест-магазин поддерживает transfers; верифицировать
   `YOOKASSA_AGENT_ID` против реального кода и договорной схемы (см. Gate 3).

## 3. Чеклист E3 — MAX staging-бот

1. Создать staging-бота → `MAX_BOT_TOKEN` (в env bot staging).
2. Подписка webhook через API → `https://bot-staging…/api/v1/ingress/`;
   проверить актуальный API host MAX (platform-api2.max.ru) в bot-platform.
3. Mini App: зарегистрировать `https://app-staging…` (MAX_MINIAPP_URL).
4. Replay/дубликаты webhook; старые подписки удалить.
5. Проверка: echo-сообщение → ответ; ingress без 4xx.

## 4. Security baseline (обязателен до smoke)

### 4.1. Сеть и секреты

- Наружу: 80, 443, 22 (allowlist). Закрыты: PostgreSQL, Redis, MinIO, ChromaDB,
  Celery-monitoring.
- Секреты: отдельные staging; запрет копий production `.env`; права 0600;
  вне Git; ротация после спринта; не выводить в CI/логи; отдельные креды на
  каждую БД; разные `DJANGO_SECRET_KEY`. «Один секрет» — только на одно
  соединение (Ayla outbound ↔ bot ingest).

### 4.2. Доступ по зонам

- Provider webhooks (ЮKassa, MAX): публичные пути со своей защитой (IP/подпись).
- Mini App: подписанный MAX-контекст.
- Internal API: Bearer/HMAC.
- Админки: VPN/IP allowlist/Basic Auth.
- `/health/` (live): минимальный ответ; `/health/ready/`: сеть/авторизация.

### 4.3. Web-security чеклист

`DEBUG=False`; точные `ALLOWED_HOSTS`; CORS allowlist; CSRF trusted origins;
secure cookies; HSTS (после проверки); CSP для Mini App;
`X-Content-Type-Options`; `Referrer-Policy`; запрет индексации staging
(robots/X-Robots-Tag); отдельный Sentry environment `staging` + release SHA;
скрытие stack traces.

## 5. Чеклист KYC — на мастера (per-master)

Статусы: `not_started → submitted → verification_pending → active` (+ rejected /
blocked). В пилот — только `active`.

1. Данные: ФИО, телефон, специализация; статус (самозанятый/ИП), ИНН, реквизиты.
   **ИНН/реквизиты не попадают в логи/Sentry/отчёты.**
2. ЮKassa суб-аккаунт (split per-master): онбординг-форма → активация →
   `yookassa_account_id` → `SpecialistProfile` (users/0014).
3. Профиль, услуги, расписание (working hours), фото.
4. Онбординг-звонок 15 мин: кабинет, подписка 690₽, привязка карты (W2-флоу),
   записи/выплаты.
5. Тест-запись: создана → confirmed → complete → **два раздельных сценария
   проверки:** online → split 90₽ платформе, income = цена − 90₽ мастеру;
   offline → BookingFee 90₽ ровно один раз (никогда оба — AYLA-DEC-0010).

## 6. Спека тестовых данных staging

- Tenant: салон «Формула тела» + 1 соло-мастер (tenant-NULL проверки, AMD-015).
- Услуги: 20–30 реальных из canonical seed с `requires_health_check`/длит.;
  знаменатель coverage — активные bookable услуги пилотных мастеров.
- Расписание: 2 недели вперёд + 1 выходной (краевой случай слотов).
- Клиенты: 2 синтетических (с memory_green и без — гейт памяти).
- Платёжное: 1 мастер с картой + active-подпиской; 1 — past_due (для C1).
- ПДн: синтетика предпочтительна; настоящие мастера — по политике §документа
  спринта (минимизация, журнал доступа, срок удаления, план очистки).

## 7. Открытые пункты (за владельцем)

- E1: вариант хоста (тот же VPS / отдельный) + подтверждение `dev.gobeauty.site`.
- E2: кто выполняет §2 в кабинете ЮKassa.
- E3: кто создаёт MAX staging-бота (§3, шаг 1).
- E4: именованный список мастеров (≥15) для §5/KYC-трека.
