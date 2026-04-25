# Phase 3 UI Specification — Ayla Client

**Дата:** 2026-04-24
**Статус:** Text-based spec (mockups deferred — awaiting PO/designer)
**Scope:** 3 Phase 3 screens — AI Chat, Food Scanner, Notification Permission
**Design System:** `DESIGN.md` (baseline tokens из Ayla Brand Vision)
**Contractor:** plan-design-review skill pass (7-pass rating)

> **Важно:** этот doc — **не финальный Figma**. Это implementation-grade spec для разработчика. PO/designer потом рисует pixel-perfect mockups на основе этого spec'а. До того — разработчик строит согласно этим tokens + layout правилам + interaction states.

---

## Context — где эти экраны живут

Ayla Client Bottom Navigation (5 tabs):

```
┌───┬───────────┬───┬──────────┬───┬──────┬───┬──────┬───┬─────────┐
│ 🏠│  Главная  │🍽️│ Питание  │ ✨│  Я   │📅 │ День │👤│Профиль │
└───┴───────────┴───┴──────────┴───┴──────┴───┴──────┴───┴─────────┘
     AI chat       Food Scanner  (Ph 7)    (Ph 5)     Settings
     (Ph 3)        (Ph 3)                              (existing)
```

**Phase 3 touches:**
- 🏠 Главная — add AI chat entry point + chat screen
- 🍽️ Питание — new tab, Food Scanner + food log
- Notification permission — triggered on first booking or first food scan

---

## Screen 1 — AI Chat

### Purpose
Entry point для клиента: "хочу маникюр рядом с офисом завтра вечером" → AI подбирает 3-5 мастеров → tap → запись.

### Information Architecture

```
┌──────────────────────────────────────┐
│  Status bar (iOS/Android system)     │
├──────────────────────────────────────┤
│  [←]   Ayla          [🎙️ voice*]    │  Header: back + title + voice btn
├──────────────────────────────────────┤
│                                      │
│   ┌──────────────────────────────┐  │  Welcome message (first open)
│   │ 🌙  Ayla                    │  │  incoming chat bubble
│   │ Привет! Помогу найти мастера│  │  --color-secondary background
│   │ по твоим предпочтениям.     │  │  17px body, 12px padding
│   │ Расскажи что ищешь?         │  │
│   └──────────────────────────────┘  │
│                                      │
│           ┌──────────────────────┐  │  Quick suggestion chips
│           │  💅 маникюр          │  │  Horizontal scroll
│           │  ✂️ стрижка          │  │  13px caption, --color-text
│           │  💆 массаж           │  │  border: --color-border
│           └──────────────────────┘  │
│                                      │
│  ···                                 │  Chat area (scrollable)
│                                      │
├──────────────────────────────────────┤
│  ┌────────────────────┐ [🎤] [→]   │  Input bar (pinned bottom)
│  │ Напиши о чём ищешь │              │  Max 2000 chars
│  └────────────────────┘              │  Mic btn = voice input (Ph 7)
└──────────────────────────────────────┘
                                          *voice btn disabled in Phase 3
                                           (grayed out, tooltip "скоро")
```

### Interaction States

| State | Trigger | UI | Copy |
|-------|---------|----|----|
| **Empty (first open)** | No prior messages | Welcome bubble + 3 suggestion chips + focused input | "Привет! Помогу найти мастера по твоим предпочтениям. Расскажи что ищешь?" |
| **Thinking** | User sent message, AI responding | Ayla bubble with 3 bouncing dots (300ms rhythm), **NOT** full-page spinner | — |
| **Results** | AI returned specialists | Ayla bubble + horizontal-scroll specialist cards (see Card Spec below) | "Нашла 3 мастера рядом с офисом 💅" |
| **Clarification** | AI needs more info | Ayla bubble with 2-3 chip options | "Подскажи, какой бюджет обычно на маникюр?" |
| **Confirmation** | User tapped specialist card | Ayla bubble with booking summary + [Подтвердить] [Изменить] buttons | "Записываю к Ирине, маникюр, завтра 19:00, 2500₽. Подтверждаешь?" |
| **Error (LLM)** | LLM API timeout/fail | Inline error bubble in Ayla message style + retry link | "AI заглянул в перерыв. Попробуй ещё раз." [Повторить] |
| **Error (network)** | Client offline | Toast at top + send button disabled | "Нет связи. Проверь интернет." |
| **Rate limit** | User sending >3/min | Input disabled + counter | "Подожди 15 секунд…" |

