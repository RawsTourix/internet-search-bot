---
id: design.v0.4.mcp-registry-foundation.sequence
version: v0.4
update: v0.4-mcp-registry-foundation
spec_status: accepted
implementation_status: planned
last_reviewed: 2026-08-02
---

# Последовательность реализации MCP registry foundation

## Цель

Реализовать config-backed MCP registry и trusted integration metadata поверх
уже выделенных `AgentRuntime`, `ToolDispatcher` и MCP runtime contracts, не
переписывая central agent loop и не добавляя distributed infrastructure.

Application/hosting profiles и transport admission определены в
[`../../../runtime-and-deployment-profiles.md`](../../../runtime-and-deployment-profiles.md).

## 1. Characterization baseline

До изменения registry/dispatch закрепить scenarios:

- legacy `mcp.config` loading;
- Streamable HTTP servers;
- stdio/executable servers как поддерживаемый MCP transport adapter;
- существующие builtin stdio/executable entries как migration baseline;
- optional/startup-required behavior;
- list servers/tools/schema;
- call success, error, timeout и reconnect;
- manager/MCP progress rendering;
- `WAITING_USER`, reset и runtime shutdown;
- no duplicate call after current recovery path;
- current single-process self-hosted Service Application behavior;
- rejection user/session stdio in service admission before spawn;
- operator-managed instance identity separated from user definition.

Tests проверяют observable behavior, а не private layout.

## 2. Tool execution semantics

Расширить generic contracts modularization:

```text
ToolExecutionSemantics
ToolRetryPolicy
ToolSideEffectClass
ToolOutcome
ToolTimeoutProfile
```

Сначала semantics применяются к существующим tools без изменения поведения.
Conservative default для неизвестного MCP tool не разрешает blind retry
потенциально mutating operation.

## 3. Presentation profile contract

Добавить trusted `ToolPresentationProfile` и registry presentation overlay:

```text
binding identity
start/success/failure localization keys
safe visible argument projection
coalescing hint
visibility/severity
```

Renderer сначала ищет approved profile фактического target binding и только
затем использует generic fallback. Server-supplied message не становится
localization key или trusted template.

## 4. Scope, ownership и server definitions

Ввести:

```text
MCPServerScope
MCPServerDefinition
MCPServerIdentity
MCPServerBinding
MCPDefinitionOwner
```

Scopes:

```text
builtin | instance | user | session
```

Scope, owner и trust назначаются trusted application boundary:

- `builtin` — project/system definition;
- `instance` — deployment operator definition;
- `user` — authenticated/local owner definition;
- `session` — bounded session/conversation definition.

User payload не может повысить scope до `builtin`/`instance`.

До v0.8 `user` остаётся owner-ready schema без ложного account-isolation claim.

## 5. Transport admission policy

Transport задаётся отдельно от scope. Runtime поддерживает Streamable HTTP и
stdio/executable, а composition root передаёт `MCPTransportAdmissionPolicy`.

Общие rules Service Application:

```text
new builtin → Streamable HTTP
managed service user/session stdio → rejected
self-hosted service user/session stdio → rejected
self-hosted operator-managed instance stdio → optional explicit policy
```

Существующие builtin stdio/executable definitions помечаются migration
compatibility, а не как новый допустимый шаблон.

Admission выполняется до process spawn/connect и не создаёт частичный binding.
Policy decision имеет structured reason и trace event без raw secrets/command
arguments.

Future Local Agent Application сможет использовать другую admission policy, но
не реализуется этим update.

## 6. Immutable registry snapshots

Ввести:

```text
MCPRegistry
MCPRegistrySnapshot
RegistryRevision
MCPToolBinding
```

Требования:

- deterministic precedence/conflict policy;
- revision changes on add/remove/enable/disable/generation/tool-list change;
- discovery сохраняет exact binding coordinates;
- execution повторно проверяет freshness;
- stale snapshot приводит к controlled rediscovery/replan, а не к вызову другого
  одноимённого tool;
- admission result принадлежит exact definition/policy revision;
- change policy revision invalidates incompatible bindings.

## 7. MCP runtime integration

`MCPServerManager` остаётся transport/lifecycle coordinator и публикует registry
изменения через port, но не владеет AgentCycle, application profile или
remote-resource ownership.

```text
MCPServerManager
→ Streamable HTTP и stdio/executable adapters
→ connection/generation/recovery

MCPTransportAdmissionPolicy
→ pre-connect/pre-spawn decision

MCPRegistry
→ definitions/snapshots/bindings

ToolDispatcher
→ policy/invocation/outcomes/progress
```

Compatibility methods старого `MCPClient` делегируют новым owners.

## 8. Retry и unknown outcome

Перенести retry decision из общего transport fallback в Dispatcher policy.

- `safe` — bounded retry допустим;
- `idempotent` — retry требует declared idempotency semantics/key;
- `never_automatic` — response loss возвращает `unknown`.

Для `unknown` trace сохраняет original call identity, binding, arguments hash,
attempt и transport failure. Runtime не создаёт новый side effect, маскируя его
как обычный retry.

## 9. Remote resource registry

Ввести нейтральные contracts:

```text
RemoteResourceHandle
RemoteResourceRegistry
RemoteResourceState
RemoteResourceLifecyclePolicy
```

Agent-side registry хранит opaque ID и ownership coordinates, но не внутреннее
состояние сервиса. Handle регистрируется только если поведение tool описано
trusted descriptor.

