---
title: Ayla Knowledge Architecture Specification
node_id: ayla.knowledge.architecture.v1.2
type: knowledge-architecture-specification
status: review
activation_status: pending-infrastructure
version: "1.2"
owner: Product Architecture
priority: P0
knowledge_area:
  - foundation
domain: []
system_owner:
  - ayla-knowledge
concerns:
  - knowledge-management
  - governance
created: 2026-07-17
updated: 2026-07-18
source_kind: canonical
classification: internal
data_sensitivity: none
security_sensitivity: low
ai_indexing: allowed
export_policy: full
tags:
  - ayla
  - ayla/foundation
  - ayla/knowledge
  - ayla/governance
  - type/specification
  - status/review
implements:
  - "[[Ayla Constitution v2.2]]"
  - "[[Ayla MVP Product Thesis v1.0 FINAL]]"
depends_on: []
used_by:
  - "[[Product MOC]]"
  - "[[Architecture MOC]]"
  - "[[Safety and Governance MOC]]"
supersedes:
  - "[[Ayla Knowledge Architecture Specification v1.0]]"
review_cycle: quarterly
---

# Ayla Knowledge Architecture Specification v1.2

## Статус документа

**Версия:** 1.2  
**Статус:** Review — Pending Infrastructure Activation  
**Приоритет:** P0  
**Владелец:** Product Architecture  
**Область действия:** вся продуктовая, архитектурная, UX, AI, safety, business и operational-документация Ayla  
**Каноническое размещение:** `docs/00 Foundation/Ayla Knowledge Architecture Specification v1.2.md`  
**Предыдущая редакция:** [[Ayla Knowledge Architecture Specification v1.0]]

---

# 1. Назначение документа

Этот документ определяет, как Ayla хранит, связывает, версионирует, проверяет и развивает собственные знания как компании и как AI-продукта.

Он отвечает на вопросы:

- где должен жить каждый тип документа;
- какие документы являются фундаментальными, а какие рабочими;
- как отличать гипотезу от утверждённого решения;
- как связывать продуктовые решения с архитектурой и кодом;
- как Obsidian используется как визуальный интерфейс к Git-репозиторию;
- какие метаданные обязательны;
- как строится граф знаний;
- как предотвращается появление дубликатов, противоречий и «мертвых» документов;
- как новая команда понимает, почему продукт устроен именно так;
- как в будущем подключить AI-поиск, RAG и полноценный Knowledge Graph.

Главная цель:

> Любое существенное решение Ayla должно иметь происхождение, владельца, статус, связи, последствия и путь до реализации.

---

# 2. Проблема, которую решает Knowledge Architecture

Без формальной архитектуры знаний документы быстро превращаются в набор разрозненных Markdown-файлов.

Типичный сценарий деградации:

1. Один документ фиксирует решение.
2. Другой документ повторяет его другими словами.
3. Третий документ меняет правило, но не обновляет первые два.
4. Разработчик реализует одну из версий.
5. UX работает по другой версии.
6. Через несколько месяцев никто не понимает, что является каноном.

Для Ayla эта проблема особенно опасна, потому что продукт одновременно включает:

- философию и этику;
- AI-оркестрацию;
- персональные данные;
- safety-ограничения;
- рекомендации;
- маркетплейс;
- booking;
- MAX Bot;
- Mini App;
- профессиональный контур;
- бизнес-модель;
- множество пользовательских сценариев.

Без Knowledge Architecture команда рискует получить:

- противоречивые требования;
- скрытые нарушения Конституции;
- несовместимые ADR;
- дублирование логики;
- архитектурный дрейф;
- потерю решений при смене участников;
- невозможность объяснить поведение AI;
- дорогостоящие переделки.

---

# 3. Основные принципы

## 3.1. Git является источником истины

Канонические документы хранятся в Git-репозитории в Markdown.

Obsidian не становится отдельным источником истины. Он является интерфейсом к тем же файлам.

Правило:

```text
Git repository = canonical source
Obsidian = human knowledge interface
GitHub / GitLab = review and version control
```

Недопустимо:

- хранить актуальную версию только локально в Obsidian;
- держать разные содержательные версии одного документа в Notion, Google Docs и Git;
- считать устное решение достаточным без фиксации.

---

## 3.2. Один документ — одна ответственность

Каждый документ должен отвечать на один основной вопрос.

Примеры:

- Конституция: какие принципы нельзя нарушать;
- Product Thesis: что строим в MVP;
- User Journey: как должен проходить путь пользователя;
- Intent Model: как Ayla структурирует намерение;
- ADR: почему принято архитектурное решение;
- Runbook: как выполнить операционное действие;
- Handoff: какое состояние конкретной работы передаётся другой команде.

Если документ пытается одновременно быть:

- философией;
- PRD;
- архитектурой;
- API-контрактом;
- инструкцией для QA;

его необходимо разделить.

---

## 3.3. Статус решения должен быть явным

Каждый документ обязан иметь статус.

Допустимые статусы:

| Статус | Значение |
|---|---|
| `idea` | неоформленная идея, не основание для реализации |
| `draft` | рабочий черновик |
| `review` | документ готов к проверке |
| `approved-with-amendments` | основа утверждена, обязательны перечисленные поправки |
| `approved` | канонический документ |
| `implemented` | утверждён и реализован |
| `deprecated` | больше не рекомендуется к использованию |
| `superseded` | заменён новым документом |
| `archived` | исторический документ без операционной силы |

Статус не должен определяться по названию файла или по памяти команды.

---

## 3.4. Связи важнее папок

Папки помогают ориентироваться человеку, но не выражают смысловых отношений.

Knowledge Graph строится через явные связи:

- `implements`;
- `depends_on`;
- `used_by`;
- `constrains`;
- `defines`;
- `extends`;
- `supersedes`;
- `conflicts_with`;
- `evidenced_by`;
- `validated_by`.

Пример:

```yaml
implements:
  - "[[Ayla Constitution v2.2]]"

depends_on:
  - "[[product/user-journeys/ayla-user-journey-specification-v1.1|Ayla User Journey Specification v1.1]]"

used_by:
  - "[[Ayla Recommendation Engine Specification v1.0]]"
```

---

## 3.5. Решения должны быть трассируемыми

Для существенного продуктового или архитектурного решения должна существовать цепочка:

```text
Принцип
↓
Продуктовое решение
↓
Спецификация
↓
ADR
↓
Модель данных / API
↓
Код
↓
Тест
↓
Метрика
```

Не каждый документ обязан включать всю цепочку, но система должна позволять её восстановить.

---

## 3.6. Документация должна быть пригодна и человеку, и AI

Markdown должен быть:

- структурированным;
- однозначным;
- разделённым на небольшие смысловые блоки;
- снабжённым метаданными;
- без скрытых смыслов в изображениях;
- с текстовым описанием схем;
- с устойчивыми заголовками.

