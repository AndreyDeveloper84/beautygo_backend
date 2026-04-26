# Refactor Prioritization — Phase A/B/C Plan

> Status: APPROVED · Date: 2026-04-26 · Approver: Andrey
> Source: `docs/ARCHITECTURE_RECOMMENDATIONS.md` (19 findings, 2026-04-26)
> Pilot deadline: M5 Penza, 2026-07-15 (~11 weeks из 2026-04-26)

## Why this document exists

`ARCHITECTURE_RECOMMENDATIONS.md` предлагает 3-sprint refactor (~3 недели). С
учётом что до пилота 11 недель и нам ещё нужно:
- Food Scanner (3 недели, multi-vendor router)
- Mobile Phase 3 UI (3-4 недели, 3 экрана)
- AI Chat integration + tuning (1-2 недели)
- Buffer + bug fixes (1-2 недели)

— "3 недели рефакторинга всё подряд" не влезает. Этот документ режет 19 findings
на 3 фазы: pre-pilot critical / параллельно с pilot dev / post-pilot.

## Three-phase split

| Phase | When | Effort | Findings |
|---|---|---|---|
| **A — Pre-pilot critical** | Этой неделей | ~13ч (1.5 дня CC) | #2 #1 #3 #11 #12 #9 |
| **B — Parallel cleanup** | Размазано по pilot dev | ~12ч (1.5 дня CC) | #5 #6 #7 #19 |
| **C — Post-pilot** | Phase 6 (август-сентябрь) | ~8 дней CC | #4 #8 #13 #14 #10 #15 #16 #17 #18 |

---

## 🔴 Phase A — Pre-pilot critical

**Branch:** `refactor/phase-a-pilot-critical` (отдельная PR от feature work)
**Total effort:** ~13 часов CC
**Why now:** security risk + perf bombs + 5-минутная гигиена. Откладывать = подрывать пилот.

### A.1 — YooKassa webhook HMAC verification (`#2`)
**File:** `payments/views.py:216-256`
**Effort:** 4ч
**Why critical:** Сейчас webhook проверяет только IP allowlist. IP spoofing внутри
VPC возможен → фейковый `payment.succeeded` → бронирование подтверждается без
реальной оплаты → реальные финансовые потери.
**Action:** добавить HMAC-SHA256 verification из заголовка `Idempotence-Key` или через `yookassa.Webhook.verify()`. Add tests for valid + invalid signature paths.

### A.2 — Глобальная пагинация (`#1`)
**File:** `djangoProject/settings/base.py` + view-level checks
**Effort:** 2ч
**Why critical:** appointments list возвращает все записи без пагинации. С ростом
до 500 юзеров × 10 записей = 5000 объектов в одном response → OOM или медленный
mobile.
**Action:** добавить `DEFAULT_PAGINATION_CLASS` + `PAGE_SIZE: 20` в DRF settings.
Проверить что list-views переопределяющие пагинацию (reviews) не сломаются.

### A.3 — N+1 на specialist detail (`#3`)
**File:** `users/specialists_api.py:153-170`
**Effort:** 2ч
**Why critical:** Каталог мастеров — самый частый запрос. p95 latency растёт
линейно с количеством мастеров. На 50-100 мастерах в Пензе уже заметно.
**Action:** добавить `prefetch_related('working_hours', 'portfolio')` +
`Prefetch('services', queryset=Service.objects.select_related('category'))` в
queryset для detail action.

### A.4 — Составные индексы на Service (`#11`)
**File:** `services/models.py` (Service.Meta)
**Effort:** 2ч
**Why critical:** Каталог фильтруется по `services__category_id`,
`services__price__gte/lte`. Без индексов — sequential scan.
**Action:** добавить `Index(fields=['specialist', 'category', 'is_active'])` +
`Index(fields=['specialist', 'price'])`. Auto-migration.

### A.5 — Slot cache invalidation на reschedule (`#12`)
**File:** `appointments/infrastructure/outbox_worker.py` + reschedule flow
**Effort:** 2ч
**Why critical:** Bug. После переноса записи клиент видит стухшие слоты.
Воспроизводимый.
**Action:** проверить что `CancelRescheduleService` эмитит `CACHE_INVALIDATE_SLOTS`.
Если нет — добавить. Test для reschedule + cache HIT-MISS sequence.

### A.6 — Удалить `backend/` (`#9`)
**Effort:** 5 минут
**Why now:** мёртвый scaffold путает новых разработчиков и AI-агентов.
**Action:** `git rm -r backend/` + verify ничего не импортирует.

### Phase A delivery
- Single PR `refactor/phase-a-pilot-critical` → `dev`
- All 6 fixes + tests
- Один коммит на каждое исправление для clean review

---

## 🟡 Phase B — Parallel cleanup (during pilot dev)

**Branch:** `refactor/phase-b-cleanup-{n}` (мелкие PR-ы)
**Total effort:** ~12 часов, размазано по 4-6 неделям
**Why parallel:** не блокируют features, улучшают consistency, легко откатить.