## 10. Lifecycle hooks и cleanup

Расширить общий `LifecycleHook` контекст owner identity и bounded cleanup budget.
Минимальные events v0.4:

```text
cycle_completed
cycle_failed
cycle_cancelled
cycle_interrupted
session_reset
runtime_shutdown
```

`WAITING_USER` использует отдельную policy: сохранить ресурс в пределах
разрешённого grace/owner lifecycle либо запросить cleanup.

Cleanup:

- идемпотентный request;
- короткий timeout;
- best effort;
- failure фиксируется как unresolved/lost;
- не меняет completed AgentResult на failed.

## 11. Configuration migration

Обновить service configuration schema и example:

- explicit registry/server ID;
- scope;
- definition owner/source;
- transport settings;
- enabled/startup-required;
- secret reference;
- approved metadata/profile reference;
- schema/integration compatibility metadata;
- admission policy/profile reference только в trusted operator configuration.

Не вводить заранее свободно придуманное обязательное поле вроде
`contract: web-search.v1`. Конкретные versioning fields определяются вместе с
validated `MCPServerDefinition` и существующими tool schemas.

На этом этапе используется `ConfigProvider`, созданный в modularization:

```text
agent.config snapshot
→ operator-owned builtin/instance definition diff
→ admission validation
→ registry revision update
→ controlled connect/disconnect/reconnect
```

Per-user service definitions не становятся секциями общего `agent.config`.
До появления durable user repository v0.8 они могут оставаться schema-ready или
ограниченным compatibility/local source, но не получают права запускать server
host executable.

Не помещать secrets или full trusted descriptors в LLM-visible listing.
Добавить configuration audit и migration tests.

## 12. Builtin definitions

Системные definitions хранятся versioned и тестируются вместе с агентом.

Новая builtin definition:

```text
scope=builtin
transport=Streamable HTTP
```

Подключение конкретного builtin MCP-сервиса является отдельным предметным
изменением и не входит автоматически в этот update.

Существующие builtin stdio/executable servers мигрируют по одному:

```text
characterization parity
→ отдельный Streamable HTTP service или удаление
→ переключение builtin definition
→ удаление legacy executable entry и его private env parameters
```

Builtin outage должна давать controlled degraded capability. Optional server не
блокирует запуск всего приложения.

## 13. Self-hosted instance stdio boundary

Если первая реализация поддерживает operator-managed instance stdio, нужны:

- definition только из trusted operator source;
- explicit opt-in deployment policy;
- exact executable/args/env validation;
- no user override or scope escalation;
- safe logging/redaction;
- process lifecycle/recovery tests;
- clear warning, что process работает с правами service host;
- отсутствие silent enablement в managed defaults.

Эта возможность может быть отложена. Runtime transport adapter при этом остаётся
поддерживаемым и используется legacy migration tests.

## 14. Compatibility cleanup

После migration callers:

- удалить duplicate registry state из compatibility facade;
- запретить direct MCP call в обход Dispatcher для production agent loop;
- удалить generic retry, который игнорирует tool semantics;
- удалить legacy builtin executable definitions после их migration/removal;
- удалить legacy private environment parameters;
- зафиксировать architecture/admission tests;
- обновить canonical owners и configuration documentation.

Поддержка stdio/executable transport adapter сохраняется. Service/user admission
не расширяется автоматически после cleanup.

## Допустимая параллельность

После characterization tests параллельно можно проектировать:

- presentation profile contract;
- scope/server/owner models;
- transport admission policy;
- tool execution semantics;
- remote-resource pure models.

Последовательно интегрируются:

```text
MCP runtime events
→ admission boundary
→ immutable registry
→ Dispatcher policy
→ remote-resource lifecycle hooks
→ config migration
```

## Required tests

- scope precedence и conflicts;
- owner/source validation;
- user cannot assign builtin/instance scope;
- revision/generation invalidation;
- stale binding rejection;
- legacy configuration parity;
- new builtin definition rejects stdio/executable;
- managed service rejects user/session stdio before spawn;
- self-hosted service rejects user/session stdio before spawn;
- operator instance stdio requires explicit policy when implemented;
- Streamable HTTP builtin connect/reconnect/degraded mode;
- generic и semantic presentation;
- safe retry budget;
- idempotent retry semantics;
- mutating `unknown` without duplicate call;
- handle owner isolation;
- cleanup success/failure/timeout;
- reset/shutdown cleanup;
- reconnect without implicit resource close;
- optional builtin unavailable/incompatible;
- secret/redaction and untrusted metadata tests.

## Acceptance gate

```text
existing self-hosted Service Application scenarios before/after migration
→ equivalent allowed tool availability and AgentResult
→ no new PostgreSQL/Redis dependency
```

```text
all production MCP calls
→ admission + ToolDispatcher policy
→ structured progress/outcome/trace
```

```text
new builtin MCP integration
→ Streamable HTTP
```

```text
service user/session executable definition
→ rejected before spawn
```

```text
operator-managed instance stdio, if supported
→ explicit self-hosted policy
→ exact operator ownership
→ no user escalation
```

```text
stateful trusted tool
→ opaque handle registered to owner
→ lifecycle cleanup requested independently from transport connection
```

Полный gate определён в
[`README.md`](README.md),
[`../../../runtime-and-deployment-profiles.md`](../../../runtime-and-deployment-profiles.md)
и
[`../../../contracts/builtin-mcp-service-contract.md`](../../../contracts/builtin-mcp-service-contract.md).
