# ARCHITECTURE_RECOMMENDATIONS.md

**Дата:** 2026-04-26
**Ветка:** `dev` (коммит `8aac531`)
**Автор:** Architecture Review Session
**Статус:** Draft — требует обсуждения перед реализацией

---

## Сводка

19 конкретных находок по результатам аудита кодовой базы Ayla (ex-BeautyGO) backend.
Каждая привязана к файлу и строке, содержит описание проблемы, рекомендацию и альтернативу.

| Приоритет | Кол-во | Effort | Описание |
|-----------|--------|--------|----------|
| **P0 — Критичные** | 3 | ~8ч | Безопасность, стабильность, performance |
| **P1 — Архитектурные** | 6 | ~3-4 дня | Консистентность, maintainability |
| **P2 — Performance** | 5 | ~2-3 дня | Оптимизация под масштабирование |
| **P3 — Стратегические** | 5 | ~4-5 дней | До масштабирования, не до пилота |

---

## P0 — Критичные (исправить до пилота)

### 1. Нет пагинации на list-эндпоинтах

**Файл:** `appointments/views.py:78-84`
**Также:** specialists list, services list

**Проблема:**
Appointments list возвращает ВСЕ записи без пагинации. Клиент с 500 записями получит 500 объектов в одном ответе. Пагинация настроена только в `reviews/views.py:54` (`ReviewPagination`), остальные apps — нет.

**Риск:** OOM на проде при росте данных. Медленные ответы на мобильном.

**Рекомендация:**
Добавить `DEFAULT_PAGINATION_CLASS` в `djangoProject/settings/base.py`:
```python
REST_FRAMEWORK = {
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
}
```

**Effort:** 2 часа

---

### 2. YooKassa webhook — нет проверки подписи

**Файл:** `payments/views.py:216-256`

**Проблема:**
Webhook проверяет только IP allowlist (строка 251), но не HMAC-подпись тела запроса. IP spoofing внутри VPC возможен. Злоумышленник может отправить фейковый `payment.succeeded` → бронирование подтвердится без реальной оплаты.

**Риск:** Фейковые платежи, финансовые потери.

**Рекомендация:**
Добавить проверку подписи через `yookassa.Webhook.verify()` или ручную проверку HMAC-SHA256 из заголовка.

**Effort:** 4 часа

---

### 3. N+1 запросы на specialist detail

**Файл:** `users/specialists_api.py:153-170`

**Проблема:**
`get_working_hours()` (строка 156) делает свежий query в serializer method — не использует prefetch. При листинге 20 мастеров = 20 дополнительных запросов. `get_services()` (строка 122) аналогично для category.

Specialist detail endpoint = минимум 4 дополнительных запроса на каждого мастера:
- working_hours (строка 156)
- services без select_related category (строка 122)
- portfolio (строка 150 — prefetch есть, но не для detail)
- services_count

**Риск:** Каталог мастеров — самый частый запрос. p95 latency растёт линейно с количеством мастеров.

**Рекомендация:**
Добавить в `get_queryset()` для detail action:
```python
.prefetch_related(
    'working_hours',
    'portfolio',
    Prefetch('services', queryset=Service.objects.select_related('category')),
)
```

**Effort:** 2 часа

---

## P1 — Архитектурные (ближайшие 2 спринта)

### 4. Монолитный `users` app

**Файл:** `users/` — 16 Python-файлов + 13 тестовых

**Проблема:**
Один app отвечает одновременно за: auth, profiles, specialists catalog, schedule management, portfolio, device tokens, SMS, social auth, middleware, permissions. Это минимум 3 bounded context в одном app.

**Рекомендация:**
Разделить постепенно:
```
auth/          — User, OTPCode, SocialAccount, AnonymousSession, views.py, services.py
specialists/   — SpecialistProfile, Portfolio, specialists_api.py, schedule_api.py, portfolio_api.py
users/         — Profile, DeviceToken, middleware.py, permissions.py, response.py
```

Первый шаг (без миграций): вынести `specialists_api.py`, `schedule_api.py`, `portfolio_api.py` в отдельный app с `db_table` alias на существующие таблицы.

**Effort:** 2-3 дня

---

### 5. Строковые error codes вне ErrorCode enum

**Файл:** `reviews/views.py:98`

**Проблема:**
```python
return error_response("APPOINTMENT_NOT_COMPLETED", ...)
```
Код `"APPOINTMENT_NOT_COMPLETED"` не существует в `core/errors.py:ErrorCode`. Мобильный клиент не может сделать exhaustive switch — неизвестные коды появляются только в runtime.

Аналогично: `users/response.py` хелперы принимают любую строку, не валидируя против enum.

**Рекомендация:**
- Добавить недостающие коды в `core/errors.py:ErrorCode`
- Заменить `error_response(string_code, ...)` на `raise DomainException` с правильным `ErrorCode`
- В `error_response()` добавить assert: `assert code in ErrorCode.__members__`

**Effort:** 3 часа

---

### 6. Два места для response envelope