Это позволит в будущем:

- строить RAG;
- индексировать документы;
- автоматически проверять противоречия;
- генерировать граф связей;
- создавать onboarding для новых сотрудников;
- использовать AI-агентов для ревью решений.

---

# 4. Границы действия

Настоящая спецификация регулирует:

- фундаментальные документы;
- продуктовые документы;
- UX-документы;
- AI-спецификации;
- domain models;
- архитектурные документы;
- ADR;
- safety и privacy-документы;
- business-документы;
- research;
- operational runbooks;
- migration plans;
- handoff-документы;
- источники и архивы.

Она не регулирует напрямую:

- исходный код;
- структуру базы данных;
- CI/CD;
- issue tracker;
- Slack или другие рабочие чаты.

Однако эти системы должны ссылаться на канонические документы, когда задача опирается на зафиксированное решение.

---

# 5. Каноническая структура репозитория

Рекомендуемая структура:

```text
docs/
├── 00 Foundation/
│   ├── Ayla Constitution v2.2.md
│   ├── Ayla Knowledge Architecture Specification v1.0.md
│   ├── Ayla Vision.md
│   ├── Ayla Principles.md
│   ├── Glossary.md
│   └── Foundation MOC.md
│
├── 01 Product/
│   ├── Foundations/
│   │   └── product-level foundation documents
│   ├── PRD/
│   ├── User Journeys/
│   ├── Product Metrics/
│   └── Product MOC.md
│
├── 02 Strategy/
│   ├── Product Strategy/
│   ├── Validation/
│   ├── Roadmaps/
│   ├── Market/
│   └── Strategy MOC.md
│
├── 03 AI System/
│   ├── Intent Model/
│   ├── Recommendation Engine/
│   ├── Orchestrator/
│   ├── Memory/
│   ├── Prompts/
│   ├── Evaluations/
│   └── AI System MOC.md
│
├── 04 Domain Models/
│   ├── User/
│   ├── Goals/
│   ├── Intent/
│   ├── Recommendation/
│   ├── Providers/
│   ├── Booking/
│   └── Domain Models MOC.md
│
├── 05 Architecture/
│   ├── System/
│   ├── Data/
│   ├── Integrations/
│   ├── Security/
│   ├── ADR/
│   └── Architecture MOC.md
│
├── 06 Safety and Governance/
│   ├── Safety/
│   ├── Privacy/
│   ├── Consent/
│   ├── Memory Governance/
│   ├── Incident Management/
│   └── Safety and Governance MOC.md
│
├── 07 Design/
│   ├── UX Principles/
│   ├── Conversation Design/
│   ├── Design System/
│   ├── Screen Specifications/
│   └── Design MOC.md
│
├── 08 Business/
│   ├── Monetization/
│   ├── Provider Economics/
│   ├── Billing/
│   ├── Attribution/
│   └── Business MOC.md
│
├── 09 Research/
│   ├── User Research/
│   ├── Competitive Research/
│   ├── Experiments/
│   ├── Evidence/
│   └── Research MOC.md
│
├── 10 Operations/
│   ├── Pilot/
│   ├── Runbooks/
│   ├── Support/
│   ├── Migration/
│   ├── Handoffs/
│   └── Operations MOC.md
│
└── 90 Sources/
    ├── External/
    ├── Imported/
    ├── Historical/
    └── Sources MOC.md
```

## Почему используется нумерация

Нумерация:

- задаёт стабильный порядок;
- отделяет фундамент от рабочих документов;
- облегчает навигацию;
- не влияет на смысловые связи;
- уменьшает хаос при росте числа файлов.

---

# 6. Назначение разделов

## 6.1. `00 Foundation`

Содержит самые стабильные документы.

Примеры:

- Конституция;
- миссия;
- принципы;
- архитектура знаний;
- словарь.

Документы этого уровня:

- меняются редко;
- имеют высокий порог утверждения;
- ограничивают документы нижних уровней;
- не должны содержать временные тарифы, API-поля или детали реализации.

---

## 6.2. `01 Product`

Описывает продуктовую модель.

Содержит:

- Product Thesis;
- PRD;
- User Journey;
- product metrics;
- feature definitions;
- scope.

Главный вопрос раздела:

> Что пользователь должен получить и как продукт создаёт эту ценность?

---

## 6.3. `02 Strategy`

Содержит:

- гипотезы;
- validation plans;
- roadmap;
- market strategy;
- launch strategy;
- growth strategy.

Главный вопрос:

> Как мы проверяем и развиваем продукт во времени?

---

## 6.4. `03 AI System`

Описывает поведение AI-ядра.

Содержит:

- Intent Model;
- Recommendation Engine;
- AI Orchestrator;
- memory processing;
- prompt architecture;
- evaluation framework;
- model routing;
- guardrails.

Главный вопрос:

> Как AI понимает, решает, объясняет и ограничивает себя?

---

## 6.5. `04 Domain Models`

Описывает понятия предметной области независимо от интерфейса и конкретной БД.

Примеры сущностей:

- User;
- Goal;
- Intent;
- Constraint;
- Preference;
- Recommendation;
- Provider;
- Service;
- Booking;
- Outcome;
- Feedback.

Главный вопрос:

> Какие сущности существуют в мире Ayla и как они связаны?

---

## 6.6. `05 Architecture`

Содержит:

- system architecture;
- data architecture;
- integration contracts;
- deployment architecture;
- ADR;
- security architecture.

Главный вопрос:

> Как продукт реализован технически и почему выбран именно этот подход?

---

## 6.7. `06 Safety and Governance`

Содержит:

- competence boundaries;
- privacy;
- consent;
- safety rules;
- incident response;
- memory governance;
- audit;
- moderation.

Главный вопрос:

> Как Ayla предотвращает вред, обеспечивает контроль и соблюдает ограничения?

---

## 6.8. `07 Design`

Содержит:

- design system;
- UX principles;
- conversation design;
- screen behavior specifications;
- interaction patterns.

Главный вопрос:

> Как решение проявляется в интерфейсе и диалоге?

---

## 6.9. `08 Business`

Содержит:

- monetization;
- tariffs;
- billing events;
- provider economics;
- attribution;
- marketplace rules.

Главный вопрос:

> Как продукт зарабатывает, не нарушая нейтральность и интересы пользователя?

---

## 6.10. `09 Research`

Содержит доказательства и исследования.

Примеры:

- интервью;
- market research;
- experiment reports;
- usability studies;
- model evaluations;
- source evidence.

Главный вопрос:

> На каких данных основано решение?

---

## 6.11. `10 Operations`

Содержит применимые инструкции:

- pilot playbooks;
- onboarding;
- support;
- migration plans;
- incident runbooks;
- handoff documents.

