# PILOT PROGRESS — готовность к пилоту 2026-08-15

Обновлено: 2026-07-19 (день 2 из 28). Владелец: оркестратор. Обновляется после каждого отчёта потоков.

**Правила подсчёта:**
- Оценки в Story Points (1/2/3/5/8) — сложность, не часы.
- ✅ done = код в dev (смержено и запушено). 🔄 in-progress = в работе. ⏳ pending = не начато/заблокировано.
- Готовность = done SP / total SP. In-progress показан отдельно — в проценты НЕ включается (честно).
- Внешние задачи (юрист, KYC, набор мастеров) считаются отдельно от кода.

## Общий прогресс

```
Код:     ████████████░░░░░░░░░░░░░░  88% done (191/217 SP)
         ░░░░████░░░░░░░░░░░░░░░░░░  16% in-progress (29 SP)
         ░░░░░░░░░░████████████░░░░  40% pending (73 SP)
```

- **Done:** 191 SP · **In-progress:** 29 SP · **Pending (код):** 57 SP · **Pending (внешнее):** 16 SP
- Осталось дней: **26** (пилот 15.08). Сделано за дни 1–2: 78 SP.
- Требуемый темп: ~4 SP/день суммарно по окнам — достижимо при текущей скорости.

## По потокам

| Поток | Готовность | Done SP | В работе | Осталось | Комментарий |
|---|---|---|---|---|---|
| W1 Booking Core | **100%** | 32/32 | — | 0 | ✅ очередь P1–P8 в dev (2ebde419); 2240 тестов зелёных |
| W2 Billing | **100%** | 30/30 | — | 0 | ✅ в dev (26835bee); shim удалён, канон 112/112 |
| W3 Bot Backend | **97%** | 32/33 | — | 1 SP (прогон staging) | ✅ follow-up в dev (8817190) |
| W4 Mini App | **88%** | 38/43 | — | 5 SP | ✅ привязка мастера в dev (121c832); pay-debt CTA + booking seam |
| W5 Concierge | **100%** | 20/20 | — | 0 | ✅ фазы 1+2 в dev (a5e215d): память в диалоге |
| W6 QA/Docs | **95%** | 19/20 | 1 SP/нед | 1 SP | ✅ smoke-runner + runbook в dev (105ffd1b) |
| **ИТОГО код** | **88%** | **191/217** | 1 SP | 42 SP | |
| Внешнее | **0%** | 0/16 | — | 16 SP | юрист/KYC/мастера |

## W1 — Ayla Booking Core (100%)

| Задача | SP | Статус |
|---|---|---|
| Sync + merge memory-ветки | 2 | ✅ |
| Запись без предоплаты (D6) | 3 | ✅ |
| Каталог: поля template_id/name/category_slug (C6) | 3 | ✅ |
| Capture pipeline (D9) | 5 | ✅ |
| Авто-отмена холда | 2 | ✅ |
| Flat 90₽ | 1 | ✅ |
| Split per-master | 3 | ✅ |
| Reconciliation + алерты | 3 | ✅ |
| Payout preview (C3) | 2 | ✅ |
| Eligibility adapter (C1) | 2 | ✅ |
| Бамп ai-core v0.9.0 | 1 | ✅ |
| Follow-up патчи P1–P8 (INSTALLED_APPS, beat, urls, топики, handler-chain, совместный тест, слоты-tz, ⚠️webhook 403 fix) | 5 | ✅ |

## W2 — Billing & Legal (100%)

| Задача | SP | Статус |
|---|---|---|
| Модели (TariffPlan, Subscription, BookingFee, Invoice, Payment, Consent) | 5 |✅ |
| C5 export/delete endpoints | 3 |✅ |
| C1 can_accept_booking | 3 |✅ |
| Первый платёж + save_payment_method | 3 |✅ |
| Рекуррент monthly charge | 5 |✅ |
| Dunning → past_due | 3 |✅ |
| Чеки 54-ФЗ платформа→мастер | 2 |✅ |
| C2 status endpoint | 2 |✅ |
| C4 события | 2 |✅ |
| Совместный инвариант-тест W1×W2 | 2 | ✅ |
| pay-debt endpoint (W4 CTA) | 1 | ✅ |
| card last4/brand read-model | 1 | ✅ |

## W3 — Bot Backend (97%)

