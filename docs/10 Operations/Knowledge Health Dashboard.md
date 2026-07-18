---
title: Knowledge Health Dashboard
node_id: ayla.operations.knowledge-health
type: dashboard
status: planned
version: "0.1"
owner: Product Architecture
priority: P1
knowledge_area:
  - operations
domain: []
system_owner:
  - ayla-knowledge
concerns:
  - knowledge-management
  - governance
  - observability
created: 2026-07-18
updated: 2026-07-18
source_kind: canonical
classification: internal
data_sensitivity: none
security_sensitivity: low
ai_indexing: allowed
export_policy: full
review_cycle: monthly
target_milestone: Knowledge validation activation
target_date: null
depends_on:
  - "[[Ayla Knowledge Architecture Specification v1.2]]"
blocking_reason: Validation pipeline is specified but not activated.
---

# Knowledge Health Dashboard

## Purpose

Показывать минимальный набор сигналов здоровья базы знаний без превращения
документации в формальный KPI.

## Planned signals

- broken links;
- stale mirrors;
- P0 documents without owner;
- P0 documents without upstream or downstream links;
- unresolved conflicts;
- overdue planned nodes;
- orphan canonical documents.

## Owner

Product Architecture.

## Dependencies

- [[Ayla Knowledge Architecture Specification v1.2]]
- `docs/.knowledge/schema.yaml`
- активированный validation pipeline.

## Target milestone

Knowledge validation activation.