Главный вопрос:

> Как команда выполняет работу на практике?

---

## 6.12. `90 Sources`

Содержит:

- внешние документы;
- импортированные материалы;
- исторические копии;
- необработанные источники.

Документы из `90 Sources` не считаются каноническими решениями, пока их содержание не перенесено в соответствующую спецификацию или ADR.

---

# 7. Типы документов

Каждый документ обязан иметь `type`.

Рекомендуемый словарь:

| Тип | Назначение |
|---|---|
| `foundation` | фундаментальный принцип или документ |
| `product-thesis` | продуктовая гипотеза и scope |
| `prd` | требования к продуктовой возможности |
| `user-journey` | путь пользователя |
| `ux-behavior-specification` | поведенческая UX-спецификация |
| `ai-specification` | AI-модель или AI-слой |
| `domain-model` | сущности и связи предметной области |
| `architecture-specification` | описание архитектуры |
| `adr` | архитектурное решение |
| `safety-specification` | safety-правила |
| `privacy-policy-internal` | внутренняя политика данных |
| `business-specification` | коммерческая логика |
| `research` | исследование |
| `experiment` | эксперимент |
| `runbook` | операционная инструкция |
| `handoff` | передача состояния работы |
| `migration-plan` | план перехода |
| `moc` | Map of Content |
| `source` | внешний или исходный материал |

Новый тип добавляется только при невозможности использовать существующий.

---

# 8. Обязательные метаданные

Каждый канонический документ должен начинаться с YAML frontmatter.

Минимальный шаблон:

```yaml
---
node_id:
title:
type:
status:
version:
owner:
priority:
knowledge_area:
domain:
system_owner:
source_repository:
concerns:
created:
updated:
source_kind:
classification:
data_sensitivity:
data_categories:
security_sensitivity:
ai_indexing:
export_policy:
tags:
implements:
depends_on:
used_by:
supersedes:
review_cycle:
---
```

## Описание полей

### `title`

Человекочитаемое название.

### `node_id`

Стабильный неизменяемый идентификатор узла. Он не зависит от filename или
title и обязан быть уникальным во всей knowledge base.

### `type`

Тип документа из утверждённого словаря.

### `status`

Текущий статус.

### `version`

Версия документа.

Рекомендуется semantic-style:

- `1.0`;
- `1.1`;
- `2.0`.

### `owner`

Роль или конкретный владелец.

Не рекомендуется использовать абстрактное `team`.

### `priority`

Допустимые значения:

- `P0`;
- `P1`;
- `P2`;
- `P3`.

### `knowledge_area`, `domain`, `system_owner`, `source_repository`, `concerns`

Эти поля описывают разные измерения документа:

- `knowledge_area` — положение в архитектуре знаний;
- `domain` — бизнес- или системный bounded context;
- `system_owner` — архитектурная система или bounded system, которой
  принадлежит описываемая ответственность;
- `source_repository` — Git-репозиторий канонического документа или исходного
  mirror; поле не заменяет `system_owner`;
- `concerns` — сквозные аспекты.

```yaml
knowledge_area:
  - ai-system
domain:
  - recommendation
system_owner:
  - ayla-recommendation
source_repository: ayla-ai-core
concerns:
  - safety
  - explainability
```

Канонические значения определены в разделе 38.4 и
`docs/.knowledge/schema.yaml`.

### `created` и `updated`

Дата в формате `YYYY-MM-DD`.

### `source_kind`

Допустимые значения:

- `canonical`;
- `mirror`;
- `external`.

`mirror` означает копию канонического документа из другого репозитория.

### Data and export policy

Поля `classification`, `data_sensitivity`, `security_sensitivity`,
`ai_indexing` и `export_policy` обязательны. Их допустимые значения и
deny-by-default правила определены в разделе 38.5 и machine-readable schema.

### `tags`

Используются для навигации, но не заменяют типы и связи.

### `implements`

Какой принцип или документ реализует этот документ.

### `depends_on`

Без каких документов текущий документ нельзя корректно использовать.

### `used_by`

Какие документы или системы используют текущий документ.

### `supersedes`

Какой документ заменён.

### `review_cycle`

Период проверки:

- `monthly`;
- `quarterly`;
- `yearly`;
- `before-major-change`;
- `event-driven`.

---

# 9. Канонический документ и mirror

В Ayla возможны несколько репозиториев.

Поэтому вводится правило:

> Один документ может иметь только один канонический источник.

Остальные копии должны иметь:

```yaml
source_kind: mirror
source: <путь или URL канонического файла>
synced: 2026-07-17
```

Mirror:

- не редактируется вручную;
- обновляется из канона;
- явно показывает дату синхронизации;
- не используется для конфликтующих изменений.

Если документ переносится в новый репозиторий, необходимо отдельно зафиксировать смену канонического источника.

---

# 10. Именование файлов

Рекомендуемый формат:

```text
Ayla <Document Name> v<Version>.md
```

Примеры:

```text
Ayla Constitution v2.2.md
Ayla User Journey Specification v1.1.md
Ayla Intent Model Specification v1.0.md
ADR-0012 Dynamic User Model.md
```

Не использовать:

- `final-final.md`;
- `new.md`;
- `copy.md`;
- `latest.md`;
- `document1.md`;
- даты вместо версии, если документ имеет нормативный статус.

Дата допустима для:

- research;
- handoff;
- experiment;
- meeting notes;
- migration snapshots.

Пример:

```text
2026-07-17 Intent Model Review.md
```

---

# 11. Версионирование

## 11.1. Major version

Повышается при изменении фундаментального смысла.

Пример:

`1.x → 2.0`

Когда:

- меняется scope;
- меняется модель ответственности;
- меняется основная архитектура;
- новая версия несовместима со старой.

## 11.2. Minor version

Повышается при значимом расширении без изменения основного принципа.

Пример:

`1.0 → 1.1`

Когда:

- добавлены edge cases;
- уточнена матрица решений;
- добавлена трассируемость;
- закрыты amendments.

## 11.3. Patch version

Для документов обычно не требуется.

Мелкие орфографические изменения фиксируются commit history.

---

# 12. Жизненный цикл документа

```text
Idea
↓
Draft
↓
Review
↓
Approved / Approved with Amendments
↓
Implemented
↓
Deprecated / Superseded
↓
Archived
```

## Правило перехода

Документ не получает статус `approved`, пока:

- не определён владелец;
- не разрешены критические противоречия;
- не указаны зависимости;
- не определён способ проверки;
- не зафиксированы открытые вопросы или amendments.

---

# 13. Map of Content

MOC — это не папка и не простой список файлов.

MOC должен объяснять структуру области.

Каждый MOC содержит:

