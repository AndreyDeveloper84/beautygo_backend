---
title: Booking Lifecycle Specification
node_id: ayla.domain.booking.lifecycle
type: specification
status: planned
version: "0.1"
owner: Booking Domain
priority: P0
knowledge_area:
  - domain-model
domain:
  - booking
system_owner:
  - ayla-booking
concerns:
  - audit
created: 2026-07-18
updated: 2026-07-18
source_kind: canonical
classification: internal
data_sensitivity: none
data_categories:
  - none
security_sensitivity: low
ai_indexing: allowed
export_policy: full
review_cycle: monthly
target_milestone: Booking lifecycle decision
target_date: null
depends_on:
  - "[[Event Taxonomy]]"
blocking_reason: Canonical booking state machine has not been approved.
---

# Booking Lifecycle Specification

## Purpose

Определить канонический Booking aggregate, состояния, переходы, команды,
события, идемпотентность и правила владения.

## Owner

Booking Domain.

## Target milestone

Booking lifecycle decision.