### B.1 — Error codes в ErrorCode enum (`#5`)
**File:** `core/errors.py` + `users/response.py` + scattered call sites
**Effort:** 3ч
**Action:** найти все `error_response("STRING", ...)`, добавить в enum, заменить.
Add assert в `error_response()` против enum membership.

### B.2 — Унификация response envelope (`#6`)
**Files:** `users/response.py` → `core/response.py`, обновить 6 импортирующих apps
**Effort:** 4ч
**Action:** перенести хелперы, обновить импорты, deprecation alias на `users.response`.

### B.3 — Унификация exception иерархий (`#7`)
**Files:** `users/services.py`, `appointments/domain/exceptions.py`, `core/errors.py`
**Effort:** 4ч
**Action:** `BookingDomainError` → наследник `DomainException`, `AuthError` → то же.
Убрать lazy-import branches из exception_handler.

### B.4 — Reviews action permission bypass (`#19`)
**File:** `users/specialists_api.py:345-350`
**Effort:** 1ч
**Action:** вынести reviews listing в `reviews/views.py` с собственным URL
`/api/v1/specialists/{id}/reviews/`, удалить cross-app late-bound import.

### Phase B delivery
- Каждая B.x — отдельная PR (1-2 файла, легко ревьюить)
- Темп: 1 PR в 1-2 недели

---

## 🟢 Phase C — Post-pilot (Phase 6)

**Branch:** TBD по результатам pilot data
**Total effort:** ~8 дней CC
**Why defer:** high-risk migrations, требуют production data, или нужны при scale а не пилоте.

| # | Что | Effort | Defer reason |
|---|---|---|---|
| **#4 + #8** | Разделить `users/` на `auth/`+`specialists/`+`users/` + circular dep fix | 3-4 дня | High-risk migration перед пилотом = катастрофа. После пилота с прод-данными есть реальные usage patterns |
| **#13** | Profile creation: signal → explicit | 2ч | Касается auth flow, риск регрессии |
| **#14** | Rating recalc: signal → explicit + management command | 3ч | Implicit→explicit migration critical path |
| **#10** | Haversine в SQL (PostGIS / annotate) | 1д | Не нужно до scale: пилот = 1 город (Пенза), 50-100 мастеров. Python-сорт за <5мс достаточно |
| **#15** | Per-specialist throttle scope | 3ч | Пилотные пользователи не сделают 100 RPS. Защита от scale |
| **#16** | Soft delete на Appointment/Service/Review | 1д | Пилот ловит ~0 случайных удалений |
| **#17** | django-auditlog | 1д | Audit trail для disputes — premature на пилоте |
| **#18** | Feature flags (django-waffle) | 1д | Для пилота — env-based toggle (`FOOD_SCANNER_ENABLED=true`). Полноценный waffle когда понадобятся per-user flags |

### Phase C trigger conditions
- Запуск Phase C не раньше **2026-08-15** (1 месяц после M5 launch — для накопления usage data)
- Decision gate: re-prioritize по реальным метрикам (какие endpoints медленные, где disputes, какие фичи нужны per-segment)

---

## Roadmap пилота

```
Неделя 1 (28 апреля - 4 мая):
  Mon-Tue: Phase A pre-pilot critical (1.5 дня) — текущая ветка
  Wed-Fri: AI Chat finalization (PR review/merge) + Phase 3 mobile UI старт

Неделя 2-4 (5-25 мая):
  Food Scanner 5 slices (multi-vendor router) — 3 недели
  Phase B cleanup — 0.5 дня в неделю вкраплениями

Неделя 5-7 (26 мая - 15 июня):
  Mobile Phase 3 UI — 3 экрана
  Water tracker — 2 дня
  Phase B cleanup — 0.5 дня в неделю

Неделя 8-9 (16-29 июня):
  Integration testing
  Phase A bug fixes + Phase B завершение
  Pilot smoke tests

Неделя 10-11 (30 июня - 13 июля):
  Pre-launch hardening
  Beta testing с 10 user-ами в Пензе
  Bug fixes

15 июля — M5 pilot launch
```

---

## Open question — Feature flags

`#18 Feature flags` — единственный пункт где Phase A vs Phase C спорно.

**Compromise:** env-based toggle сейчас (`FOOD_SCANNER_ENABLED`, `AI_CHAT_ENABLED`),
полноценный django-waffle в Phase C если пилот покажет необходимость per-user flags.

Это **не отдельный finding в плане выше** — реализуется как часть Phase A.6 deliverable
"add critical-path env toggles" (5 минут работы).

## References

- `docs/ARCHITECTURE_RECOMMENDATIONS.md` — полный список 19 findings с file:line
- `docs/AI_CHAT_PLAN.md` — параллельный AI Chat поток
- `docs/FOOD_SCANNER_DECISION.md` — Plan Y+ multi-vendor router
- `docs/REFACTORING_PLAN.md` — предыдущий refactor (Phase 1-3.5, всё MERGED)

---

*Last updated: 2026-04-26*