1. Назначение области.
2. Канонические документы.
3. Документы в работе.
4. Зависимости.
5. Открытые пробелы.
6. Недавние изменения.
7. Рекомендуемый порядок чтения.

Пример:

```markdown
# AI System MOC

## Назначение

Документы, определяющие поведение AI-ядра Ayla.

## Порядок чтения

1. [[Ayla Intent Model Specification v1.0]]
2. [[Ayla Recommendation Engine Specification v1.0]]
3. [[Ayla AI Orchestrator Architecture v1.0]]
4. [[ADR-0012 Dynamic User Model]]

## Approved

- ...

## Draft

- ...

## Open gaps

- Evaluation Framework
- Model Routing Policy
```

---

# 14. Правила ссылок

Для смысловых связей используются Obsidian wikilinks:

```markdown
[[Ayla Constitution v2.2]]
```

Для конкретного раздела:

```markdown
[[Ayla Constitution v2.2#Статья XII. Границы компетенции]]
```

Для внешних ресурсов используются обычные Markdown-ссылки.

Правило:

- внутренняя сущность — wikilink;
- внешний источник — URL;
- код — путь в репозитории;
- issue — ссылка на issue tracker.

---

# 15. Онтология связей

Используется ограниченный словарь.

## `implements`

Документ реализует принцип или спецификацию.

## `depends_on`

Документ нельзя корректно использовать без зависимости.

## `used_by`

Документ используется другим документом или системой.

## `defines`

Документ является каноническим определением сущности.

## `constrains`

Документ накладывает ограничения.

## `extends`

Документ расширяет другой.

## `supersedes`

Документ заменяет другой.

## `conflicts_with`

Есть известное противоречие, требующее разрешения.

## `evidenced_by`

Решение подтверждается исследованием или данными.

## `validated_by`

Решение прошло конкретную проверку.

Не следует создавать десятки почти одинаковых типов связей.

---

# 16. Теги

Теги используются для быстрого фильтра.

Рекомендуемая система:

```text
#ayla
#status/approved
#status/draft
#type/adr
#type/specification
#domain/product
#domain/ai
#domain/safety
#priority/p0
```

Не использовать теги для:

- версий;
- владельцев;
- дат;
- отношений между документами.

Для этого существуют YAML-поля.

---

# 17. Obsidian как рабочая среда

## 17.1. Vault

Vault открывает корень knowledge-репозитория или папку `docs`.

Рекомендуемый вариант:

```text
Open folder as vault:
<repository-root>
```

Это позволяет видеть:

- `docs`;
- README;
- связанные конфигурации;
- templates.

## 17.2. Обязательные возможности

Core:

- File Explorer;
- Backlinks;
- Outgoing Links;
- Graph View;
- Canvas;
- Properties View;
- Templates.

Community plugins:

- Dataview;
- Templater;
- Obsidian Git;
- Excalidraw;
- optional: Linter.

## 17.3. Ограничение плагинов

Новый плагин добавляется только если:

- решает конкретную проблему;
- активно поддерживается;
- не создаёт proprietary lock-in;
- не меняет Markdown в несовместимый формат;
- не требует хранить чувствительные данные во внешнем сервисе.

---

# 18. Graph View

Graph View используется для анализа связей, а не как декоративная визуализация.

Рекомендуемые группы:

- Foundation;
- Product;
- AI;
- Architecture;
- Safety;
- Design;
- Business;
- Research.

Рекомендуемые фильтры:

```text
path:"docs"
-tag:#status/archived
```

Graph View не заменяет MOC и не является источником истины.

Если граф выглядит красиво, но документы не имеют явных отношений, архитектура знаний считается слабой.

---

# 19. Dataview

Примеры полезных запросов.

## Все P0-документы

```dataview
TABLE type, status, owner, updated
FROM "docs"
WHERE priority = "P0"
SORT status ASC, updated DESC
```

## Документы на ревью

```dataview
TABLE owner, version, updated
FROM "docs"
WHERE status = "review" OR status = "approved-with-amendments"
SORT priority ASC
```

## Mirrors, которые давно не синхронизировались

```dataview
TABLE source, synced
FROM "docs"
WHERE source_kind = "mirror"
SORT synced ASC
```

## Документы без владельца

```dataview
TABLE type, status
FROM "docs"
WHERE !owner
```

---

# 20. Шаблоны документов

Обязательные шаблоны:

- Foundation Document;
- Product Specification;
- PRD;
- AI Specification;
- ADR;
- Research;
- Handoff;
- Runbook;
- MOC.

Каждый шаблон должен:

- включать YAML;
- объяснять назначение;
- содержать раздел «Не входит»;
- содержать зависимости;
- содержать открытые вопросы;
- содержать критерии утверждения.

---

# 21. Review-процесс

## 21.1. Перед созданием нового документа

Автор проверяет:

1. Нет ли уже документа с той же ответственностью.
2. В какой раздел он относится.
3. Какой тип использовать.
4. Какие документы являются зависимостями.
5. Кто владелец.
6. Как будет проверяться результат.

## 21.2. Перед утверждением

Проверяется:

- непротиворечивость Конституции;
- совместимость с Product Thesis;
- актуальность зависимостей;
- отсутствие дублирования;
- связь с ADR;
- наличие edge cases;
- наличие последствий;
- соответствие scope.

## 21.3. Reviewers

В зависимости от типа:

| Документ | Обязательные роли |
|---|---|
| Foundation | Founder, Product Architecture |
| Product | Product Owner, UX |
| AI | AI Architecture, Safety |
| ADR | Tech Lead, затронутые владельцы |
| Privacy | Legal/Data Protection, Security |
| Safety | Safety Owner, Domain Expert |
| Business | Founder, Finance/Product |
| Runbook | Operational Owner |

---

# 22. Change Log

Канонический документ должен иметь раздел:

```markdown
# Change Log

## v1.1 — 2026-08-10

- добавлен ...
- изменён ...
- причина ...
```

Для мелких редакторских изменений достаточно Git history.

Для смысловых изменений Change Log обязателен.

---

# 23. Противоречия

Если два approved-документа противоречат друг другу:

1. Противоречие фиксируется через `conflicts_with`.
2. Создаётся issue или decision record.
3. До разрешения более новый документ не считается автоматически главным.
4. Определяется верхнеуровневый принцип.
5. Выпускается amendment или новая версия.
6. Старый документ получает `superseded` или корректируется.

Недопустимо молча исправлять один документ и оставлять другой устаревшим.

---

# 24. Устаревание

Каждый канонический документ имеет review cycle.

При наступлении срока владелец должен:

- подтвердить актуальность;
- обновить;
- deprecated;
- superseded;
- archived.

Документ без владельца и без актуального review не должен использоваться как основание для нового решения.

---

# 25. Handoff-документы

Handoff — это снимок состояния работы, а не каноническая продуктовая спецификация.

