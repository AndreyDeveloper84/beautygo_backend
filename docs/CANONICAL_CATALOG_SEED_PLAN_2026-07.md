# План: залив канонического списка услуг в Ayla → мост в бот

> Статус: DRAFT-план (план-first). Дата: 2026-07-01.
> Связано: Ayla #200 (S2), ai-bot-platform #1044 (эпик), #1034 (гейт флипа), #1041/#1045 (флип/hardening).
> Цель этого документа — согласовать шаги ДО кода.

## 1. Зачем

Разблокировать флип `BOOKING_VIA_AYLA_REST` в боте (см. #1034), а именно —
поднять **покрытие `ayla_service_id`** у услуг бота. Для этого нужен
канонический список услуг в Ayla со **стабильными UUID**, и эти UUID надо
прописать в зеркало бота `CatalogService.ayla_service_id`.

**Границы (важно):** это НЕ полный 4-слойный ребилд #200. Делаем
**прагматичный форвард-совместимый сид** — строгий «первый взнос» в #200,
не выброс. Полные `SalonService` / `YClientsMapping` / select-only — отдельно, позже.

## 2. Текущее состояние (по коду, а не по эпику)

**Ayla (`services/models.py`):**
- `ServiceTemplate` — есть, **UUID PK = уже стабильный ID** ✓. Но бедный:
  `category, name, name_short, duration_*, is_popular, sort_order`.
  **Нет** `requires_health_check`, `contraindications`, `slug`, синонимов, goals.
- `Service` (услуга мастера) — **не связан** с `ServiceTemplate` (FK только у `RegionalPricing`).
- Internal API боту (`services/internal_api.py`) — `template_id`/`slug` пока не отдаёт (#200 task 5).

**Бот (`apps/catalog/models.py`):**
- `CatalogService` — **богаче** шаблона Ayla: уже несёт `slug, name, goals,
  requires_health_check, contraindications, price_from, duration_min, ...`.
- Питается из легаси **`mysite`** синк-апсертером (C3/DRF-574), ключ `(tenant, external_id)`.
- `ayla_service_id` (UUID, nullable, миграция `0006`) — **новый мост в Ayla, сейчас пустой**.
- Бот при флаге-ON грайндит health-check по `ayla_service_id` → читает `requires_health_check`
  найденной строки (PR-B #1036). Промах → fail-closed handoff.

**Следствие:** источник истины по услугам сегодня — `mysite` → зеркалится в бот.
Ayla-шаблон надо наполнить и связать мостом. Совпадение строк бот↔Ayla делаем по `name`/`slug`.

## 3. Источник данных (ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ)

«Мой список услуг» = ? Варианты:
- **(a)** легаси `mysite` (пилотный салон Пензы) — эпик прямо пишет «из mysite»;
- **(b)** отдельный файл (Excel/CSV/Google-таблица) с курированным списком;
- **(c)** прислан текстом.

Нужные поля на услугу: `name`, `category`, `duration` (default/min/max),
**`requires_health_check`**, `contraindications` (если health-check=true),
опц. `slug`, синонимы, goals. Цена — позже (`RegionalPricing` / услуга мастера).

## 4. Пайплайн

### 4.1 Ayla-сторона
1. **Аддитивная миграция `ServiceTemplate`** (= подмножество #200 task 1, не выброс):
   `+ requires_health_check (bool, default False)`, `+ contraindications (text, blank)`,
   опц. `+ slug (stable)`, `+ synonyms`, `+ goals`. Nullable/дефолты → безопасно.
2. **Сид** канонических шаблонов из источника (§3): idempotent management-команда
   `seed_service_templates` (upsert по `(category, name)` — уже `unique_together`).
   На выходе — стабильный UUID у каждой услуги.
3. **(опц., #200 task 5)** отдать `template_id`/`slug` боту в internal API — если решим
   матчить по ID, а не по name/slug.

### 4.2 Мост в бот
4. **Management-команда в боте** `link_ayla_service_ids` (tenant-scoped):
   матчит `CatalogService` ↔ Ayla-шаблон (по `name`/`slug`) и проставляет
   `ayla_service_id`. Легаси `external_id`/mysite-синк **не трогаем** (параллельны).
5. **Отчёт покрытия:** `python manage.py ayla_service_id_coverage` → цифра % + список
   непокрытых. Это метрика снятия гейта из #1034.

## 5. Маппинг-ключ и риски
- Матчим по нормализованному `name` (или `slug`). Риск: расхождения написания
  (регистр, ё/е, пробелы). Для пилота (≈15–20 мастеров, услуг немного) —
  допустимо ручное ревью таблицы соответствий перед проставлением.
- Дубли/синонимы услуг → свести к одному шаблону.

## 6. Проверка (acceptance)
- [ ] `ServiceTemplate` несёт `requires_health_check` (+ contraindications), сид применён.
- [ ] У всех/≥порога живых `CatalogService` в боте проставлен `ayla_service_id`.
- [ ] `ayla_service_id_coverage` ≥ **порог** (число согласовать; для пилота реально ~100%).
- [ ] Booking-тесты Ayla зелёные (снапшот `Appointment` не сломан — FK на шаблон не влияет).
- [ ] Дым health-check: услуга с `requires_health_check=true` под флагом-ON → handoff, не авто-бронь.

## 7. Декомпозиция на PR (мелкими шагами)
1. **Ayla PR-1:** миграция `ServiceTemplate += requires_health_check/contraindications` (+тесты).
2. **Ayla PR-2:** `seed_service_templates` команда + данные пилота (idempotent).
3. **(опц.) Ayla PR-3:** `template_id`/`slug` в internal API (#200 task 5).
4. **Bot PR-1:** `link_ayla_service_ids` команда + прогон + отчёт покрытия.

## 8. Что НЕ делаем сейчас (осознанные границы)
- `SalonService`, `YClientsMapping` как отдельная сущность, enforce select-only —
  это полный #200, отдельный трек. Здесь только шаблон + мост + покрытие.
- Не трогаем легаси mysite-синк и YClients-путь бота (флаг остаётся OFF).

## 9. Открытые вопросы владельцу
1. **Источник списка** (§3): mysite / файл / текст?
2. **Порог покрытия** для снятия гейта: ориентир ≥90%, для пилота можно 100%.
3. **Ключ матчинга** бот↔Ayla: по `name`/`slug` (быстро) или ввести `template_id` в
   internal API и матчить по ID (чище, +1 PR)?
