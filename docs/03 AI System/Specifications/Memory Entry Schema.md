---
title: Memory Entry Schema
node_id: ayla.ai.user-context.memory-entry-schema
type: source-placeholder
status: planned
version: "0.1"
owner: AI Platform Architecture
priority: P0
knowledge_area:
  - ai-system
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
data_categories:
  - none
security_sensitivity: medium
ai_indexing: metadata-only
export_policy: metadata-only
review_cycle: event-driven
target_milestone: Knowledge repository activation
target_date: null
depends_on:
  - "[[ADR-0011 User Context Privacy]]"
blocking_reason: Manifest-driven mirror sync is not activated.
source_repository: ai-bot-platform
source_path: docs/specs/memory-entry-schema.md
---

# Memory Entry Schema

## Purpose

Представляет будущий mirror табличной спецификации Memory Entry из
`ai-bot-platform`.

## Source status

Versioned companion specification to proposed ADR-0011; governance maturity
requires an explicit decision.