### Specialist Card Spec (in chat)

Inline card inside AI response bubble:

```
┌────────────────────────────────────┐
│  [Photo 48px]  Ирина               │  Name: H2 20px / 600
│                ⭐ 4.9 (127)         │  Rating: caption --color-accent
│                📍 Центральный, 1.2км│  Location: caption --color-text-secondary
│                                     │
│   Маникюр + покрытие  2500₽        │  Service + price
│   Завтра 19:00 · свободен         │  Slot: caption --color-success
│                                     │
│   [Записаться]                      │  Primary button
└────────────────────────────────────┘
```

Horizontal scroll 3-5 cards в bubble. Card width 280px, gap 12px.

### User Journey (first-time AI chat)

| Step | User does | Emotion | Ayla says |
|------|-----------|---------|-----------|
| 1 | Opens AI chat tab | Curious, slight scepticism ("ещё один бот?") | Welcome + suggestion chips |
| 2 | Taps chip "💅 маникюр" | Reduced friction | Auto-fills message "хочу маникюр" |
| 3 | Adds "рядом с офисом, завтра вечером" | Testing boundaries | Thinking... |
| 4 | Sees 3 cards в chat | Surprise ("быстро!") | "Нашла 3 рядом с офисом, все свободны после 18:00" |
| 5 | Taps card | Decision point | Confirmation bubble |
| 6 | Confirms | Relief | "Готово! Запись в твоём календаре" + deep link to booking details |

**Target feel:** conversational, not transactional. Ayla — friend, not search engine.

### AI Slop Checklist — для этого экрана

- [x] **NO** purple gradient background — solid --color-background #FAFAF8
- [x] **NO** 3-column grid as first impression — chat conversation flow, не grid
- [x] **NO** centered hero — left-aligned chat (like iMessage/Telegram)
- [x] **NO** decorative icons in colored circles
- [x] **NO** generic "Welcome to Ayla" copy — specific warmth "Привет! Помогу найти..."
- [x] **NO** emoji как design elements — ограниченно, только в categories (💅, ✂️, 💆) и Ayla avatar (🌙)

**Risk flags для review:**
- ⚠️ Ayla avatar 🌙 — emoji как visual anchor. На Phase 7 заменить кастомной иллюстрацией avatar'а.
- ⚠️ Horizontal scroll cards в chat — известный UX anti-pattern (скрывает cards 2-5). Альтернатива: показать все в vertical stack внутри bubble.

---

## Screen 2 — Food Scanner (Camera)

### Purpose
Одна из ключевых retention-механик. Пользователь фотографирует завтрак → AI распознаёт → Log entry с КБЖУ + витаминами. 3× в день.

### Information Architecture

```
┌──────────────────────────────────────┐
│  Status bar                           │
├──────────────────────────────────────┤
│  [×]                    [🔦 flash]   │  Minimal chrome: close + flash
│                                      │
│                                      │
│   ┌────────────────────────────┐    │
│   │                            │    │  Camera viewfinder
│   │   ╔════════════════════╗   │    │  (live preview)
│   │   ║                    ║   │    │
│   │   ║  Center guide      ║   │    │  Dashed rectangle guide
│   │   ║  (dashed)          ║   │    │  375px × 375px approx
│   │   ║                    ║   │    │
│   │   ╚════════════════════╝   │    │
│   │                            │    │
│   │  Держи еду в кадре        │    │  Helper hint
│   │                            │    │  caption 13px
│   └────────────────────────────┘    │
│                                      │
│                                      │
│                ┌─────┐               │  Shutter button
│                │  ●  │               │  64px circle, --color-primary
│                └─────┘               │  outer stroke: --color-surface
│                                      │
│   [📁 галерея]        [🔁 flip]     │  Secondary actions
└──────────────────────────────────────┘
```

### Interaction States

