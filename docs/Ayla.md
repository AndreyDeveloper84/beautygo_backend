---
title: Ayla
node_id: ayla.root
type: moc
status: approved
activation_status: pending-infrastructure
version: "1.0"
owner: Product Architecture
priority: P0
knowledge_area:
  - foundation
domain: []
system_owner:
  - ayla-knowledge
concerns:
  - knowledge-management
created: 2026-07-18
updated: 2026-07-18
source_kind: canonical
classification: internal
data_sensitivity: none
security_sensitivity: low
ai_indexing: allowed
export_policy: full
review_cycle: monthly
depends_on:
  - "[[Ayla Knowledge Architecture Specification v1.2]]"
---

# Ayla

Единая входная точка в продуктовые, архитектурные и операционные знания Ayla.

## Start Here

1. [[Ayla Constitution v2.2]]
2. [[Glossary|Ayla Glossary v1.2]]
3. [[Ayla MVP Product Thesis v1.0 FINAL]]
4. [[product/user-journeys/ayla-user-journey-specification-v1.1|Ayla User Journey Specification v1.1]]

Governance: [[Ayla Knowledge Architecture Specification v1.2]].

## Current Product State

- Current phase: MVP architecture.
- Current milestone: Intent and Recommendation design.
- Current critical gaps:
  - [[Ayla Intent Model Specification v1.0]]
  - [[Ayla Recommendation Engine Specification v1.0]]
- [[MVP_ROADMAP_2026-07|MVP Roadmap 2026-07]]

## Decision Hierarchy

1. [[Ayla Constitution v2.2]]
2. [[architecture/ADR-0009-split-domain|ADR-0009 Split Domain Architecture]]
3. Approved specifications
4. Implementation contracts
5. Plans, research и working notes

## Maps

- [[Foundation MOC]]
- [[Product MOC]]
- [[AI System MOC]]
- [[Architecture MOC]]
- [[Safety and Governance MOC]]

## Operational Entry Points

- [Runbooks](runbooks/)
- [[Knowledge Health Dashboard]]

## Current governance status

- Knowledge Architecture v1.2: review, pending infrastructure activation.
- Отдельный `ayla-knowledge` repository: planned.
- Manifest-driven sync: specified, not activated.
- AI indexing: denied by default until validation pipeline is active.
