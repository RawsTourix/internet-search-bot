---
id: design.v0.8.index
version: v0.8
spec_status: draft
implementation_status: planned
last_reviewed: 2026-07-27
---

# v0.8 — Identity & Multi-user Workspace

Версия добавляет accounts, linked identities, conversations и точное
ownership/authorization durable resources. Она является security prerequisite
для multi-user isolated execution v0.9.

## Читать

1. [`identity-and-multi-user.md`](identity-and-multi-user.md) — исходная identity
   model, scopes и deployment concepts.
2. [`implementation-plan.md`](implementation-plan.md) — последовательные updates,
   negative authorization и security gate.

## Именованные updates

| Порядок | Update | Результат |
|---:|---|---|
| 1 | `v0.8.1-identity-model` | Account/Identity/AuthSession/Principal model |
| 2 | `v0.8.2-authentication` | Email/password и auth session lifecycle |
| 3 | `v0.8.3-linked-identities` | Telegram account linking |
| 4 | `v0.8.4-conversations-and-workspaces` | Единая Web/Telegram workspace/history |
| 5 | `v0.8.5-authorization-and-ownership` | Exact access control durable resources |
| 6 | `v0.8.6-quotas-settings-and-secrets` | Limits, settings и protected credentials |
| 7 | `v0.8.7-security-hardening` | Threat model, audit и release gate |

## Предыдущий контекст

- [`../v0.5/README.md`](../v0.5/README.md) — ownership-ready persistence;
- [`../v0.6/README.md`](../v0.6/README.md) — durable AgentRun/TaskRun и services;
- [`../v0.7/README.md`](../v0.7/README.md) — skill/MCP scopes и capabilities;
- [`../../release-gates.md`](../../release-gates.md) — security gate.

Точные auth protocols и UI утверждаются перед соответствующим update, но
разделение identity/domain сущностей и enforcement boundaries является частью
этой draft-спецификации.