Handoff должен содержать:

- контекст;
- что выполнено;
- что не выполнено;
- ссылки на канонические документы;
- код и ветки;
- известные дефекты;
- следующие действия;
- владельца.

Handoff не должен становиться единственным местом, где зафиксировано продуктовое или архитектурное решение.

Если в handoff обнаружено новое решение, оно переносится в:

- PRD;
- specification;
- ADR;
- runbook.

---

# 26. Источники и evidence

Внешний источник не становится правилом Ayla автоматически.

Порядок:

```text
External source
↓
Research note
↓
Interpretation
↓
Decision
↓
Specification / ADR
```

Документ должен различать:

- факт;
- вывод;
- гипотезу;
- решение.

---

# 27. Работа с чувствительными данными

Knowledge repository не должен содержать:

- реальные медицинские данные пользователей;
- токены;
- пароли;
- API-ключи;
- персональные переписки;
- production dumps;
- персональные идентификаторы;
- коммерческие секреты без необходимого контроля доступа.

Примеры в документации должны использовать:

- вымышленные данные;
- обезличенные сценарии;
- synthetic identifiers.

---

# 28. Связь с кодом и задачами

PR, issue или implementation task должны ссылаться на:

- PRD;
- specification;
- ADR;
- acceptance criteria.

Документ может ссылаться на:

```text
Repository:
apps/orchestrator/

Issue:
#1234

Tests:
tests/intent/
```

Но документация не должна копировать большие фрагменты кода, которые быстро устаревают.

---

# 29. Связь с QA и аналитикой

Для существенной функции должна существовать трассируемость:

```text
Requirement ID
↓
Implementation
↓
Test
↓
Analytics Event
↓
Metric
```

Рекомендуемый формат requirement ID:

```text
UJ-DISC-001
INT-CONTEXT-003
REC-SAFETY-005
```

Это должно быть подробно определено в отдельной Traceability Specification после стабилизации базовых документов.

---

# 30. Knowledge Quality Metrics

Качество базы знаний измеряется не числом документов.

Полезные показатели:

- доля канонических документов с owner;
- доля документов с review cycle;
- число broken links;
- число approved conflicts;
- доля mirrors с актуальной синхронизацией;
- время поиска канонического решения;
- доля P0-документов, связанных с реализацией;
- число документов без входящих ссылок;
- время onboarding нового участника.

---

# 31. Anti-patterns

## 31.1. Documentation dump

Просто складывать файлы в папки без связей.

## 31.2. Final-final naming

Использовать названия вроде:

```text
spec-final-v2-new.md
```

## 31.3. Silent canon

Считать документ главным только потому, что так сказал один участник.

## 31.4. Duplicate truth

Хранить одинаковое решение в нескольких документах без ссылки на канон.

## 31.5. Obsidian-only knowledge

Использовать специфический формат, который перестаёт читаться без Obsidian.

## 31.6. Graph theater

Строить красивый граф без реальных семантических связей.

## 31.7. Endless document expansion

Пытаться сделать один документ исчерпывающим для всех ролей.

## 31.8. Unowned knowledge

Документ не имеет владельца и никогда не пересматривается.

## 31.9. Decisions in chat

Оставлять утверждённые решения только в переписке.

## 31.10. Handoff as architecture

Использовать временный handoff как постоянный ADR.

---

# 32. План внедрения

## Фаза 1. Фундамент

Срок: 1–2 дня.

- утвердить структуру;
- создать `00 Foundation`;
- добавить этот документ;
- создать Foundation MOC;
- зафиксировать словарь статусов и типов;
- создать шаблоны YAML.

## Фаза 2. Инвентаризация

Срок: 2–4 дня.

- собрать существующие документы;
- определить канонические источники;
- найти дубликаты;
- определить mirrors;
- присвоить owner, type, status;
- выделить устаревшие документы.

## Фаза 3. Миграция P0

Срок: 3–5 дней.

Перенести и связать:

- Constitution;
- MVP Product Thesis;
- User Journey Specification;
- Intent Model;
- Recommendation Engine;
- key ADR;
- Safety Contract;
- Dynamic User Model.

## Фаза 4. MOC и граф

Срок: 2–3 дня.

- создать MOC;
- добавить wikilinks;
- настроить Graph View;
- добавить Dataview dashboards;
- проверить orphan documents.

## Фаза 5. Governance

Срок: 2–3 дня.

- определить review flow;
- настроить pull request template;
- добавить documentation checklist;
- создать правила утверждения;
- создать CHANGELOG.

## Фаза 6. Handoff migration

Срок: 3–7 дней.

- классифицировать handoff-файлы;
- извлечь канонические решения;
- перенести операционные части в runbooks;
- сохранить исходные handoff в Operations или Sources;
- связать их с продуктовой и архитектурной документацией.

---

# 33. Минимальный список документов для запуска системы

## Foundation

- [[Ayla Constitution v2.2]]
- [[Ayla Knowledge Architecture Specification v1.2]]
- [[Glossary]]
- [[Foundation MOC]]

## Product

- [[Ayla MVP Product Thesis v1.0 FINAL]]
- [[product/user-journeys/ayla-user-journey-specification-v1.1|Ayla User Journey Specification v1.1]]
- [[Product MOC]]

## AI

- [[Ayla Intent Model Specification v1.0]]
- [[Ayla Recommendation Engine Specification v1.0]]
- [[Ayla AI Orchestrator Architecture v1.0]]
- [[AI System MOC]]

## Architecture

- [[architecture/ADR-0009-split-domain|ADR-0009 Split Domain Architecture]]
- [[ADR-0011 User Context Privacy]]
- [[ADR-0012 Dynamic User Model]]
- [[Architecture MOC]]

## Safety

- [[Ayla Safety and Boundary Specification v1.0]]
- [[safety/cross_domain_safety_contract|Cross-Domain Safety Contract]]
- [[Safety and Governance MOC]]

---

# 34. Definition of Done

Ayla Knowledge Architecture v1.2 считается внедрённой, когда:

- существует утверждённая структура;
- P0-документы размещены в правильных разделах;
- у P0-документов есть metadata;
- у каждого P0-документа есть owner;
- определён canonical source;
- mirrors помечены;
- MOC созданы;
- документы связаны wikilinks;
- Graph View показывает смысловые связи;
- Dataview показывает статусы и пробелы;
- определён review process;
- существует миграционный список handoff-файлов;
- команда знает, где искать каноническое решение.

---

# 35. Решения и открытые вопросы реализации

В v1.1 приняты решения:

1. Целевой канонический knowledge repository — отдельный `ayla-knowledge`.
2. Владелец Knowledge Architecture — Product Architecture.
3. Доменные документы остаются каноническими в owning repositories и
   синхронизируются как mirrors.
