# Ayla documentation index

This directory contains product, architecture, API, research, design, and
operational documentation for the Ayla backend and the wider Ayla product.

## Product foundations

These documents define what Ayla is, whom it serves, and what product outcome
the system is expected to create:

- [`Ayla.md`](Ayla.md) — root map of content and the entry point into the Ayla
  knowledge base.
- [`00 Foundation/Ayla Knowledge Architecture Specification v1.2.md`](00%20Foundation/Ayla%20Knowledge%20Architecture%20Specification%20v1.2.md)
  — governance contract for canonical documents, mirrors, metadata, sync,
  validation, privacy, and knowledge lifecycle.
- [`Ayla__Product_Vision.pdf`](../Ayla__Product_Vision.pdf) — original product
  vision and long-term direction.
- [`Product_Requirements_Document_Ayla__AI_Life_Assistant_v2.0.pdf`](../Product_Requirements_Document_Ayla__AI_Life_Assistant_v2.0.pdf)
  — base product PRD.
- [`product/user-journeys/ayla-user-journey-specification-v1.1.md`](product/user-journeys/ayla-user-journey-specification-v1.1.md)
  — implementation-level user journey, decision rules, state transitions,
  safety gates, and success metrics. Status: Approved with Amendments.
- [`PRD_Ayla_Killer_Scenario_v1.0.md`](PRD_Ayla_Killer_Scenario_v1.0.md)
  — cross-domain MVP scenario and its acceptance criteria.
- [`../DESIGN.md`](../DESIGN.md) — brand voice and baseline design system.

The Journey Specification links to materialized foundation nodes:

- [`00 Foundation/Ayla Constitution v2.2.md`](00%20Foundation/Ayla%20Constitution%20v2.2.md);
- [`01 Product/Ayla MVP Product Thesis v1.0 FINAL.md`](01%20Product/Ayla%20MVP%20Product%20Thesis%20v1.0%20FINAL.md);
- [`00 Foundation/Glossary.md`](00%20Foundation/Glossary.md).

Source placeholders remain non-authoritative until their canonical source or
mirror is activated through the knowledge manifest.

## Product strategy and current direction

- [`PRODUCT_AUDIT_2026-04.md`](PRODUCT_AUDIT_2026-04.md) — product positioning,
  value proposition, segments, risks, and strategic trade-offs.
- [`research/00-SYNTHESIS.md`](research/00-SYNTHESIS.md) — consolidated product
  research findings. Currently untracked in this checkout.
- [`MVP_ROADMAP_2026-07.md`](MVP_ROADMAP_2026-07.md) — current delivery map
  across Ayla, bot, Mini App, and AI core.
- [`HYPOTHESIS_VALIDATION_PLAN_2026-04.md`](HYPOTHESIS_VALIDATION_PLAN_2026-04.md)
  — validation plan for load-bearing product assumptions.

## Architecture and domain ownership

- [`ARCHITECTURE_REVIEW.md`](ARCHITECTURE_REVIEW.md) — target/current-state
  review, including the product essence and major capability gaps.
- [`architecture/ADR-0009-split-domain.md`](architecture/ADR-0009-split-domain.md)
  — canonical ownership split between Ayla, the bot platform, and AI core.
- [`safety/cross_domain_safety_contract.md`](safety/cross_domain_safety_contract.md)
  — cross-domain safety rules.
- [`PERSONAL_CONTEXT_INTERNAL_API_CONTRACT.md`](PERSONAL_CONTEXT_INTERNAL_API_CONTRACT.md)
  — internal contract for personal context and memory integration.

## Other documentation areas

- `architecture/` — architecture decisions and cross-repository contracts.
- `design/` — detailed technical and domain designs.
- `product/` — normative product foundations, journeys, and specifications.
- `research/` — product, market, competition, and economics research.
- `runbooks/` — operational procedures and readiness checks.
- `safety/` — safety contracts and guardrails.
- `setup/` — local and infrastructure setup.
- `openapi.yaml` and API-specific Markdown files — API contracts.

## Placement rule

Until the separate `ayla-knowledge` repository is created, cross-product
knowledge governance is staged under `00 Foundation/`. Domain-owned canonical
documents remain in their owning repository and are represented in the future
knowledge repository as read-only mirrors. User journey charters and executable
journey specifications live under `product/user-journeys/`. Architecture
decisions, implementation designs, research, and operational procedures belong
in their respective subdirectories.

The machine-readable knowledge contract and source registry are in
`.knowledge/schema.yaml` and `.knowledge/sources-manifest.yaml`. The manifest is
not connected to CI yet; it must not be treated as an active sync pipeline.

The repository root above `djangoproject/` is not versioned, and the sibling
`djangoproject-*` directories are parallel checkouts of the same backend
repository. Product documents should therefore be committed once in the
canonical backend branch rather than copied into every checkout.
