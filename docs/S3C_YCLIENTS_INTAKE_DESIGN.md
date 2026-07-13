# S3C — Pilot Salon Catalog Intake (YClients API-pull → Draft → confirm)

> **v1.0 AGREED · 2026-07-10 · оркестратор согласовал механизм + 3 развилки.**
> Решения (2026-07-10): (1) единый pipeline + 2 адаптера — ✅; (2) YClients креды
> есть/скоро → **PR1 = API-client first** (primary path), CSV fallback строим тем
> же пайплайном; (3) confirm пилота = **mgmt-команда** `intake_confirm` (admin-UI —
> follow-up).
> Ticket: `AndreyDeveloper84/ai-bot-platform#1109` · EPIC `#1044` (catalog rebuild).
> Repo: `beautygo_backend`, base `origin/dev`, branch `feat/1044-s3c-yclients-intake`.
> Зависит от **S3A** (agent-1): `DraftSalonService`, `ExternalSourceMapping`,
> `SalonService`, `SpecialistService` — на момент написания в `dev` ещё НЕТ.

## 1. Цель и границы

**Цель:** завести услуги пилотного салона (Пенза, 1 салон) в Ayla-каталог так,
чтобы они стали bookable, а повторный импорт был **идемпотентным**.

**Поток (целевой):**
```
YClients (source of record услуг)
   │  export: API-pull primary / CSV fallback
   ▼
IntakePipeline (S3C)  ──uses──►  ExternalSourceMapping (идемпотентность)
   │  normalize → upsert
   ▼
DraftSalonService (staging, не bookable)
   │  admin confirm
   ▼
SalonService + SpecialistService (bookable)
```

**В границах S3C (allowed):** `services/` (intake-пайплайн, YClients API-client,
CSV-loader, mgmt-команда, admin confirm action), новые тесты. **НЕ трогаю:**
`payments/`, `appointments/` (booking-движок), модели S3A (их владеет agent-1 —
я только читаю/пишу через них, координируя ребейз).

**Вне S3C:** сам booking через YClients (S3-CAL Variant B, занятость), consent,
мультисалонность. YClients здесь = **источник каталога услуг**, не движок броней.

## 2. Развилка механизма: API-pull vs CSV — предложение

Founder (2026-07-07): **API-pull primary**. Предлагаю так:

| | API-pull (primary) | CSV bootstrap (fallback) |
|---|---|---|
| Когда | Креды YClients готовы | Кредов нет / салон отдал выгрузку файлом |
| Источник | `GET /api/v1/company/{id}/services` (+ staff) | Ручной/экспортный CSV в `services/seeds/` |
| Свежесть | Повторяемый pull (крон/ручной) | Разовый bootstrap |
| Идемпотентность | `ExternalSourceMapping` по `service_id/staff_id` | Тот же mapping по `external_id` из CSV-колонки |
| Риск | Зависит от кредов/скоупов/rate-limit | Дрейф формата, ручная выгрузка |

**Рекомендация:** **единый `IntakePipeline`** с двумя **адаптерами-источниками**
(`YClientsApiSource`, `CsvSource`), которые оба отдают один нормализованный DTO
`RawServiceRecord`. Пайплайн (normalize → map → upsert draft) не знает, откуда
данные. Это даёт:
- API как primary, CSV как fallback **без ветвления бизнес-логики**;
- одинаковую идемпотентность (оба несут `external_id`);
- тестируемость пайплайна без сети (CSV-фикстура), API-client тестируется отдельно
  замоканным `requests` (как `outbox/publisher` tests).

```
RawServiceRecord(
  external_service_id: str,      # YClients service_id  (или CSV колонка)
  external_staff_ids: list[str], # YClients staff_id[]  (кто оказывает)
  title: str, duration_min: int|None, price: Decimal|None,
  category_hint: str, raw: dict,  # сырой payload для аудита
)
```

## 3. YClients API-client (независимый каркас — PR1)

Стиль — как `appointments/infrastructure/outbox/publisher.py`: `requests`,
креды/URL из `settings` c **fail-closed**, timeout, backoff-ретрай на 429/5xx,
структурированные DTO, чистая функция запроса отдельно от нормализации.

- Модуль: `services/integrations/yclients/client.py` (+ `dto.py`, `errors.py`).
- Auth (YClients API v1, dual-token): заголовки
  `Authorization: Bearer <YCLIENTS_PARTNER_TOKEN>, User <YCLIENTS_USER_TOKEN>`,
  `Accept: application/vnd.yclients.v2+json`. **[confirm на кредах]** — точные
  скоупы/эндпоинты сверить, когда придут креды.
- Settings (fail-closed, пустые в dev/CI → client no-op/raise at call):
  `YCLIENTS_PARTNER_TOKEN`, `YCLIENTS_USER_TOKEN`, `YCLIENTS_API_BASE_URL`
  (default `https://api.yclients.com/api/v1`), `YCLIENTS_HTTP_TIMEOUT` (10s).