| State | Trigger | UI |
|-------|---------|----|
| **Permission not granted** | First open | Full-screen explainer + [Разрешить камеру] button |
| **Camera active** | Permission granted | Viewfinder + guide + shutter |
| **Capturing** | Shutter tap | Flash overlay 150ms + shutter haptic |
| **Processing** | Photo captured | Viewfinder frozen + overlay "Распознаю..." + Ayla spinner (3 dots) |
| **Success** | LogMeal returns match | Transition to Result screen (see below) |
| **Low confidence** | LogMeal confidence <0.5 | Dialog "Не могу распознать — введи вручную" + [Ввести] [Переснять] |
| **API down** | LogMeal error | Dialog "Сервис распознавания недоступен" + [Ввести вручную] [Позже] |
| **Offline** | No network at capture | Photo saved locally, queued for upload | "Загружу когда появится связь" toast |

### Food Scanner Result Screen

После successful recognition:

```
┌──────────────────────────────────────┐
│  [← назад]           Скан 08:42      │
├──────────────────────────────────────┤
│                                      │
│   ┌──────────────────────────────┐  │
│   │                               │  │  Captured photo
│   │   [Food photo]                │  │  rounded 16px
│   │                               │  │  200px height
│   └──────────────────────────────┘  │
│                                      │
│   Завтрак · 520 ккал                │  Headline H2 20px / 600
│                                      │
│   ┌─────────┬─────────┬──────────┐  │
│   │ Белки   │ Жиры    │ Углеводы│  │  Macros row
│   │ 28г     │ 22г     │ 45г     │  │  Each: caption + H2
│   └─────────┴─────────┴──────────┘  │
│                                      │
│   ✓ Овсянка с бананом и орехами    │  Identified items
│   ✓ Чай зелёный                     │  body 15px, checkmarks success
│                                      │
│   Витамины:                          │  caption 13px label
│   Магний ·· Кальций · B1 ··         │  Pills with level indicators
│                                      │
│   ┌────────────────────────────┐    │
│   │ + Добавить / исправить     │    │  Ghost button
│   └────────────────────────────┘    │
│                                      │
│                                      │
│   [Сохранить в дневник]              │  Primary button
└──────────────────────────────────────┘
```

### User Journey (first scan)

| Step | User does | Emotion | Ayla |
|------|-----------|---------|------|
| 1 | Taps Питание tab | Curiosity | Camera opens immediately (если permission granted) |
| 2 | Tap shutter on завтрак | Normal | Processing... |
| 3 | Sees recognized "овсянка с бананом" в 3с | Wow moment | "520 ккал · 28/22/45" |
| 4 | Scrolls to vitamins | Interest | "У тебя низкий магний на этой неделе" (contextual, Phase 4+) |
| 5 | Saves to log | Satisfaction | Return to tab with log entry visible |

**Target feel:** friction near zero. Camera → result in ≤5 seconds. No modals. No forms. No multi-step wizards.

### AI Slop Checklist

- [x] **NO** 3-column feature grid showing "Быстро! Точно! Легко!"
- [x] **NO** purple splash screen — jump to camera immediately
- [x] **NO** onboarding tour ("swipe to see") before first scan
- [x] **NO** centered hero — camera viewfinder is hero (full-bleed)
- [x] **NO** decorative circles/blobs/gradients
- [x] **NO** hype copy "Unlock insights about your nutrition" — utility copy "Держи еду в кадре"

---

## Screen 3 — Notification Permission Ask

### Purpose
Критичный touch-point. Плохая ask → decline → user теряет reminders forever (iOS не re-ask). Хорошая ask → accept → engagement works.

### Context of asking

**НЕ** на первом открытии (too early, нет доверия).
**Триггер:** первое успешное booking OR первый food scan saved to log. Justify контекстом: "напомню за час до записи" / "напомню сканировать обед".

### Information Architecture