| Задача | SP | Статус |
|---|---|---|
| Import cycle fix + baseline | 1 | ✅ |
| link_ayla_service_ids + тесты | 5 | ✅ (прогон — staging) |
| Route-table idempotency pins | 1 | ✅ |
| C1 нейтральный surface | 1 | ✅ |
| Merge memory-consent-global | 2 | ✅ |
| Inferred memory persistence | 3 | ✅ |
| Personal-context client | 3 | ✅ |
| C4 топики + consumers | 2 | ✅ |
| R1 напоминания (верификация) | 1 | ✅ |
| C5 privacy endpoints | 3 | ✅ |
| C2/C3 прокси master_api | 2 | ✅ |
| Baseline-rot fixes | 2 | ✅ |
| #1045 разрешение конфликта | 2 | ✅ |
| Бамп ai-core v0.9.0 | 1 | ✅ |
| payment_required на create (G-1) | 1 | ✅ |
| link до AMD-001 (tiebreaker, stripping, mapping file) | 2 | ✅ |
| Прогон покрытия на staging | 1 | ⏳ staging |

## W4 — Mini App (26%)

| Задача | SP | Статус |
|---|---|---|
| Vitest + первые тесты | 3 | ✅ |
| C5 шторки export/delete | 3 | ✅ |
| Аудит 48 экранов | 2 | ✅ |
| Коммит 3: скрыть stub-секции | 2 | ✅ |
| Экран биллинга мастера (2б) | 5 | ✅ |
| Карточка «К выплате» (2б) | 2 | ✅ |
| Привязка карты мастера (D7 UI) | 2 | W4 | ✅ |
| Booking flow на реальном API (3) | 5 | ⏳ |
| UX-статусы оплаты (3) | 2 | ✅ |
| C1 нейтральное сообщение (3) | 1 | ✅ |
| Profile polish + notification prefs | 3 | ✅ |
| Гейт stub-экранов + ComingSoonScreen | 2 | ✅ |
| Stub-экраны → реальные данные (фаза 3) | 3 | ✅ |

## W5 — Concierge (100%)

| Задача | SP | Статус |
|---|---|---|
| Фаза 1: релиз ai-core v0.9.0 | 3 | ✅ |
| Concierge wiring (DRF-241) | 5 |✅ |
| Memory block injection + consent-гейт | 3 |✅ |
| Memory-ask (S3.5) | 5 |✅ |
| Голос/границы (Конституция) | 2 |✅ |
| Orchestrator baseline-rot (9 тестов) | 2 |✅ |

## W6 — QA/Docs (95%)

| Задача | SP | Статус |
|---|---|---|
| Документы в Git | 1 | ✅ |
| Baseline + контрактная матрица | 2 | ✅ |
| Тест-спецификации W1–W5 | 3 | ✅ |
| Черновики (Killer PRD, Memory Lifecycle) | 3 | ✅ |
| Roadmap rebase | 2 | ✅ |
| Smoke-runner | 5 | ✅ (боевой прогон — staging) |
| Runbook | 3 | ✅ |
| Drift-контроль (еженедельно) | 1/нед | 🔄 |

## Эпик C7 — Client Payments (новый, решение владельца 19.07)

| Задача | SP | Поток | Статус |
|---|---|---|---|
| C7.1 internal payment create (hold, idempotent) | 3 | W1 | ✅ |
| C7.2 card binding клиента (setup/list/delete) | 5 | W1 | ✅ |
| payment.authorized event (проверить/эмитить) | 1 | W1 | ✅ n/a — решением не вводится (сигнал = booking.confirmed) |
| C7.3 read endpoint (on-demand status) | 1 | W1 | ✅ |
| C7 passthrough в miniapp_api (payment + cards) | 3 | W3 | ✅ |
| Платёжные поля в BookingItem + статусы | 2 | W3 | ✅ |
| Маппинг C1 → клиентский slug UNAVAILABLE | 1 | W3 | ✅ |
| card-setup proxy (денежный путь мастера) | 1 | W3 | ✅ |
| pay-debt proxy (dunning escape) | 1 | W3 | ✅ |
| Выбор оплаты на summary + webview | 3 | W4 ✅ |
| Статусы оплаты в records/detail | 2 | W4 ✅ |
| Экран карт в профиле | 2 | W4 ✅ |
| C1 нейтральное сообщение + альтернативы | 1 | W4 ✅ |

