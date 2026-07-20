# Спринт 2 — Staging Setup: провижининг и чеклисты

Приложение к `docs/SPRINT_2_STAGING_INTEGRATION.md` (S0–S1, E2–E4).
Дата: 2026-07-20. Основание: deploy.sh, docker-compose*.yml, nginx/dev.gobeauty.site.conf (beautygo_backend); infra/deploy, infra/systemd (ai-bot-platform).

## 1. Провижининг staging (S0)

### 1.1. Вариант A — тот же VPS (быстрый путь, если E1 подтверждает `dev.gobeauty.site` как staging Ayla)

1. Ayla backend: деплой по существующему `deploy.sh` (ветка `dev`, gunicorn :8000).
2. Bot staging: на том же VPS — отдельный compose-проект из `ai-bot-platform/docker-compose.yml`:
   - `docker compose -p ayla-bot-staging up -d` (web :8001, worker, beat, postgres :5433, redis :6380, chromadb :8001, minio :9002 — порты смещены от основного проекта).
   - systemd-юниты по образцу `infra/systemd/` (web/worker/beat, env-файл отдельный).
3. nginx: `staging.gobeauty.site` → bot web :8001 (конфиг по образцу `nginx/dev.gobeauty.site.conf`); TLS через существующий certbot-процесс.
4. Проверка: `/health/` и `/health/ready/` на обоих; Celery worker+beat живы (`celery inspect ping`).

### 1.2. Вариант B — отдельный хост (если VPS нет/занят)

- Минимум: 1 VPS (2 CPU / 4 GB). Порядок: Ayla (compose) → bot (compose) → nginx → TLS → §2 env-инвентарь.

### 1.3. Env-инвентарь (по F0–F11 из runbook §3, заполняется до smoke)

| Где | Ключ | Значение |
|---|---|---|
| Ayla | `AYLA_INTERNAL_API_TOKEN` | общий секрет (генерируем один, обе стороны) |
| Ayla | `BOT_PLATFORM_BASE_URL` | `https://staging.gobeauty.site` |
| Ayla | `AYLA_OUTBOUND_HMAC_SECRET` | = bot `EVENT_INGEST_HMAC_SECRET` (одна пара) |
| Ayla | `YOOKASSA_SHOP_ID/SECRET_KEY` | тест-магазин (E2) |
| bot | `AYLA_BASE_URL` | `https://dev.gobeauty.site` (или staging Ayla) |
| bot | `EVENT_INGEST_HMAC_SECRET` | = Ayla outbound (O1 закрыт в коде — ddf9818) |
| bot | `MAX_BOT_TOKEN` | staging-бот (E3) |
| bot | `MAX_MINIAPP_URL` | staging miniapp URL |

## 2. Чеклист E2 — тестовый магазин ЮKassa

1. В кабинете ЮKassa создать тестовый магазин (если нет): взять `shopId` + `secret key` (test).
2. Webhook URLs (2):
   - payments: `https://<ayla-staging>/api/v1/payments/webhook/`
   - billing: `https://<ayla-staging>/api/v1/internal/billing/webhook/` (AMD-014)
3. Включить события webhook: `payment.succeeded`, `payment.canceled`, `payment.waiting_for_capture`, `refund.succeeded`.
4. Тестовые карты (из доки ЮKassa): успешная, отказ, 3DS — записать в runbook §5 (smoke S3).
5. Split: регистрация платформы как агента (для transfers per-master) — проверить, что тест-магазин поддерживает split; `YOOKASSA_AGENT_ID` в env.

## 3. Чеклист E3 — MAX staging-бот

1. Создать staging-бота в MAX (или выделить существующего): получить `MAX_BOT_TOKEN`.
2. Webhook бота → `https://staging.gobeauty.site/api/v1/ingress/max/` (проверить путь по config/urls.py).
3. Mini App staging: зарегистрировать URL `https://staging.gobeauty.site/miniapp/` (или путь из `MAX_MINIAPP_URL`).
4. Проверка: echo-сообщение боту → ответ; ingress лог без 4xx.

## 4. Чеклист KYC — на мастера (E4, per-master)

Повторять для каждого из 15+ мастеров списка:

1. Данные: ФИО, телефон, специализация; статус (самозанятый/ИП) + ИНН + реквизиты для выплат.
2. ЮKassa суб-аккаунт (split per-master): онбординг-форма → `yookassa_account_id` → записать в `SpecialistProfile` (поле из W1, users/0014).
3. В системе: профиль мастера, услуги (из каталога), расписание (working hours), фото.
4. Онбординг-звонок (15 мин): приложение/кабинет, подписка 690₽, привязка карты (C7.2 мастера → W2-флоу), как работают записи/выплаты.
5. Тест-запись на каждого: создана → confirmed → завершена → 90₽ начислено/удержано верно.

## 5. Спека тестовых данных staging

Минимум для реального прогона:

- Tenant: салон «Формула тела» (Пенза) + 1 соло-мастер (для tenant-NULL проверок, AMD-015).
- Услуги: пилотный набор из canonical seed (1223) — отобрать 20–30 реальных услуг салона с `requires_health_check`/длительностями; каталог-синк YClients (если салон на YClients).
- Расписание: рабочие часы на 2 недели вперёд + 1 отпуск/выходной (краевой случай слотов).
- Пользователи: 2 тестовых клиента (с consent memory_green и без — для S2 и гейта памяти).
- Платёжное: 1 мастер с привязанной картой + активной подпиской; 1 — past_due (для C1-блокировки).

## 6. Что осталось открытым (за владельцем)

- E1: подтверждение варианта A или B (по факту `dev.gobeauty.site`).
- E2: доступ в кабинет ЮKassa (кто выполняет §2).
- E3: кто создаёт MAX staging-бота (§3, шаг 1).
- E4: именованный список мастеров для §4.