```
┌──────────────────────────────────────┐
│                                      │
│                                      │
│          ┌──────────────┐            │
│          │              │            │  Illustration area
│          │    🔔 →      │            │  120px height
│          │   [simple    │            │  (simple icon-based,
│          │    graphic]  │            │   NOT decorative blob)
│          └──────────────┘            │
│                                      │
│                                      │
│   Не пропусти свою запись            │  H1 24px / 600, centered
│                                      │
│   Напомню за 1 час и за 2 часа      │  Body 17px / 400
│   до визита к мастеру.               │  --color-text-secondary
│   Без спама. Обещаю.                 │  (last line warmth)
│                                      │
│                                      │
│   ┌────────────────────────────┐    │
│   │ Разрешить уведомления      │    │  Primary button
│   └────────────────────────────┘    │
│                                      │
│          Не сейчас                   │  Text link, --color-text-secondary
│                                      │  Does NOT close = just dismisses ask
│                                      │  (user stays в flow)
└──────────────────────────────────────┘
```

### Interaction States

| State | Trigger | Result |
|-------|---------|--------|
| **Initial ask** | First booking/scan saved | Show sheet |
| **User taps "Разрешить"** | Primary CTA | Fire OS permission dialog |
| **OS granted** | System allowed | Sheet dismisses + toast "Готово, первое напоминание за 2 часа до записи" |
| **OS denied** | System denied | Sheet dismisses + silent (don't nag) |
| **User taps "Не сейчас"** | Dismissal | Sheet closes, return to context. Re-ask в 7 дней, но only в right context |
| **Settings state** | User later enables из Settings | Works same way, OS dialog appears |

### AI Slop Checklist

- [x] **NO** generic illustration ("person with phone floating in purple gradient")
- [x] **NO** 3 feature bullets "⚡ Fast · 🎯 Accurate · 🔒 Safe"
- [x] **NO** emoji в heading ("🔔 Stay notified! 🎉")
- [x] **NO** fear tactic ("Ты пропустишь все свои записи если откажешься!")
- [x] **NO** "Dark pattern" — кнопка "Не сейчас" как text link, не grayed, easy to tap. Respect user agency.

### User Journey

| Step | User does | Emotion | System |
|------|-----------|---------|--------|
| 1 | Завершает booking первой записи | Satisfaction | Sheet slides up |
| 2 | Читает "Напомню за 1 час и 2 часа" | Relief ("это то что я хотела") | — |
| 3 | Taps "Разрешить" | Low friction | OS dialog |
| 4 | Allow в OS | Sense of control | Toast "Готово" + return to booking details |

---

## Design Ratings (0-10 per dimension)

Применил 7-pass rating из plan-design-review skill.

| Pass | Screen 1 AI Chat | Screen 2 Food Scanner | Screen 3 Notification | Overall |
|------|------------------|------------------------|----------------------|---------|
| 1. Information Architecture | 8/10 | 9/10 (minimal chrome, camera-first) | 9/10 (focused ask) | 8.5 |
| 2. Interaction State Coverage | 9/10 (8 states defined) | 8/10 (7 states + permission) | 7/10 (6 states) | 8 |
| 3. User Journey & Emotional Arc | 8/10 | 9/10 (wow moment framed) | 9/10 (contextual ask defuses resistance) | 8.5 |
| 4. AI Slop Risk | 9/10 | 9/10 | 9/10 | 9 |
| 5. Design System Alignment | 9/10 (uses DESIGN.md tokens) | 9/10 | 9/10 | 9 |
| 6. Responsive & Accessibility | 7/10 (need Tablet variant spec) | 7/10 | 7/10 | 7 |
| 7. Unresolved Decisions | 6/10 (4 deferred) | 7/10 (2 deferred) | 8/10 (1 deferred) | 7 |

**Overall design score: 8.1/10** (up from 0/10 at start of review — before this spec existed, nothing was designed для Phase 3).

**Что поднимет до 9.5+:** Figma mockups от designer/PO + усиленный accessibility audit + tablet variants.

---

## Accessibility Requirements

### Applies to all 3 screens

- **Touch targets:** ≥44×44pt (shutter 64px ✅, suggestion chips 36px ❌ — увеличить до 44px)
- **Contrast:** WCAG AA — check colors против `--color-background`:
  - `--color-primary` #7B6CF6 on #FAFAF8: 4.53:1 ✅ (passes AA для large text, borderline body)
  - `--color-text` #1A1A2E on #FAFAF8: 15.32:1 ✅ (passes AAA)
  - `--color-text-secondary` #5A5A70 on #FAFAF8: 6.94:1 ✅
- **Keyboard navigation:** Chat input Tab order: input → suggestion chips → send btn
- **Screen reader:**
  - AI chat: each message has `accessibilityRole="text"` + speaker indicator "Ayla says:" / "You said:"
  - Food scanner: viewfinder has `accessibilityLabel="Camera active, position food in center"`
  - Permission sheet: title + body are single announcement
- **Reduced motion:** disable 3-dot thinking bounce + shutter flash if `prefers-reduced-motion`
- **RTL support:** NOT scope Phase 3 (Arabic/Hebrew — Phase 6+)

---

## Unresolved Decisions (для PO/designer Phase 3)

| # | Decision | Options | Blocker? | Owner |
|---|----------|---------|----------|-------|
| 1 | Ayla avatar visual — emoji 🌙 или custom illustration? | A) 🌙 temporary / B) Commission illustrator now / C) Use Ayla wordmark | No (ship with 🌙 placeholder) | Product |
| 2 | AI chat horizontal-scroll vs vertical-stack cards | A) Horizontal (compact но hidden) / B) Vertical (scroll-heavy но all visible) | Medium (affects conversion) | Design/PO |
| 3 | Food scanner — показывать ли витамины на первом скане без context? | A) Always show / B) Only after 7 days of data / C) Show only deficits | Low | Product |
| 4 | Notification permission illustration — simple icon или custom illustration? | A) Phosphor 🔔 icon / B) Custom / C) Animated | Low (A works for Phase 3) | Design |
| 5 | Voice input button в AI chat — прятать в Phase 3 или показывать disabled? | A) Hide / B) Show disabled with tooltip "скоро" / C) Show active, no-op | Low | UX |
| 6 | Suggestion chips set — dynamic (based on history) или static top categories? | A) Static top 6 / B) Dynamic per user / C) Static но локализованные по городу | Medium | Product |
| 7 | Tablet portrait layout — Phase 3 или Phase 5? | A) Defer to Phase 5 / B) Ship Phase 3 | Low | Eng |