Зависимость C7.4: флип #1041 ← отчёт покрытия каталога на staging.

## Внешние задачи (0%)

| Задача | SP-экв | Владелец | Дедлайн |
|---|---|---|---|
| Оферта автоплатежа | 2 | юрист | 01.08 |
| Агентская формулировка чеков | 1 | юрист | 01.08 |
| Правки 3/5 cross-domain правил | 2 | юрист | 01.08 |
| KYC-онбординг мастеров в ЮKassa | 3 | ops+юрист | 08.08 |
| Набор 15+ мастеров (supply) | 5 | основатель | 08.08 |
| Staging: прогон link + флип гейта | 3 | оркестратор | нед. 3 |

## Журнал обновлений

- **2026-07-20 (день 3):** +1 SP — W3 pay-debt proxy в dev `30bb104` (cherry-pick: его miniapp-коммит 4fadc2a — дубль зоны W4 — в dev НЕ пошёл). Денежный контур мастера замкнут полностью. Done: 191/217 (88%).

- **2026-07-20 (день 3):** +1 SP — W2 card last4/brand в dev `7afc8b4b` (миграция 0003, webhook-заполнение с guard saved==true, C2 card | null). Контракты v1.10.0 (AMD-017). Done: 190/216 (88%).

- **2026-07-20 (день 3):** +2 SP — W4 привязка карты мастера в dev `121c832` (consent-гейт, webview, тариф из C2). Тесты FE: 157/157. Назначено: pay-debt proxy → W3, last4/brand read-model → W2. Done: 189/215 (88%).

