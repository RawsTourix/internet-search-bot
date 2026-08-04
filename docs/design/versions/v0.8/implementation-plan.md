---
id: design.v0.8.implementation-plan
version: v0.8
document_role: implementation-plan
spec_status: draft
implementation_status: planned
last_reviewed: 2026-08-02
---

# Пошаговый план v0.8

## Общая цель

Добавить accounts, linked identities, conversations и точное
ownership/authorization всех durable resources Service Application, сохранив
self-hosted и managed hosting modes.

Application profiles, configuration ownership и отличие локально запущенного
self-hosted service от Future Local Agent Application определены в
[`../../runtime-and-deployment-profiles.md`](../../runtime-and-deployment-profiles.md).

Future Local Agent Application не входит в v0.8. Версия сохраняет совместимые
principal/ownership contracts, но не проектирует local executable UX, host
permissions или отдельную local root configuration.

## Реестр updates

| Порядок | Update | Главный результат |
|---:|---|---|
| 1 | `v0.8.1-identity-model` | Account/Identity/AuthSession/Principal boundaries |
| 2 | `v0.8.2-authentication` | Email/password и session lifecycle |
| 3 | `v0.8.3-linked-identities` | Безопасная привязка Telegram к account |
| 4 | `v0.8.4-conversations-and-workspaces` | Единые чаты и durable workspace |
| 5 | `v0.8.5-authorization-and-ownership` | Exact access control всех resources |
| 6 | `v0.8.6-quotas-settings-and-secrets` | Limits, configuration и secret references |
| 7 | `v0.8.7-security-hardening` | Threat model, audit и multi-user release gate |

Новые updates из-за deployment-profile clarification не добавляются. Изменение
уточняет boundaries уже запланированных работ.

## v0.8.1-identity-model

Разделяются сущности:

```text
Account
Identity
AuthSession
Principal
Conversation
Workspace
AgentRun
TaskRun
AgentCycle
```

Требования:

- stable IDs и timestamps;
- identity provider/type + external subject;
- один account может иметь несколько identities;
- principal представляет действующего субъекта, включая service/system principal;
- conversation не равна auth session или runtime process session;
- ownership metadata мигрируется для всех existing resources;
- single-user self-hosted Service Application имеет explicit system/local
  principal, а не скрытый `default` без модели;
- contracts допускают local/system principal будущего Local Agent, не фиксируя
  его account/login UX в v0.8;
- application profile, hosting mode и principal type не смешиваются в одну
  сущность или строковый `mode`.

## v0.8.2-authentication

### Scope

- email/password registration/login MVP;
- password hashing и algorithm migration;
- email normalization/uniqueness;
- optional verification policy;
- auth session/refresh/revocation;
- password reset/recovery;
- rate limits и brute-force protection;
- secure cookie/token strategy в зависимости от selected interface;
- logout all sessions и credential rotation;
- audit events без secret/token values;
- bootstrap/trusted-local flow для self-hosted Service Application, если он будет
  утверждён отдельной policy, без удаления principal/ownership checks.

Точный protocol и UI утверждаются перед реализацией, но domain/session contracts
не зависят от FastAPI route shape, hosting mode или конкретного client surface.

## v0.8.3-linked-identities

Telegram linking flow:

```text
authenticated account
→ short-lived one-time link intent/token
→ Telegram identity proves possession
→ conflict/ownership check
→ atomic link commit
```

Необходимы:

- token expiry и single use;
- replay/conflict handling;
- identity already linked to another account;
- unlink/relink policy;
- protection from account takeover;
- audit trail;
- migration existing Telegram user/chat bindings;
- no trust based only on user-supplied Telegram ID.

Linked identities относятся к Service Application workspace. Возможная
синхронизация с Future Local Agent не входит в этот update.

## v0.8.4-conversations-and-workspaces

### Scope

- conversation/chat CRUD и selection;
- Web/Telegram bindings к одной conversation;
- shared message history и client-specific presentation bindings;
- workspace membership/owner;
- active/pinned/archive state;
- conversation settings и selected model/tool/runtime preferences;
- exact artifact/memory/run relations;
- cross-client resume;
- deletion/retention semantics;
- group chat policy и future collaboration readiness.

