# RSSRipple Documentation

Central map of all RSSRipple documentation. Start here if you are unsure where
a topic lives; coding agents should read [`AGENTS.md`](../AGENTS.md) first.

## Project overview

| Document | Contents |
|----------|----------|
| [README.md](../README.md) / [README_CN.md](../README_CN.md) | Project overview, quick start, usage, feedback |
| [ARCHITECTURE.md](../ARCHITECTURE.md) | Module layout and runtime data flow |
| [DESIGN.md](../DESIGN.md) | Frontend design tokens and visual guidelines |

## Design & specification (authoritative)

Detailed design lives in [`design/`](design/) and is the source of truth for all
implementation. Changes to these areas must update the corresponding sub-document.

| Document | Contents |
|----------|----------|
| [design/data-models.md](design/data-models.md) | All ORM models (fields, constraints, relations) |
| [design/per-season-works.md](design/per-season-works.md) | Per-season works model (implemented P2–P7) |
| [design/filter-dsl.md](design/filter-dsl.md) | Filter DSL types, evaluation semantics, examples |
| [design/api-endpoints.md](design/api-endpoints.md) | All REST endpoints and request/response shapes |
| [design/business-logic.md](design/business-logic.md) | Fetching, metadata matching, Agent runs, scheduling, download sync |
| [design/notifications.md](design/notifications.md) | Download-complete notifications, snapshot contract, webhooks |
| [design/file-organization.md](design/file-organization.md) | Built-in organize: Library/OrganizeRule/OrganizePlan, storage volumes |
| [design/frontend.md](design/frontend.md) | Frontend routes, pages, key interactions |
| [design/error-handling.md](design/error-handling.md) | Unified response shape, error codes, global handling |
| [design/conventions.md](design/conventions.md) | Time format, download dirs, env vars, posters, logging, idempotency |
| [design/db-migration.md](design/db-migration.md) | SQLite → Turso → PostgreSQL migration matrix and scripts |
| [design/branching.md](design/branching.md) | Branch naming, CI/CD and release flow |
| [design/constraints.md](design/constraints.md) | **Core constraints quick reference** (full detail split out of AGENTS.md) |
| [sitemap.md](sitemap.md) | Frontend routes and browser tab titles |

## Testing & QA

| Document | Contents |
|----------|----------|
| [testing/integration-inventory.md](testing/integration-inventory.md) | Integration suites, case counts, coverage gates, reorg history |
| [testing/metadata-corpus.md](testing/metadata-corpus.md) | Production metadata offline acceptance corpus and review flow |
| [testing/organize-integration.md](testing/organize-integration.md) | Organize integration tests and container-level half E2E |
| [testing/midscene-e2e.md](testing/midscene-e2e.md) | Midscene.js browser E2E suites |
| [testing/web-ui-functional-cases.md](testing/web-ui-functional-cases.md) | Manual Web UI functional case checklist |

## Research & plans

| Document | Contents |
|----------|----------|
| [plans/metadata-deepsearch-validation/README.md](plans/metadata-deepsearch-validation/README.md) | Metadata DeepSearch hard-sample validation and NO-GO decision |

## For contributors & coding agents

| Document | Contents |
|----------|----------|
| [AGENTS.md](../AGENTS.md) | Authoritative spec index and condensed core constraints |
| [design/constraints.md](design/constraints.md) | Full core-constraints reference |
| [CONTRIBUTION.md](../CONTRIBUTION.md) | Dev setup, tests, branch policy, CI/CD, agent reading order |