- **2026-07-20 (день 3):** +3 SP — W4 profile polish в dev `e7e7efb`: все 5 issue (#948/#950/#951/#953/#949), notification-prefs на реальных тоглах, убрана необоснованная строка retention 180 дней. Тесты FE: 152/152. Done: 187/213 (88%).

- **2026-07-20 (день 3):** +1 SP — W3 card-setup proxy в dev `69be5a3` (денежный путь: привязка карты мастера → подписка → без dunning → без past_due). Bot suite: 5555/0. Done: 184/213 (86%).

- **2026-07-20 (день 3):** +1 SP — W2 pay-debt endpoint в dev `bd9c6802` (долг = неоплаченный инвойс + последующие fee; settle → past_due → active, C1 разблокирует). W4 может подключать CTA. Done: 183/212 (86%).

- **2026-07-20 (день 3):** +7 SP — W4 фаза 2б в dev `a93aa49`: экран биллинга мастера (все статусы C2, next_charge AMD-013), карточка «К выплате» (два состояния). Тесты FE: 145/145. Блокер: card-setup passthrough → назначен W3; pay-debt endpoint → назначен W2. Done: 182/211 (86%).

- **2026-07-20 (день 3):** +1 SP — W1 C7.3 read endpoint в dev `3123a2e1` (+ ретро-фикс C7.1 на фактический pending по AMD-016). **C7 backend закрыт целиком (C7.1/2/3/6).** Тесты backend: 2264 → 2275. Done: 175/211 (83%).

- **2026-07-20 (день 3):** +3 SP — W4 C7 live-интеграция в dev `7352d23`: payment create с отказоустойчивостью (запись не теряется при падении payment create), cards live с consent-гейтом, C1 против реального slug. Тесты FE: 127/127. Done: 174/210 (83%).

- **2026-07-20 (день 3):** +6 SP — W3 followup2 в dev `398435d`: C7 passthrough целиком (verified binding, payments, cards, PaymentMirror, BookingItem.payment), C1 slug, AMD-015, O1 (HMAC secret из env), 5 падений W5 закрыты. **Bot suite впервые 5540/0 — абсолютный ноль падений.** Done: 171/207 (83%).

- **2026-07-20 (день 3):** +9 SP — W1 C7 backend в dev `ade20092`: C7.1 payment create (snapshot-amount, идемпотентность), C7.2 card binding (consent-поля, saved-флаг, revoke), C7.6 ownership. Тесты backend: 2240 → 2264. payment.authorized — решением не вводится (AMD-016: сигнал = booking.confirmed). Контракты v1.9.0. Done: 165/207 (80%).

- **2026-07-19 (день 2, вечер):** +8 SP — W6 волна 3 в dev `105ffd1b`: smoke-runner S1–S7 (black-box, env-driven) + runbook (deploy/rollback, канарейка, Concierge Mode, инциденты). W6 → 95%. Боевой прогон — после E1 (staging) и O1 (HMAC secret в bot settings). Done: 156/207 (75%).

- **2026-07-19 (день 2, вечер):** +2 SP — W2 shim-cleanup в dev `26835bee`; совместный тест закрыт W1 (P6). **W2 → 100%.** Done: 148/207 (71%). Три потока на 100% (W1/W2/W5).

- **2026-07-19 (день 2, вечер):** +8 SP — W4 фаза 3.3 в dev `97bd98c`: выбор оплаты UI + webview-seam, C1 нейтральное сообщение, C7.3 каркас статусов, экран карт (каркас). Живые платежи — после passthrough W3 + флип #1041. W4 → 61%. Done: 146/207 (71%).

- **2026-07-19 (день 2, вечер):** +5 SP — W1 очередь P1–P8 в dev `2ebde419`: billing подключён (apps/beat/urls), топики C4 зарегистрированы, handler-chain, ⚠️P8 — подтверждённый прод-баг payments webhook исправлен, совместный тест W1×W2 зелёный, AMD-005 стык выправлен с обеих сторон. W1 → 100%. Тесты backend: 2110 → 2240. Done: 138/207 (67%).

- **2026-07-19 (день 2, вечер):** +3 SP — W3 follow-up в dev `8817190` (payment_required AMD-002, link до AMD-001 с mapping-file, AMD-005 specialist_id_for_master). W3 → 97%. Решение D-1 → AMD-015 (tenant NULL для billing-событий соло). D-2 (топики не зарегистрированы) — это W1 P4, напоминание. Done: 133/207 (64%).

- **2026-07-19 (день 2, вечер):** +17 SP — W5 фаза 2 полностью в dev `a5e215d`: concierge wiring (DRF-241), memory block с consent-гейтом, memory-ask (S3.5), голос/границы, orchestrator-пакет зелёный. W5 → 100%. Тесты бота: 20 failed → 5 failed (все вне его зон, root cause передан W3). Done: 130/207 (63%).

- **2026-07-19 (день 2, вечер):** решение владельца — штатный контур оплаты клиента в miniapp (привязка карты + нормальная оплата, без DM-only). Новый контракт C7 (v1.6.0), эпик +23 SP (не 21 — поправка review): W1 9, W3 6, W4 8. Масштаб вырос до 207 SP → done 113/207 (55%). C7 переведён в status review до закрытия consent/authorization границ (v1.7.0). Критическая цепь: staging coverage → флип #1041 → C7 в miniapp.

- **2026-07-19 (день 2, вечер):** +3 SP — W4 фаза 3.1+3.2 (каталог и записи/деталь на реальных данных, home=записи) в dev `eb2c9f8`; 94/94 FE-тестов. W4 → 45%. Done: 113/184 (61%).

- **2026-07-19 (день 2, вечер):** +28 SP — W2 полный объём в dev `41a5133c` (модели, C1/C2/C4/C5, рекуррент, dunning, чеки; конфликт internal_users_urls разрешён оркестратором). W2 → 93%. Контракты v1.5.0 (AMD-013/014). Очередь W1 (P1–P8) разблокирована, ⚠️ P8 — проверка 403 payments webhook. Done: 110/184 (60%).

- **2026-07-19 (день 2, вечер):** +4 SP — W4 коммиты 3+4 (stub-gate prod + ComingSoonScreen + гейт каталога) в dev `7b56816`/`8853fed`. W4 → 36% (12/33). Фазе 3 (каталог real) дан зелёный свет. Done: 82/184 (45%).
- **2026-07-19 (день 2, вечер):** +2 SP — W4 коммит 3 (stub-gate prod) в dev `7b56816`. W4 → 32% (10/31). Решение по home-маршруту: interim ComingSoonCard → цель «Мои записи»; catalog fake-data → gate до фазы 3. Done: 80/182 (44%).
- **2026-07-19 (день 2):** стартовый снимок. Done 78 SP (43%): W1 ✅ (27), W3 ✅ (29), W4 частично (8), W5 фаза 1 (3), W6 (11). Парный бамп ai-core v0.9.0 замкнут. Контракты v1.4.0.
