# DESIGN.md — Ayla Design System

**Версия:** 0.1 (baseline)
**Дата:** 2026-04-24
**Статус:** Draft baseline from Ayla Brand Vision (Notion 2026-03-28)
**Scope:** Ayla mobile (🟢 Client) + Ayla Pro (🟣 Specialist)

> Этот документ — источник истины для дизайн-решений. До первой полноценной design-consultation sprint'а содержит только baseline tokens. Расширяем по мере развития продукта.

---

## Brand Voice

**Позиционирование:** «Ayla — твой AI-компаньон по качеству жизни.»

**Brand personality** (из Brand Vision):
- **Умная** — рекомендации на данных, не шаблонах
- **Тёплая** — тон подруги, не уведомление
- **Честная** — "тебе не хватает витамина C" без сахара
- **Действующая** — каждая рекомендация имеет конкретный next step
- **Растущая** — помнит историю, видит прогресс

**Не должна выглядеть:** как холодный AI · как медицинский app · как SaaS template · как Dikidi/YCLIENTS

---

## Color System

### Primary palette

| Role | Token | Hex | RGB | Usage |
|------|-------|-----|-----|-------|
| Primary | `--color-primary` | `#7B6CF6` | `123,108,246` | CTA buttons, active states, AI accent |
| Secondary | `--color-secondary` | `#F5E6D3` | `245,230,211` | Beauty warmth, subtle backgrounds, cards |
| Accent | `--color-accent` | `#E8A598` | `232,165,152` | Feminine touches, highlights, success warmth |
| Background | `--color-background` | `#FAFAF8` | `250,250,248` | Page background (почти белый, не stark white) |
| Text primary | `--color-text` | `#1A1A2E` | `26,26,46` | Headlines, body (тёмно-синий, не чёрный) |

### Semantic palette (extended)

| Role | Token | Hex | Notes |
|------|-------|-----|-------|
| Text secondary | `--color-text-secondary` | `#5A5A70` | Subtle copy, metadata |
| Border | `--color-border` | `#E8E4D8` | Subtle dividers, cards |
| Surface | `--color-surface` | `#FFFFFF` | Cards, sheets (on --color-background) |
| Error | `--color-error` | `#D4502E` | Destructive, validation failures |
| Success | `--color-success` | `#5A8C6A` | Confirmations (не ярко-зелёный) |
| Warning | `--color-warning` | `#D49B2E` | Non-critical alerts |

### Rules
- **NEVER** use default purple-on-white defaults → используй `--color-primary` строго
- **NEVER** пиши hex inline в коде → всегда через CSS variables / RN theme tokens
- Accent colors не использовать для `body` text — только для highlights ≤5 слов

---

## Typography

### Font stack

| Role | Token | Font | Fallback | Notes |
|------|-------|------|----------|-------|
| Display (logo, hero) | `--font-display` | `Inter` | `-apple-system, sans-serif` | Строчные буквы для логотипа "ayla" |
| UI primary | `--font-ui` | `Inter` | `-apple-system, sans-serif` | Экраны, кнопки, labels |
| UI secondary (iOS) | — | `SF Pro` | — | Use SF Pro if Inter unavailable на iOS |

**NEVER** fallback на `system-ui` / `-apple-system` as PRIMARY — это "I gave up on typography" signal. Inter — обязателен, грузим через expo-font.

### Type scale (Mobile, Ayla client)

| Role | Size | Weight | Line-height | Usage |
|------|------|--------|-------------|-------|
| Display | 32px | 700 | 1.2 | Таб-заголовки, AI-аватар screen |
| H1 | 24px | 600 | 1.3 | Onboarding steps, major sections |
| H2 | 20px | 600 | 1.35 | Мастер карточка title |
| Body Large | 17px | 400 | 1.5 | Primary body, AI chat messages |
| Body | 15px | 400 | 1.5 | Secondary body, metadata |
| Caption | 13px | 500 | 1.4 | Labels, chips, timestamps |
| Micro | 11px | 500 | 1.3 | Badges, counters |

**Rules:**
- Minimum body text **16px** (accessibility — контраст + readability)
- Minimum contrast ratio **4.5:1** on body text
- Weight под 500 для mobile (300 weight = нечитаемо на small screens)
- Line-height ≥1.4 для всего что больше 2 строк

---

## Spacing Scale

Base unit: **4px**. Scale через multiples.

| Token | Size | Usage |
|-------|------|-------|
| `--space-1` | 4px | Icon padding, tight inline |
| `--space-2` | 8px | Between related elements |
| `--space-3` | 12px | Button inner padding |
| `--space-4` | 16px | Standard gap, card inner |
| `--space-5` | 20px | — |
| `--space-6` | 24px | Section gap |
| `--space-8` | 32px | Screen padding (mobile) |
| `--space-10` | 40px | Major section gap |
| `--space-16` | 64px | Hero spacing |

**Screen edge padding:** 16px (`--space-4`) по умолчанию mobile. 24px на iPad portrait.

---

## Corner Radius

| Token | Size | Usage |
|-------|------|-------|
| `--radius-sm` | 4px | Input fields, chips |
| `--radius-md` | 8px | Buttons, cards |
| `--radius-lg` | 16px | Bottom sheets, modals |
| `--radius-xl` | 24px | Avatar containers, hero cards |
| `--radius-full` | 9999px | Avatars, pills, FAB |

