---
title: ADR-0011 User Context Privacy
aliases:
  - ADR-0011 User Personal Context Privacy
  - ADR-0011 UserPersonalContext Privacy
node_id: ayla.adr.0011
type: source-placeholder
status: planned
version: "0.1"
owner: AI Platform Architecture
priority: P0
knowledge_area:
  - architecture
  - safety-governance
domain:
  - user-context
system_owner:
  - ayla-user-context
concerns:
  - privacy
  - security
  - audit
created: 2026-07-18
updated: 2026-07-18
source_kind: external
classification: internal
data_sensitivity: none
security_sensitivity: medium
ai_indexing: metadata-only
export_policy: metadata-only
review_cycle: event-driven
target_milestone: Knowledge repository activation
target_date: null
depends_on:
  - "[[architecture/ADR-0009-split-domain]]"
blocking_reason: Manifest-driven mirror sync is not activated.
source_repository: ai-bot-platform
source_path: docs/adr/ADR-0011-user-personal-context-privacy.md
---

# ADR-0011 User Context Privacy

## Purpose

Представляет будущий mirror канонического ADR-0011 из `ai-bot-platform`.

## Owner

AI Platform Architecture.

## Dependencies

- [[architecture/ADR-0009-split-domain|ADR-0009 Split Domain Architecture]]

## Target milestone

Knowledge repository activation.

## Delivery condition

Заменить placeholder автоматически созданным read-only mirror с commit SHA и
content hash после активации sync pipeline.
