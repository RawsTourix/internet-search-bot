---
id: design.v0.4.mcp-registry-foundation.sequence
version: v0.4
update: v0.4-mcp-registry-foundation
spec_status: accepted
implementation_status: planned
last_reviewed: 2026-08-01
---

# Последовательность реализации MCP registry foundation

## Цель

Реализовать config-backed MCP registry и trusted integration metadata поверх
уже выделенных `AgentRuntime`, `ToolDispatcher` и MCP runtime contracts, не
переписывая central agent loop и не добавляя distributed infrastructure.

## 1. Characterization baseline

До изменения registry/dispatch закрепить scenarios:

- legacy `mcp.config` loading;
- Streamable HTTP servers;
- stdio/executable servers как поддерживаемый MCP transport;
- существующие builtin stdio/executable entries как migration baseline;
- optional/startup-required behavior;
- list servers/tools/schema;
- call success, error, timeout и reconnect;
- manager/MCP progress rendering;
- `WAITING_USER`, reset и runtime shutdown;
- no duplicate call after current recovery path.

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

## 4. Scope и server definitions

Ввести:

```text
MCPServerScope
MCPServerDefinition
MCPServerIdentity
MCPServerBinding
```

Scopes:

```text
builtin | instance | user | session
```

Transport задаётся отдельно от scope. Runtime поддерживает Streamable HTTP и
stdio/executable, но validation новых definitions применяет правило:

```text
builtin → Streamable HTTP
```

Существующие builtin stdio/executable definitions помечаются как migration
compatibility, а не как новый допустимый шаблон. User definitions могут
использовать stdio/executable.

Legacy config migration должна быть явной и обратимо диагностируемой. До v0.8
`user` остаётся owner-ready schema без ложного account-isolation claim.

## 5. Immutable registry snapshots

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
  одноимённого tool.

## 6. MCP runtime integration

`MCPServerManager` остаётся transport/lifecycle coordinator и публикует registry
изменения через port, но не владеет AgentCycle или remote-resource ownership.

```text
MCPServerManager
→ Streamable HTTP и stdio/executable adapters
→ connection/generation/recovery

MCPRegistry
→ definitions/snapshots/bindings

ToolDispatcher
→ policy/invocation/outcomes/progress
```

Compatibility methods старого `MCPClient` делегируют новым owners.

## 7. Retry и unknown outcome

Перенести retry decision из общего transport fallback в Dispatcher policy.

- `safe` — bounded retry допустим;
- `idempotent` — retry требует declared idempotency semantics/key;
- `never_automatic` — response loss возвращает `unknown`.

Для `unknown` trace сохраняет original call identity, binding, arguments hash,
attempt и transport failure. Runtime не создаёт новый side effect, маскируя его
как обычный retry.

## 8. Remote resource registry

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

## 9. Lifecycle hooks и cleanup

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

## 10. Configuration migration

Обновить configuration schema и example:

- explicit registry/server ID;
- scope;
- transport settings;
- enabled/startup-required;
- secret reference;
- approved metadata/profile reference;
- schema/integration compatibility metadata.

Не вводить заранее свободно придуманное обязательное поле вроде
`contract: web-search.v1`. Конкретные versioning fields определяются вместе с
validated `MCPServerDefinition` и существующими tool schemas.

На этом этапе используется `ConfigProvider`, созданный в modularization:

```text
agent.config snapshot
→ MCP definition diff
→ registry revision update
→ controlled connect/disconnect/reconnect
```

Не помещать secrets или full trusted descriptors в LLM-visible listing.
Добавить configuration audit и migration tests.

## 11. Builtin definitions

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

## 12. Compatibility cleanup

После migration callers:

- удалить duplicate registry state из compatibility facade;
- запретить direct MCP call в обход Dispatcher для production agent loop;
- удалить generic retry, который игнорирует tool semantics;
- удалить legacy builtin executable definitions после их migration/removal;
- зафиксировать architecture tests;
- обновить canonical owners и configuration documentation.

Поддержка stdio/executable transport для user MCP при этом сохраняется.

## Допустимая параллельность

После characterization tests параллельно можно проектировать:

- presentation profile contract;
- scope/server models;
- tool execution semantics;
- remote-resource pure models.

Последовательно интегрируются:

```text
MCP runtime events
→ immutable registry
→ Dispatcher policy
→ remote-resource lifecycle hooks
→ config migration
```

## Required tests

- scope precedence и conflicts;
- revision/generation invalidation;
- stale binding rejection;
- legacy configuration parity;
- new builtin definition rejects stdio/executable;
- user stdio/executable definition remains supported;
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
existing local scenarios before/after migration
→ equivalent tool availability and AgentResult
→ no new PostgreSQL/Redis dependency
```

```text
all production MCP calls
→ ToolDispatcher policy
→ structured progress/outcome/trace
```

```text
new builtin MCP integration
→ Streamable HTTP

user MCP integration
→ Streamable HTTP or stdio/executable according to definition
```

```text
stateful trusted tool
→ opaque handle registered to owner
→ lifecycle cleanup requested independently from transport connection
```

Полный gate определён в
[`README.md`](README.md) и
[`../../../contracts/builtin-mcp-service-contract.md`](../../../contracts/builtin-mcp-service-contract.md).