**Файлы:**
- `users/response.py` — `success_response()` / `error_response()`
- `djangoProject/exception_handler.py` — `_envelope()` с другой семантикой

**Проблема:**
Error codes из views идут через `error_response()` (не валидируются против enum), а из exceptions — через handler (валидируются). Мобильный клиент получает два разных формата ошибок в зависимости от endpoint.

Шесть apps импортируют хелперы из `users.response` — это ещё и cross-app coupling через users.

**Рекомендация:**
Перенести `success_response()` / `error_response()` в `core/response.py`. Все ошибки — через `raise DomainException`, не через `return error_response()`. Response envelope — через custom DRF renderer или middleware.

**Effort:** 4 часа

---

### 7. Три иерархии exceptions — не унифицированы

**Файлы:**
- `core/errors.py:77` — `DomainException` (целевая)
- `users/services.py:18-74` — `AuthError`, `PhoneAlreadyRegisteredError` (legacy)
- `appointments/domain/exceptions.py:7` — `BookingDomainError` (domain-level)

**Проблема:**
Центральный exception handler (`djangoProject/exception_handler.py:50-74`) вынужден lazy-import и проверять 3+ иерархии. Комментарий в коде прямо говорит: *"Legacy domain bases still in use..."*.

Reviews не используют ни одну из иерархий — бросают raw DRF exceptions или строки через `error_response()`.

**Рекомендация:**
- Сделать `BookingDomainError` наследником `DomainException`
- Сделать `AuthError` наследником `DomainException`
- Убрать lazy-import fallbacks из exception handler
- В reviews — перейти на `DomainException`

**Effort:** 4 часа

---

### 8. Circular dependency users ↔ appointments

**Файлы:**
- `users/specialists_api.py:155,287-288` импортирует из appointments:
  ```python
  from appointments.models import SpecialistWorkingHours
  from appointments.application.services.availability_query_service import AvailabilityQueryService
  ```
- `appointments/application/services/availability_query_service.py:61` импортирует обратно:
  ```python
  from users.models import SpecialistProfile
  ```

**Проблема:**
Mid-level circular dependency. Работает только потому что imports — late-bound (внутри функций). При рефакторинге на top-level imports — сломается.

**Рекомендация:**
Вынести `SpecialistWorkingHours` и `SpecialistTimeOff` из appointments в specialists app (при разделении users). Или: availability endpoint — в appointments app, не в users/specialists_api.

**Effort:** 1 день (вместе с п.4)

---

### 9. Удалить stale `backend/` directory

**Файл:** `backend/backend/` — мёртвый дубликат проекта

**Проблема:**
Старый scaffold с `ROOT_URLCONF='backend.urls'`, без filter backend, без urls для users app. Путает новых разработчиков и AI-ассистентов.

**Рекомендация:**
```bash
git rm -r backend/
```

**Effort:** 5 минут

---

## P2 — Performance (до масштабирования)

### 10. Поиск — haversine в Python, не в SQL

**Файл:** `search/views.py:157-164`

**Проблема:**
```python
specialists = list(qs[:limit * 3])  # overfetch в память
specialists.sort(key=lambda s: _haversine(...))  # сортировка в Python
```
Загружает 3x мастеров в RAM, сортирует Python-ом. При 1000 мастеров — ~3000 объектов на каждый поисковый запрос.

**Рекомендация:**
Перейти на PostGIS `ST_Distance` или SQL-level приближение через `.annotate()`:
```python
from django.db.models.functions import Sqrt, Power
.annotate(
    distance=Sqrt(Power(F('location_lat') - lat, 2) + Power(F('location_lng') - lon, 2))
)
.order_by('distance')[:limit]
```
Не учитывает кривизну Земли, но для одного города (Пенза) — достаточно и в 100x быстрее.

**Effort:** 1 день

---

### 11. Отсутствуют составные индексы на фильтруемых полях

**Файл:** `services/models.py` — `Service.Meta`

**Проблема:**
Specialist catalog фильтруется по `services__category_id`, `services__price__gte/lte`, `specialist__rating`. Составных индексов для этих паттернов нет.

**Рекомендация:**
```python
# services/models.py → Service.Meta.indexes
models.Index(fields=['specialist', 'category', 'is_active']),
models.Index(fields=['specialist', 'price']),
```

**Effort:** 2 часа

---

### 12. Slot cache — нет инвалидации при reschedule

**Файл:** `appointments/infrastructure/outbox_worker.py`

**Проблема:**
Cache invalidation привязана к `CACHE_INVALIDATE_SLOTS` outbox event. Необходимо убедиться что reschedule эмитит этот event. Если нет — клиент видит стухшие слоты после переноса записи.

**Рекомендация:**
Проверить `CancelRescheduleService` — если `CACHE_INVALIDATE_SLOTS` не эмитится при reschedule, добавить.

**Effort:** 2 часа

---

### 13. Signal для критической бизнес-логики (profile creation)

**Файл:** `users/signals.py:7-14`