4. YAML и relationship lint обязателен.
5. Foundation и ADR изменяются только через pull request и review matrix.
6. AI-index включается только после активации schema, PII и export validation.

Открытыми остаются инфраструктурные вопросы:

1. Где будет создан репозиторий `ayla-knowledge` и кто выдаёт credentials sync
   automation.
2. Какой Git-клиент используется в Obsidian; выбор не меняет source-of-truth
   модель.
3. Какой retention period применяется к архивным документам.
4. Какие численные thresholds используются в Knowledge Health Dashboard.

---

# 36. Рекомендованное решение для текущего этапа

Для пилота рекомендуется:

```text
Canonical knowledge:
Git repository + Markdown

Human interface:
Obsidian

Version control:
GitHub

Visualization:
Obsidian Graph + Canvas + Excalidraw

Queries:
Dataview

Automation:
Schema + manifest + required validation

AI indexing:
отложить до стабилизации P0-документов
```

Neo4j, LlamaIndex или иной полноценный Knowledge Graph не внедряются на этом этапе.

Причина:

- онтология ещё меняется;
- документы ещё мигрируют;
- автоматизация грязной базы знаний лишь масштабирует хаос.

---

# 37. Заключение

Ayla Knowledge Architecture — не вспомогательная система хранения файлов.

Это механизм, который позволяет компании:

- сохранять решения;
- предотвращать архитектурный дрейф;
- объяснять поведение AI;
- масштабировать команду;
- безопасно развивать продукт;
- связывать философию, UX, AI, архитектуру, код и метрики.

Главный принцип:

> Документ ценен не потому, что он написан, а потому, что понятно, какой статус он имеет, что определяет, на чём основан, чем ограничен и где реализован.

---

# 38. Нормативные поправки v1.1

Этот раздел закрывает amendments, выявленные при ревью v1.0. Если правило
этого раздела противоречит более раннему разделу документа, применяется
правило v1.1.

## 38.1. Роль knowledge repository

Целевая модель использует отдельный Git-репозиторий `ayla-knowledge`.

Knowledge repository является каноническим источником для:

- корневого MOC [[Ayla]];
- этой Knowledge Architecture Specification;
- межрепозиторных карт и MOC;
- общего Glossary;
- единого domain registry;
- правил knowledge governance.

Knowledge repository является read-only aggregation layer для документов,
канонический источник которых находится в продуктовых репозиториях:

- `beautygo_backend` — booking, payments, catalog, identity и backend product
  contracts;
- `ai-bot-platform` — bot, conversations, memory integration и AI backbone;
- `ayla-ai-core` — модели intent, recommendation, orchestration и evaluation.

Mirror запрещено редактировать вручную. Любое смысловое изменение сначала
вносится в канонический репозиторий.

До создания отдельного `ayla-knowledge` текущий файл в `beautygo_backend`
является каноническим staging-контрактом. Этот временный статус не превращает
весь backend `docs/` в knowledge repository и должен быть снят отдельным
решением о миграции канона.

## 38.2. Manifest-driven Git sync

Синхронизация управляется файлом `docs/.knowledge/sources-manifest.yaml`.
Каждый источник обязан определять:

- `repository`;
- `branch`;
- `include`;
- `exclude`;
- `target`;
- `mode`;
- `owner`;
- `review_policy`.

Нормативный pipeline:

```text
merge в main канонического репозитория
→ sync job читает sources-manifest.yaml
→ получает разрешённые Markdown-файлы
→ добавляет mirror metadata
→ вычисляет source_content_hash
→ записывает результат в generated/mirror branch
→ запускает structural, semantic, link, secret и PII checks
→ создаёт pull request в knowledge repository
→ merge после обязательного review
```

Mirror metadata:

```yaml
source_kind: mirror
source_repository: https://github.com/example/repository
source_path: docs/path/document.md
source_ref: <immutable-commit-sha>
synced: 2026-07-18
source_content_hash: "sha256:<digest>"
```

`branch` в manifest является динамической ссылкой. Sync job обязан разрешить
её в commit SHA и записать этот SHA в `source_ref`.

`source_content_hash` определяется однозначно:

```text
Algorithm: SHA-256
Input: canonical content до добавления mirror metadata
Encoding: UTF-8
Line endings: LF
BOM: excluded
Trailing whitespace: preserved
```

Правила конфликтов:

- локальные изменения mirror считаются ошибкой;
- несовпадение `source_content_hash` с ожидаемым состоянием вызывает fail-fast;
- pipeline не выполняет silent overwrite;
- результат синхронизации содержит audit log: source commit, changed files,
  validation result и actor;
- удаление источника создаёт PR с deprecation action, но не удаляет историю
  автоматически.

Fail-fast collision включает:

- два source-файла с одинаковым target path;
- повторяющийся `node_id`;
- совпадение `node_id` или title у canonical и mirror;
- локально изменённый mirror;
- попадание одного source-файла под несколько include mappings.

Удаление source создаёт deprecation PR. Переименование создаёт rename PR с
сохранением `node_id` и backlinks.

На пилотном этапе допустим manual sync тем же manifest-driven инструментом.
Ручное копирование без manifest, hash и review не считается синхронизацией.

## 38.3. ADR lifecycle

ADR остаётся каноническим в репозитории, владеющем соответствующей
архитектурой. Knowledge repository хранит ADR node или его mirror.

Обязательные поля ADR:

```yaml
adr_id: ADR-0011
decision_status: accepted
revision: 1
amendments: []
superseded_by: null
```

Различаются три вида изменений:

1. Minor revision — уточнение без изменения решения; меняется `revision`.
2. Amendment — отдельный документ, например `ADR-0011-A1`, связанный через
   `amends`.
3. Replacement — новое решение с новым ADR ID; старый ADR получает
   `decision_status: superseded` и `superseded_by`.

Обычные документы ссылаются на knowledge node ADR через `adr`. Git URL,
repository, path и commit SHA хранятся в самом ADR node. Это сохраняет удобные
wikilinks без потери технической трассируемости.

`status` описывает зрелость ADR-файла, а `decision_status` — состояние решения.
Общий статус `proposed` не используется. Schema проверяет соответствие:

- `decision_status: proposed` → `status: draft | review`;
- `accepted` → `approved | approved-with-amendments | implemented`;
- `superseded` → `superseded | archived` и непустой `superseded_by`;
- `amended` требует непустой список `amendments`.

## 38.4. Онтология областей, доменов и ответственности

Поля не смешиваются:

| Поле | Смысл |
|---|---|
| `knowledge_area` | Где документ расположен в архитектуре знаний |
| `domain` | Какой бизнес- или системный bounded context он описывает |
| `system_owner` | Какая архитектурная система или bounded system владеет описываемой ответственностью |
| `source_repository` | В каком Git-репозитории находится канонический источник или исходный mirror |
| `concerns` | Какие сквозные аспекты затрагивает документ |