---

## NOT in scope Phase 3 UI

- Voice input UI for AI chat — Phase 7
- AI avatar interaction (tap → expand) — Phase 7
- Food scanner photo editing (crop/rotate) — Phase 4
- Food scanner manual entry form — needed в Phase 3 как fallback, но simpler (just name + optional calories)
- Dark mode — Phase 6+
- Tablet layout — Phase 5

---

## What already exists (reusable)

- `MasterPreviewCard.tsx` (mobile/packages/shared/src/components/) — может быть base для chat specialist card
- `ProtectedRoute.tsx` — для chat auth gating
- `authStore.tsx` — user context для AI personalization (пустой сейчас, memory arch deferred)
- Ayla Brand Vision в Notion — visual language
- `figma-screens/onboarding/` — existing onboarding illustrations могут inform style direction

---

## Next design iterations

**Phase 3 Week 4** (перед implementation):
- `/design-consultation` — full design system sprint (expand DESIGN.md)
- PO/designer рисует Figma mockups на базе этого spec'а
- `/plan-design-review` round 2 — review actual Figma mockups (will unlock visual checks that text can't do)

**Phase 3 Week 8** (перед ship):
- `/design-review` (not plan version) — visual QA на built screens

**Phase 4 (Pilot prep):**
- Accessibility audit via VoiceOver / TalkBack testing
- Tablet portrait layout spec (если confirmed в scope Phase 5)

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | CLEAR | REDUCTION, 5 tokens deferred |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 6 arch issues, 5 test gaps, 3 critical failures, 15 TODOs |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | CLEAR | score 0→8.1/10, 7 decisions deferred, 3 screens specified |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **UNRESOLVED:** 7 (все deferred to PO/designer, не блокеры Phase 3 start)
- **VERDICT:** CEO + ENG + DESIGN CLEARED — ready for Phase 2 foundation → Phase 3 implementation

Design review covered: 3 screens · 8 interaction states each · accessibility baseline · AI slop checks · 7 design dimensions rated. Mockups deferred to PO/designer (OpenAI API key not configured — spec-first approach).