**Проблема:**
```python
@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created and instance.role in ('client', 'specialist'):
        Profile.objects.create(user=instance)
```
Если signal не сработает (disconnect, exception) — User существует без Profile. Никакого fallback, никакого health check.

**Рекомендация:**
Перенести создание Profile в `AuthService.create_user()` — explicit, в той же транзакции. Signal оставить только как защитный fallback.

**Effort:** 2 часа

---

### 14. Signal для rating recalculation

**Файл:** `users/signals.py`

**Проблема:**
Рейтинг мастера (`SpecialistProfile.rating`, `reviews_count`) пересчитывается через `post_save` signal на Review. Implicit coupling: ничего в `reviews/views.py` не указывает что рейтинг обновится.

Если signal сломается — рейтинг стухнет тихо, без ошибки.

**Рекомендация:**
Перенести пересчёт в review create/update view (или ReviewService). Добавить management command `recalculate_ratings` для периодической сверки.

**Effort:** 3 часа

---

## P3 — Стратегические (до масштабирования, не до пилота)

### 15. Нет rate limiting по specialist_id

**Проблема:**
Throttle scopes — по пользователю (anon/user). Нет защиты от одного клиента, который шлёт 100 запросов на слоты одного мастера. Slot calculation — дорогая операция.

**Рекомендация:**
Добавить throttle scope `specialist_slots` с ключом `specialist_id + user_id`.

**Effort:** 3 часа

---

### 16. Нет soft delete

**Проблема:**
`User.deleted_at` есть, но `Appointment`, `Service`, `Review` — hard delete (`CASCADE` / `PROTECT`). При ошибочном удалении — потеря данных.

**Рекомендация:**
Добавить `deleted_at` + `SoftDeleteManager` на Appointment и Service. Или использовать `django-safedelete`.

**Effort:** 1 день

---

### 17. Нет audit log

**Проблема:**
Кто изменил статус бронирования? Кто отменил платёж? Сейчас — только `updated_at`. Для споров и поддержки нужен audit trail.

**Рекомендация:**
`django-auditlog` на критических моделях: Appointment, Payment, Review, SpecialistProfile.

**Effort:** 1 день

---

### 18. Нет feature flags

**Проблема:**
`core/env_strictness.py` — fail-fast для env vars, но нет feature flags для gradual rollout. AI chat, Food Scanner, Water Tracker — фичи, которые нужно включать по сегментам.

**Рекомендация:**
Минимальный вариант: `Feature` модель + middleware + `@feature_required('ai_chat')` decorator. Или: `django-waffle`.

**Effort:** 1 день

---

### 19. Reviews action как permission bypass

**Файл:** `users/specialists_api.py:345-350`

**Проблема:**
```python
@action(detail=True, methods=['get'], url_path='reviews',
        permission_classes=[permissions.AllowAny])
def reviews(self, request, pk=None):
    from reviews.views import SpecialistReviewsView
    return SpecialistReviewsView().get(request, specialist_id=pk)
```
ViewSet имеет `permission_classes = [IsAuthenticated]` (строка 247), но action переопределяет на `AllowAny`. Late-bound import из другого app. Скрытая точка отказа.

**Рекомендация:**
Вынести reviews listing в отдельный view в reviews app с собственным URL (`/api/v1/specialists/{id}/reviews/` → `reviews.urls`).

**Effort:** 1 час

---

## Дорожная карта реализации

### Sprint 1 (неделя 1) — P0 + Quick P1 wins
| День | Задача | # | Effort |
|------|--------|---|--------|
| Пн | Пагинация + удаление backend/ | 1, 9 | 2ч |
| Пн | N+1 fix specialist detail | 3 | 2ч |
| Вт | Webhook signature verification | 2 | 4ч |
| Ср | Error codes в enum + DomainException миграция | 5, 7 | 6ч |
| Чт | Единый response envelope в core/ | 6 | 4ч |
| Пт | Reviews permission fix + review action вынос | 19 | 2ч |

### Sprint 2 (неделя 2) — P1 + P2
| День | Задача | # | Effort |
|------|--------|---|--------|
| Пн-Ср | Разделение users app + circular dep fix | 4, 8 | 3д |
| Чт | Database indexes + slot cache fix | 11, 12 | 4ч |
| Пт | Signals → explicit (profile + rating) | 13, 14 | 4ч |

### Sprint 3 (неделя 3) — P2 + P3
| День | Задача | # | Effort |
|------|--------|---|--------|
| Пн | SQL haversine в поиске | 10 | 1д |
| Вт | Rate limiting per specialist | 15 | 3ч |
| Ср | Audit log (django-auditlog) | 17 | 1д |
| Чт | Feature flags | 18 | 1д |
| Пт | Soft delete | 16 | 1д |

---

## Связанные документы

- `CLAUDE.md` — Project Intelligence (source of truth для AI)
- `docs/ARCHITECTURE_REVIEW.md` — Current State Audit (Phase 1)
- `DESIGN.md` — Ayla Design System
- `docs/REFACTORING_PLAN.md` — Phase 1-3.5 refactoring history