`node_id` является неизменяемым идентификатором узла. Title, filename и
wikilinks могут изменяться, `node_id` — нет. Для активных узлов `node_id`,
`knowledge_area` и `system_owner` обязательны. `system_owner` всегда является
списком и не содержит названий ролей, команд или репозиториев. `domain`
обязателен и непуст для product, AI, domain model, architecture, safety, design
и business documents;
для реального междоменного документа используется `cross-domain`.

Canonical `knowledge_area`:

```text
foundation
product
strategy
ai-system
domain-model
architecture
safety-governance
design
business
research
operations
sources
```

Canonical business domains:

```text
identity
consent
user-context
conversation
intent
recommendation
catalog
provider
booking
payment
nutrition
wellness
notification
analytics
marketing
loyalty
```

Canonical system owners:

```text
ayla-platform
ayla-ai-core
ayla-conversation
ayla-user-context
ayla-recommendation
ayla-booking
ayla-provider-platform
ayla-mobile
ayla-mini-app
ayla-knowledge
shared
external
to-be-confirmed
```

`to-be-confirmed` является временным migration-значением до 2026-08-15 и не
допускается для approved, implemented или delivered документов.

Canonical source repositories:

```text
beautygo_backend
ai-bot-platform
ayla-knowledge
ayla-ai-core
external
```

Canonical cross-cutting concerns:

```text
privacy
security
safety
observability
audit
compliance
```

Новый небольшой domain добавляется review-изменением в unified domain
registry. Новый bounded context, меняющий ownership или системные границы,
добавляется через ADR. Значения полей проверяются по
`docs/.knowledge/schema.yaml`. Роль или команда, отвечающая за документ,
указывается только в `owner`; репозиторий указывается в `source_repository`.

## 38.5. Data classification, PII и AI export

Каждый индексируемый или экспортируемый документ обязан иметь:

```yaml
classification: internal
data_sensitivity: none
security_sensitivity: low
ai_indexing: allowed
export_policy: full
data_categories:
  - none
```

Шкалы `data_sensitivity` и `security_sensitivity`:

```text
none
low
medium
high
critical
```

Обе шкалы описывают содержимое документа, а не только предметную область.
Например, документ может описывать health-domain и иметь
`data_sensitivity: none`, если в нём нет реальных пользовательских данных.
Значения `high` и `critical` требуют restricted review и не отменяют
абсолютный запрет на реальные персональные данные и secrets.

Допустимые значения `export_policy`:

```text
full
sanitized
metadata-only
prohibited
```

Допустимые значения определены в schema. По умолчанию применяется deny:
отсутствие `ai_indexing: allowed` означает запрет AI-индексации.

Необязательное поле `data_categories` объясняет причину чувствительности:
`synthetic`, `pii`, `health`, `financial`, `credentials`,
`raw-user-content`, `production-data`. Для sensitivity выше `none` категория
обязательна.

В knowledge repository и его exports запрещены:

- реальные health, pregnancy и chronic-condition entries;
- raw user text из сообщений, переписок и обращений;
- телефоны, email, карты и другие PII tokens;
- production dumps и audit logs с персональными событиями;
- encryption keys, peppers, tokens, passwords и secrets;
- реальные body/food photos и иные чувствительные пользовательские файлы.

Архитектурная документация может содержать названия полей, типы, constraints,
encryption mechanisms, access-control rules и audit requirements. Примеры
должны быть синтетическими и явно помеченными.

Контроль состоит из:

- secret scanner;
- PII scanner;
- path-based policies;
- allowlist документов для AI-index;
- ручного review для `restricted`;
- отдельного pre-export validation report.

Документ с `export_policy: prohibited` не попадает ни в vector index, ни в
LlamaIndex/Neo4j export. При `metadata-only` содержимое не экспортируется.
При `sanitized` экспортируется только результат отдельной sanitization step.

## 38.6. Planned Knowledge Nodes

Planned node — управляемое обязательство, а не пустая ссылка.

Обязательные поля:

```yaml
status: planned
owner: <role-or-person>
target_milestone: <milestone-or-review-window>
target_date: null
priority: P1
depends_on: []
blocking_reason: null
```

Lifecycle:

```text
planned → scheduled → in-progress → delivered
                     ↘ blocked
planned/scheduled → cancelled
```

`target_date` обязательна только для активных P0/P1 commitments после
согласования с roadmap и capacity. Для долгосрочных узлов используется
`target_milestone`. Просроченный узел не становится автоматически `blocked`:
блокировка требует заполненного `blocking_reason`.

| Node | Owner | Milestone | Priority | Status | Dependencies |
|---|---|---|---|---|---|
| MVP Product Thesis | Product Lead | Foundation baseline | P0 | scheduled | Constitution |
| Intent Model Specification | AI Engineer | AI foundation | P0 | planned | User Journey |
| Recommendation Engine Specification | AI Engineer | AI foundation | P0 | planned | Intent Model |
| AI Orchestrator Specification | AI Engineer | Orchestration milestone | P1 | planned | Intent, Recommendation |
| Dynamic User Model Specification | Backend Architect | Personalization milestone | P0 | planned | ADR-0012 |
| Prompt Architecture | AI Engineer | Orchestration milestone | P1 | planned | Intent Model |
| Agent Architecture | AI Engineer | Post-orchestration review | P2 | planned | Orchestrator |
| AI Evaluation Framework | AI Engineer | AI quality milestone | P1 | planned | AI specifications |
| Domain Model MOCs | Backend Architect | Domain registry milestone | P1 | planned | ADR-0009 |
| Business Model Specification | Product Lead | Business validation | P1 | planned | MVP Product Thesis |
| Glossary | Product Architecture | Foundation baseline | P0 | scheduled | Constitution |

Минимальный stub допустим только если известны назначение, owner, status,
target milestone и зависимости. Пустые файлы ради устранения broken links
запрещены.

## 38.7. Root MOC [[Ayla]]

`[[Ayla]]` — обязательная входная точка базы знаний. Она создаётся одновременно
с утверждением v1.1 и содержит:

- Start Here;
- Current Product State;
- Decision Hierarchy;
- верхнеуровневые Maps of Content;
- Operational Entry Points.

Минимальная структура:

```markdown
# Ayla

## Start Here
- [[Ayla Constitution v2.2]]
- [[Ayla Knowledge Architecture Specification v1.2]]
- [[Glossary]]

## Current Product State
- [[MVP Product Thesis]]
- [[product/user-journeys/ayla-user-journey-specification-v1.1|Ayla User Journey Specification v1.1]]
- [[Product Roadmap]]

## Decision Hierarchy
- [[Constitution MOC]]
- [[ADR MOC]]
- [[Approved Specifications]]

## Maps
- [[Product MOC]]
- [[AI System MOC]]
- [[Architecture MOC]]
- [[Domain Model MOC]]
- [[Safety and Governance MOC]]
- [[Business MOC]]

## Operations
- [[Runbooks MOC]]
- [[Knowledge Health Dashboard]]
```

