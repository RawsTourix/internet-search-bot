---
id: design.v0.7.index
version: v0.7
spec_status: draft
implementation_status: planned
last_reviewed: 2026-08-01
---

# v0.7 — Skills и extension platform

Версия добавляет подключаемые декларативные skills с bounded on-demand loading,
scopes, capability enforcement и task-scoped integration.

## Читать

1. [`skills-library.md`](skills-library.md) — архитектурная концепция, trust
   boundaries и исходный MVP.
2. [`implementation-plan.md`](implementation-plan.md) — последовательные updates
   от package format до stabilization.

## Именованные updates

| Порядок | Update | Результат |
|---:|---|---|
| 1 | `v0.7.1-skill-package-format` | Versioned manifest/content contract |
| 2 | `v0.7.2-skill-registry` | Scopes, revisions и lifecycle packages |
| 3 | `v0.7.3-skill-discovery` | Bounded task-scoped selection |
| 4 | `v0.7.4-skill-runtime-integration` | Composition-based runtime extensions |
| 5 | `v0.7.5-capability-and-trust` | Effective permissions и trust classes |
| 6 | `v0.7.6-builtin-skills` | Audited system/domain skills |
| 7 | `v0.7.7-extension-platform-stabilization` | API compatibility и security readiness |

## Зависимости

- [`../v0.4/v0.4-mcp-registry-foundation/README.md`](../v0.4/v0.4-mcp-registry-foundation/README.md)
  — общая scope-модель `builtin|instance|user|session` и local registry contracts;
- [`../v0.6/README.md`](../v0.6/README.md) — workflow/task orchestration;
- [`../v0.6/distributed-capability-registry.md`](../v0.6/distributed-capability-registry.md)
  — durable/distributed revisions и ownership-ready registry;
- [`../v0.5/postgresql-and-rag.md`](../v0.5/postgresql-and-rag.md) — retrieval;
- [`../../dependency-rules.md`](../../dependency-rules.md) — composition и
  dependency direction.

User ownership/permissions уточняются в [`../v0.8/README.md`](../v0.8/README.md).
Execution profiles и sandbox enforcement продолжаются в
[`../v0.9/README.md`](../v0.9/README.md).
