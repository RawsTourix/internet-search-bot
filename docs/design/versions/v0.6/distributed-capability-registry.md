---
id: design.v0.6.distributed-capability-registry
version: v0.6
update: v0.6.9-distributed-capability-registry
spec_status: draft
implementation_status: planned
last_reviewed: 2026-08-01
supersedes: design.v0.6.distributed-runtime#127.7
---

# Distributed capability registry

## Статус относительно старого overview

Документ явно заменяет раздел `127.7. MCP registry scopes на стыке v0.5/v0.6`
в [`distributed-runtime.md`](distributed-runtime.md). Scope-модель больше не
считается новой задачей v0.6: локальный config-backed foundation принят в
[`v0.4-mcp-registry-foundation`](../v0.4/v0.4-mcp-registry-foundation/README.md).

v0.6 развивает существующий contract до durable multi-process runtime.

## Исходный contract v0.4

Сохраняются без переопределения:

```text
scopes: builtin | instance | user | session
trusted server/tool descriptors
immutable local registry snapshot/revision
deterministic precedence and binding coordinates
side-effect-aware retry and normalized outcomes
opaque remote resource handles
lifecycle ownership and best-effort cleanup
```

Общий contract встроенных сервисов:
[`../../contracts/builtin-mcp-service-contract.md`](../../contracts/builtin-mcp-service-contract.md).

## Цель v0.6.9

```text
local/config-backed registry
→ PostgreSQL-backed canonical registry
→ worker-visible immutable revisions
→ distributed freshness and reconciliation
→ ownership-ready durable lifecycle metadata
```

## Durable registry state

PostgreSQL становится source of truth для:

- server definitions и scopes;
- enabled/disabled state;
- safe metadata и secret references;
- tool bindings и compatibility metadata;
- committed registry revisions;
- ownership-ready `user`/`session` coordinates;
- unresolved lifecycle/cleanup intent, если remote resource переживает process
  boundary.

Raw secrets не входят в обычные snapshots, LLM context, progress или trace.
Redis может ускорять invalidation и event delivery, но не заменяет canonical
registry state.

## Revision и worker visibility

Каждая committed change создаёт новую revision:

```text
add/remove server
enable/disable
binding/schema compatibility change
server generation or discovered tool-list change
scope/owner visibility change
trusted descriptor change
```

Agent worker получает immutable snapshot и сохраняет revision в discovery/task
context. Перед execution он повторно проверяет freshness exact binding.

Stale worker:

- не вызывает disabled/rebound tool;
- не подменяет binding одноимённым tool другого scope;
- выполняет controlled rediscovery/replan либо завершает operation с structured
  stale-binding outcome.

## Precedence и conflict policy

Distributed implementation обязана сохранить deterministic precedence v0.4.
Порядок получения events, restart worker-а или локальный cache не меняют
выбранный binding.

Conflict разрешается registry policy, namespace или явным administrator/user
choice, но не случайным порядком rows или network responses.

## Active run/task snapshots

`TaskContextManifest`/run state фиксируют:

```text
registry revision
selected server/tool binding coordinates
trusted execution semantics version
required capability/authorization decision
```

Hot update не меняет committed task snapshot молча. Policy может:

- разрешить завершение на прежнем совместимом binding;
- потребовать controlled rediscovery;
- отменить unsafe/disabled operation;
- создать новую task/workflow revision.

## Remote resource lifecycle

Если stateful MCP resource переживает agent process:

- ownership coordinates и cleanup intent становятся durable;
- agent worker restart не теряет handle accounting;
- cleanup claim использует lease/idempotency;
- повтор cleanup безопасен;
- unavailable service оставляет `unresolved`/`lost`, а не ложный `closed`;
- сервис остаётся окончательным владельцем expiration/orphan cleanup.

MCP transport connection и remote-resource lifecycle остаются независимыми.

## Scope и authorization readiness

```text
builtin
  system-managed definition

instance
  deployment administrator-managed definition

user
  owner-ready definition; full account enforcement после v0.8

session
  bounded conversation/run visibility
```

v0.6 сохраняет owner/principal fields, но не заявляет полноценную multi-user
изоляцию до Identity/Authorization layer v0.8.

## Recovery и reconciliation

Startup/recovery должны:

- загрузить последнюю committed revision;
- invalidировать stale local caches;
- восстановить enabled bindings;
- сопоставить live server generations с durable definitions;
- пометить потерянные/несовместимые bindings;
- восстановить unresolved cleanup intents;
- не публиковать partially committed revision.

## Acceptance criteria

```text
two agent workers
→ same committed revision and deterministic bindings
```

```text
server disabled/rebound
→ stale worker cannot execute old binding
```

```text
worker restart
→ definitions, revision and unresolved lifecycle metadata recovered
```

```text
hot change during active task
→ committed snapshot preserved or controlled rediscovery
→ no silent binding substitution
```

```text
remote handle survives process restart
→ ownership/cleanup intent recovered
→ cleanup remains idempotent
```

```text
Redis unavailable
→ canonical registry remains recoverable from PostgreSQL
```

```text
local single-process mode
→ same registry contract suite through local adapter
```

## Non-goals

- новая scope-модель, несовместимая с v0.4;
- полноценный account authorization до v0.8;
- automatic trust для user/session metadata;
- обязательное выделение MCP runtime в отдельный сервис;
- хранение raw secrets в registry snapshots;
- привязка remote resource к MCP connection lifetime;
- превращение Redis в единственный source of truth.