- Методы: `list_services(company_id) -> list[RawServiceRecord]`,
  `list_staff(company_id)` (для `staff_id → SpecialistProfile`).
- Ретрай: 429/5xx → экспоненциальный backoff (кап), 4xx → fail сразу.
- **Никаких записей в YClients** в S3C (pull-only).

## 4. Идемпотентность — ExternalSourceMapping (S3A)

Ключ маппинга (ожидаемая форма модели S3A — **свериться с agent-1**):
```
ExternalSourceMapping(source='yclients', external_type, external_id) → local FK
  external_type='company' external_id=<company_id> → Tenant/Salon
  external_type='service' external_id=<service_id> → SalonService
  external_type='staff'   external_id=<staff_id>   → SpecialistProfile
```
Re-import = **upsert по (source, external_type, external_id)**: есть маппинг →
update целевой строки; нет → создать draft + маппинг. Гарантия: повторный pull не
плодит дубли и не перетирает admin-confirmed поля без явного правила.

`company_id → Tenant` и `staff_id → SpecialistProfile` уже частично разрешимы:
`SpecialistProfile.yclients_company_id` / `yclients_staff_id` (db_index) **уже на
модели** (#439/#0013). Резолв specialist по `yclients_staff_id` — без S3A.

## 5. Draft → confirm

- `DraftSalonService` (S3A) — staging-строка: нормализованные поля + `raw` +
  предложенный маппинг на канонический `ServiceTemplate`/категорию + статус
  `pending|confirmed|rejected`. **Не bookable.**
- Авто-предложение категории/шаблона: match по `title`/`category_hint` к
  `ServiceTemplate` (fuzzy, из уже засиженного canonical catalog `#201`);
  низкая уверенность → оставляем на admin.
- **Confirm** (для пилота — предлагаю **mgmt-команду** `intake_confirm`, а не UI;
  админ-мини-апп confirm-action — follow-up): при confirm создаём/обновляем
  `SalonService` (+ `SpecialistService` по `external_staff_ids`), проставляем
  bookable, фиксируем `ExternalSourceMapping`.
- Rejected drafts не мапятся; повторный импорт их не воскрешает (маппинг помнит).

## 6. Зависимость от S3A — как каркасить сейчас

S3A-моделей в `dev` нет. Чтобы не блокироваться:
1. **PR1 (сейчас, независимо):** YClients API-client + `RawServiceRecord` DTO +
   CSV-source + normalize + unit-тесты (мок `requests`, CSV-фикстура). **Без**
   импорта S3A-моделей — пайплайн заканчивается на «список нормализованных DTO».
2. **PR2 (после мержа S3A):** wire пайплайна к `DraftSalonService` +
   `ExternalSourceMapping` (upsert), ребейз на `dev`.
3. **PR3:** confirm-команда → `SalonService`/`SpecialistService` bookable.
4. **PR4 (по необходимости):** CSV-bootstrap mgmt-команда для салона без кредов.

Координация по `services/`: я и agent-1 в разных worktree. Мой PR1 не трогает
`services/models.py` (только новые файлы в `services/integrations/`), поэтому
конфликтов по моделям нет. Перед PR2 — `git fetch && rebase origin/dev` после
мержа S3A. **Согласовать с оркестратором точки мержа S3A.**

## 7. Декомпозиция (per-chunk PR, §H.3, PR → dev, Refs #1044)

| PR | Содержание | Зависит |
|----|-----------|---------|
| **S3C-PR1** | YClients API-client + DTO + CSV-source + normalize + тесты | — (независим) |
| **S3C-PR2** | Wire → DraftSalonService + ExternalSourceMapping upsert (идемпотентность) | S3A merged |
| **S3C-PR3** | `intake_confirm` mgmt-команда → SalonService/SpecialistService bookable | PR2 |
| **S3C-PR4** | CSV-bootstrap команда (fallback без кредов) | PR1 |

## 8. Открытые вопросы (нужно решение до/по ходу)

1. **YClients креды и тайминг** — есть partner/user token для пилотного салона?
   Если нет к PR1 — CSV-bootstrap становится критическим путём пилота (меняет
   приоритет PR4 ↔ PR2).
2. **Точные эндпоинты/скоупы YClients** (services/staff, версия API) — сверить на
   кредах. Сейчас в дизайне — v1 dual-token как предположение.
3. **Confirm-поверхность пилота** — mgmt-команда (предлагаю) vs admin-мини-апп
   action. UI — follow-up?
4. **Auto-match YClients-услуги → canonical `ServiceTemplate`** — порог
   уверенности; что делать с no-match (draft без шаблона, admin вручную)?
5. **Форма `ExternalSourceMapping`/`DraftSalonService` от S3A** — свериться с
   agent-1, чтобы PR2 писал в реальную схему.
6. **CSV-контракт** (колонки) — если fallback нужен, зафиксировать формат
   (предложу в PR4: `external_service_id,title,duration_min,price,category,staff_ids`).

## 9. DoD (из #1109)
- [ ] YClients API-client (pull services/staff), fail-closed на кредах.
- [ ] intake draft → confirm поток.
- [ ] `ExternalSourceMapping` идемпотентность (повторный import без дублей).
- [ ] §H.3 double-pass; PR → dev; Refs #1044.

## 10. Проверенный контракт YClients (live 2026-07-10, company 884045)

Read-only probe, креды из env (разовая верификация; прод-адаптер читает **свои**
`YCLIENTS_*` env). Значения токенов/данные салона не сохранялись.

**Base:** `https://api.yclients.com/api/v1`
**Auth:** `Authorization: Bearer <partner>, User <user>` + `Accept: application/vnd.yclients.v2+json`.
Формат заголовка **подтверждён** (management-вызов дошёл до бизнес-слоя, не 401).
**Envelope:** `{ "success": bool, "data": <list|dict>, "meta": <obj|[]> }`.
Ошибка → `success:false` + `meta.message` (RU-текст). ✅ live.
**Rate-limit (✅ live, заголовки ответа):** `X-RateLimit-Limit: 200`,
`X-RateLimit-Remaining`, `X-RateLimit-Reset: <сек до сброса окна>`. Адаптер:
следить за `Remaining`, backoff на `429`.

### Эндпоинты (проверено live)

| Endpoint | Токен | `data` | Статус probe |
|---|---|---|---|
| `GET /company/{id}/services[?staff_id=]` | partner+**user** + **активная лицензия** | **плоский список** service-объектов | ⛔ `403` — лицензия истекла (см. блокер) |
| `GET /book_services/{id}` | partner | **dict** `{services[], category[], category_groups[], events[]}` | ✅ `200` (но `services`=пусто из-за лицензии) |
| `GET /book_staff/{id}[?service_ids[]=X]` | partner | **список** staff-объектов | ✅ `200`, 6 мастеров |

`book_services.data` — **сгруппированный dict**, НЕ плоский список (в отличие от
management `/services`). Услуги в `data.services`, категории в `data.category`.

### Формы объектов

**Staff (✅ live):** `id`(int), `name`, `specialization`, `avatar`, `avatar_big`,
`bookable`(bool), `weight`(int), `rating`, `prepaid`(str, напр. `forbidden`),
`position_id`, `schedule_till`(`YYYY-MM-DD`), `fired`, `hidden`, `status`,
`information`, `image_group`(dict), `votes_count`, `comments_count`, `api_id`, `user_id`.
→ для intake нужны: `id`(staff_id), `name`, `specialization`, `bookable`, `weight`.

**Category (✅ live, `book_services.data.category`):** `id`(int), `parent_id`(int),
`title`, `sex`(int), `weight`(int), `api_id`. Иерархия через `parent_id`.

**Service (из прод-кода mysite; live не переподтверждён — лицензия):** `id`(int,
service_id), `title`, `price_min`(number), `price_max`(number), `price_from`,
`seance_length`(int, **СЕКУНДЫ**), `category_id`, `is_folder`(bool), `active`,
`comment`, `weight`, `prepaid`, `image_group`. Прод-sync `mysite` парсит именно
эти поля → контракт высокой точности.

### Конверсии/правила адаптера (подтверждены)
- `seance_length` секунды → **минуты** (`round(sec/60)`) для `duration_min`.
- `is_folder == true` → **папка-группировка, НЕ bookable-услуга → фильтровать**.
- `service_ids[]` — **bracket-notation** (без скобок → 404).
- Даты — `YYYY-MM-DD`.
- `429` → exponential backoff (лимит 200/окно).

### Пагинация
Management `/services` — не проверено live (лицензия). `book_services` отдаёт
всё в одном `data` без страничных полей. **[уточнить на активной лицензии]** —
адаптер закладывает возможную пагинацию через `meta`/`count`, но по умолчанию
читает весь `data`.

### 🚨 БЛОКЕР ПИЛОТА: лицензия YClients истекла (884045)
Management `/company/{id}/services` → `403` + `meta.message`
«Необходимо продлить лицензию в филиале с id: 884045». `book_services.data.services`
= пусто. **Каталог услуг с ценами/длительностями сейчас из YClients НЕ вытянуть**
(ни management, ни booking). Следствия:
1. **API-pull даёт пустой каталог пилота, пока лицензия не продлена.**
2. Критпуть пилота → **CSV-fallback**, либо продлить лицензию (тогда API-pull primary).
3. **PR1 (адаптер+pipeline+DTO+замоканные тесты) НЕ заблокирован** — строится
   против этого проверенного контракта; live-данные для unit-тестов не нужны
   (санитизированные/синтетические фикстуры).