Transport message remains distinct from logical `InputBatch`/conversation turn.

Web, Telegram, network CLI и network IDE clients используют один Service
Application API. Запуск клиента на машине пользователя не превращает его в
Future Local Agent Application и не выдаёт host execution capabilities.

## v0.8.5-authorization-and-ownership

### RequestContext

Каждая application command получает verified context:

```text
principal_id
account/tenant_id
identity/auth_session_id when applicable
conversation/workspace scope
request_id
permissions/roles
application/deployment policy revision when relevant
```

### Enforcement

- repositories/services добавляют exact owner/scope filters;
- LLM/tool arguments не определяют authorization;
- memory/artifact/content/plan/run/task/skill/MCP/settings access проверяется в
  application boundary;
- service-to-service calls используют отдельную identity;
- sharing/collaboration grants explicit и auditable;
- user scope registry становится полноценно enforced;
- обычный пользователь не может назначить MCP/skill definition scope `builtin`
  или `instance`;
- MCP transport admission остаётся policy Service Application: user/session
  executable definitions не запускаются в trusted control plane;
- negative authorization tests обязательны.

### Tenant model

Первый release может использовать account как tenant. Отдельная organization
entity добавляется только при реальной необходимости, но contracts не должны
смешивать principal и tenant.

Self-hosted single-user deployment может иметь один explicit local/system
principal, оставаясь Service Application с теми же ownership predicates.

## v0.8.6-quotas-settings-and-secrets

### Quotas

- active/concurrent runs;
- task/workflow limits;
- LLM token/cost budgets;
- tool/network operations;
- storage bytes/artifact count;
- uploads;
- background jobs;
- future sandbox CPU/RAM/time/minutes.

Quota проверяется до expensive side effect и имеет atomic reservation/usage
accounting там, где race существенен.

### Settings

- account/profile;
- conversation/workspace;
- models/runtime preferences;
- enabled skills/MCP definitions and scopes;
- localization/client preferences;
- retention/privacy.

Per-user settings не становятся секциями общего operator-owned `agent.config`.
Они хранятся через owner-aware repositories и изменяются через application API.
`ConfigProvider` Service Application продолжает владеть deployment/operator
configuration, а не пользовательскими данными.

### Secrets

Хранятся как encrypted/managed secret references. Raw secret не попадает в LLM
context, trace, progress, artifact metadata или sandbox manifest. Rotation и
revocation имеют audit.

User credential reference не раскрывается в registry listing и не даёт внешнему
MCP/tool больше permissions, чем выдано effective policy.

## v0.8.7-security-hardening

### Обязательные работы

- threat model authentication/authorization/linking;
- session fixation/replay/revocation tests;
- CSRF/CORS/cookie/token review для выбранного Web protocol;
- credential stuffing/rate limiting;
- cross-user ID enumeration tests;
- ownership backfill validation;
- data export/delete flows;
- audit log integrity/retention;
- abuse and quota bypass tests;
- secrets exposure review;
- backup/restore с owner relations;
- managed/self-hosted policy-default review;
- transport-admission bypass tests для user/session MCP definitions;
- security review sandbox prerequisites v0.9.

### Gate v0.9

- every durable resource имеет owner/scope;
- TaskContextManifest и ExecutionRequest содержат verified principal/scope IDs,
  но не raw credentials;
- sandbox input materializer проверяет access до execution;
- per-user quotas готовы резервировать execution resources;
- runner/sandbox получает short-lived least-privilege access only;
- отсутствие sandbox не приводит к host-process fallback Service Application;
- self-hosted operator privileges не смешиваются с ordinary user permissions.

## Non-goals v0.8

- полноценная organization billing platform;
- public marketplace skills;
- permanent per-user VM/container;
- distributed runner fleet;
- доказательство абсолютной безопасности;
- автоматическая передача user credentials внешним tools/skills;
- реализация Future Local Agent Application;
- local executable packaging, host terminal permission UX или local root config;
- синхронизация local-agent и service conversations;
- превращение `agent.config` в хранилище per-user settings.