**Rules:**
- **NEVER** apply `--radius-xl` на everything (bubbly-everywhere = AI-slop signal)
- Buttons consistent `--radius-md` (8px)
- Avatars только `--radius-full` (круглые, not squircle)

---

## Elevation (Shadows)

Минималистично — Ayla не SaaS.

| Token | Shadow | Usage |
|-------|--------|-------|
| `--shadow-sm` | `0 1px 2px rgba(26,26,46,0.04)` | Subtle cards |
| `--shadow-md` | `0 2px 8px rgba(26,26,46,0.06)` | Elevated cards, sheets |
| `--shadow-lg` | `0 8px 24px rgba(26,26,46,0.08)` | Modals, dropdown menus |

**NEVER** add decorative drop shadows на buttons / icons. Flat. Clean.

---

## Motion

Motion покrывает 2-3 intentional use cases, не ornament.

| Role | Duration | Easing | Usage |
|------|----------|--------|-------|
| Enter | 200ms | `cubic-bezier(0.25, 0.1, 0.25, 1.0)` | Screen transitions |
| Exit | 150ms | `cubic-bezier(0.4, 0.0, 1, 1)` | Dismiss sheets, modals |
| Emphasis | 400ms | `cubic-bezier(0.16, 1, 0.3, 1)` | AI response arrival (gentle bounce) |

**Reduced Motion:** respect `prefers-reduced-motion` — disable emphasis animation, keep functional transitions.

---

## Iconography

- **Library:** Phosphor Icons (light weight default) или custom Ayla icons
- **Stroke width:** 1.5px default (UI), 2px для tab bar active
- **Size:** 24px default, 20px inline, 32px hero/feature
- **NEVER** icons in coloured circles as section decoration (AI slop pattern #3)

---

## Layout Grid (Mobile)

```
┌─────────────────────────────┐  ← viewport 375px (iPhone SE baseline)
│                             │
│  16px screen edge padding   │
│  ┌───────────────────────┐  │
│  │                       │  │
│  │  Content area         │  │
│  │  (343px wide)         │  │
│  │                       │  │
│  └───────────────────────┘  │
│                             │
│  [Bottom tab bar — 83px]    │
└─────────────────────────────┘
```

**Breakpoints:**
- **Mobile portrait:** 320-428px (primary)
- **Mobile landscape:** 568-926px (не primary, minimal support)
- **Tablet portrait:** 768-834px (Phase 6+)

---

## Component Tokens (preview)

### Primary Button
```
Background: var(--color-primary) — #7B6CF6
Text: var(--color-surface) — #FFFFFF
Height: 48px
Padding: 12px 24px
Border-radius: var(--radius-md) — 8px
Font: var(--font-ui) 17px / 600 weight
Disabled opacity: 0.5
Active state: scale(0.98), opacity 0.9
```

### AI Chat Message (incoming)
```
Background: var(--color-secondary) — #F5E6D3 (warm beige)
Text: var(--color-text) — #1A1A2E
Padding: 12px 16px
Border-radius: var(--radius-lg) 16px, except top-left = 4px (bubble tail)
Max-width: 75% of content area
Avatar: 32px круг слева, Ayla mark icon
```

### AI Chat Message (outgoing, from user)
```
Background: var(--color-primary) — #7B6CF6
Text: var(--color-surface) — #FFFFFF
Padding: 12px 16px
Border-radius: var(--radius-lg) 16px, except top-right = 4px
Max-width: 75% of content area
Align: right
```

---

## Accessibility Requirements

- **Touch targets:** Minimum 44×44pt (Apple HIG) / 48×48dp (Material)
- **Contrast:** WCAG AA minimum (4.5:1 body, 3:1 large text)
- **Keyboard navigation:** Tab order logical, focus states visible (border + shadow)
- **Screen reader:** ARIA labels на всех interactive elements, `accessibilityRole` в RN
- **Reduced motion:** respect system preference
- **Dark mode:** NOT in scope до Phase 6+ (MVP light-mode only — tokens готовы к dark theme через CSS variables)

---

## AI Slop Blacklist (что НЕ делать)

Ayla дизайн **не должен содержать**:

1. ❌ Purple/violet gradient backgrounds (использовать solid primary, gradient только для AI-accent moments)
2. ❌ 3-column feature grid с иконками-в-кружочках
3. ❌ Centered everything (left-align for mobile, не симметрия)
4. ❌ Uniform bubbly radius на всех элементах
5. ❌ Decorative blobs / floating circles / wavy dividers
6. ❌ Emoji как design elements (🚀 в заголовках)
7. ❌ Colored left-border на cards
8. ❌ Generic copy ("Unlock the power of", "Your all-in-one")
9. ❌ Cookie-cutter rhythm (hero → 3 feat → testimonial → CTA)
10. ❌ system-ui / -apple-system as PRIMARY font — fallback only

**Защитная проверка:** если экран можно поставить в любой YC-startup pitch без замены логотипа — плохо, слишком generic.

---

## Next iteration

**Phase 2 design sprint** (рекомендуется через 2-3 недели):
- Full design consultation through `/design-consultation`
- Expanded component library (sheets, tabs, empty states, loading skeletons)
- Onboarding sequence redesign (existing figma-screens/onboarding/ — BeautyGO era, нужно ревизить под Ayla)
- Dark mode tokens (для Phase 6+)

**До design consultation:** используем эти tokens как baseline для Phase 3 UI mockups.