## 38.8. Schema-driven validation

Machine-readable contract schema v1.3 находится в
`docs/.knowledge/schema.yaml`.

Structural validation проверяет:

- обязательные поля и enum values;
- уникальность и формат обязательного `node_id`;
- mirror metadata и immutable `source_ref`;
- наличие owner, review cycle и dependencies у P0/P1;
- replacement link у deprecated/superseded документов.

Semantic validation проверяет:

- существование wikilinks для approved nodes;
- отсутствие циклов в `depends_on` и `supersedes`;
- существование ADR node для `adr`;
- значения ontology из registry;
- допустимые lifecycle transitions;
- отсутствие двух узлов с одним `node_id`;
- согласованность `status` и ADR `decision_status`;
- conditional rules для domain, export policy и planned commitments;
- отсутствие duplicate YAML keys и target collisions.

Relationship rules:

| Relationship | Cycle allowed | Target rule |
|---|---:|---|
| `depends_on` | no | existing knowledge node |
| `supersedes` | no | existing older node |
| `implements` | yes | Constitution principle, ADR or approved specification |
| `adr` | yes | existing ADR node |
| `related` | yes | existing node |
| `conflicts_with` | yes | existing node plus issue/decision reference |

В machine-readable schema эти свойства записываются явно:

```yaml
relationships:
  depends_on:
    directed: true
    acyclic: true
  related:
    directed: false
    acyclic: false
  supersedes:
    directed: true
    acyclic: true
```

Automation levels:

- pre-commit: YAML, required fields, enum, local links, secret patterns;
- CI: graph cycles, cross-repository source checks, PII scan, duplicate canon,
  AI-export allowlist;
- scheduled health report: stale mirrors, orphan nodes, broken links, overdue
  reviews and unresolved conflicts.

## 38.9. Versioning и release bundles

Версия в frontmatter описывает смысловую версию документа. Git commit SHA
фиксирует точное состояние файла. Git tag или release bundle фиксирует
согласованный набор документов для milestone.

Пример release bundle:

```yaml
bundle: ayla-mvp-foundation-2026-07
documents:
  - note: Ayla Knowledge Architecture Specification
    version: "1.1"
    commit: "<sha>"
  - note: Ayla User Journey Specification
    version: "1.1"
    commit: "<sha>"
```

Constitution изменяется через amendment process, ADR — по правилам 38.3,
обычные спецификации — через новую semantic version и Change Log. Смысловая
правка без новой версии запрещена.

## 38.10. Deprecation workflow

Deprecated или superseded документы:

- не удаляются;
- становятся read-only;
- содержат `superseded_by` или объяснение отсутствия замены;
- исключаются из default MOC и default search;
- сохраняют backlinks и доступны в history view;
- перемещаются в archive только после retention period;
- не попадают в AI index, если явно не разрешены для historical analysis.

## 38.11. Access control и review matrix

| Document class | Create | Required review | Merge authority |
|---|---|---|---|
| Foundation / P0 | Product Architecture | Founder + relevant owner | designated maintainer |
| ADR | Architecture owner | affected domain owners | architecture maintainer |
| Safety / privacy | Domain author | Security/Safety owner | designated maintainer |
| Product specification | Product owner | Engineering + UX where affected | product maintainer |
| Mirror | Sync automation | source owner or KB maintainer | KB maintainer |
| Research / working notes | contributor | optional unless promoted | area maintainer |

Права реализуются через Git branch protection, CODEOWNERS и pull requests.
Редактирование Obsidian-файла не обходит Git review.

## 38.12. Search and discovery hierarchy

Основной путь поиска:

```text
Root MOC
→ domain/area MOC
→ full-text search
→ metadata/Dataview filters
→ backlinks
→ graph view
```

Graph View — вспомогательный инструмент исследования, а не основной интерфейс
навигации.

## 38.13. Knowledge Health Dashboard

Минимальный dashboard показывает:

- broken links;
- orphan canonical notes;
- stale mirrors;
- overdue reviews;
- unresolved approved conflicts;
- P0/P1 coverage;
- planned-node completion;
- documents without owner;
- documents excluded from AI index by validation.

Пороговые значения и периодичность отчёта задаются в CI/scheduled job, а не
зашиваются в текст спецификации.

## 38.14. Responsibility boundaries для AI-документов

Документы Intent Model, Recommendation Engine и AI Orchestrator обязаны иметь
разделы:

```text
Responsibility
Inputs
Outputs
Owns
Does not own
```

`depends_on` описывает зависимость, но не заменяет границу ответственности.
Cross-cutting concern определяется один раз в каноническом документе и
связывается из потребителей.

---

# Change Log

## v1.2 — 2026-07-18

- `owner`, `system_owner` и `source_repository` разведены семантически;
- `system_owner` формализован как список bounded systems;
- добавлены canonical registry систем и source repositories;
- `to-be-confirmed` ограничен сроком и допустимыми статусами;
- machine-readable contract обновлён до schema v1.3;
- миграция существующих узлов выполняется через явный mapping.

## v1.1 — 2026-07-18

- утверждена модель отдельного knowledge repository и read-only mirrors;
- определён manifest-driven sync с generated branch, validation и PR;
- формализован ADR lifecycle;
- разделены knowledge area, domain, system owner и concerns;
- введены data classification, PII policy и deny-by-default AI export;
- шкалы data/security sensitivity и export policy приведены к финальному
  пяти- и четырёхуровневому контракту;
- добавлены immutable `node_id`, conditional domain и обязательный
  `system_owner`;
- формализованы ADR status consistency, resolved commit SHA, source hash и
  collision semantics;
- добавлены governance planned nodes и Root MOC;
- созданы минимальные stub notes для Start Here, Current State и MOC;
- введены schema-driven validation и relationship rules;
- определены versioning, release bundles, deprecation и access control;
- зафиксированы search hierarchy, health metrics и AI responsibility boundaries.

## v1.0 — 2026-07-17

- создана первая полная версия Knowledge Architecture;
- определены структура репозитория и типы документов;
- зафиксированы metadata, статусы и связи;
- определены правила Obsidian, Git, MOC и Graph View;
- добавлены migration plan, governance и Definition of Done.

---

# Approval

**Status:** Review — Pending Infrastructure Activation

**Founder:** Андрей Тихонов

**Owner:** Product Architecture

**Approval date:** __________________

**Signature / decision reference:** Knowledge Architecture v1.2 metadata-contract review
