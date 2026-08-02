---
id: design.v0.6.distributed-capability-registry
version: v0.6
update: v0.6.9-distributed-capability-registry
spec_status: draft
implementation_status: planned
last_reviewed: 2026-08-02
supersedes: design.v0.6.distributed-runtime#127.7
---

# Distributed capability registry

## Статус относительно старого overview

Документ явно заменяет раздел `127.7. MCP registry scopes на стыке v0.5/v0.6`
в [`distributed-runtime.md`](distributed-runtime.md). Scope-модель больше не
считается новой задачей v0.6: config-backed foundation Service Application принят
в
[`v0.4-mcp-registry-foundation`](../v0.4/v0.4-mcp-registry-foundation/README.md).

Application profiles, hosting modes и transport admission определены в
[`../../runtime-and-deployment-profiles.md`](../../runtime-and-deployment-profiles.md).

v0.6 развивает существующий contract до durable multi-process runtime. Он не
создаёт отдельную registry architecture для Future Local Agent Application.

## Исходный contract v0.4

Сохраняются без переопределения:

```text
scopes: builtin | instance | user | session
trusted server/tool descriptors
immutable config-backed registry snapshot/revision
deterministic precedence and binding coordinates
side-effect-aware retry and normalized outcomes
opaque remote resource handles
lifecycle ownership and best-effort cleanup
transport support separated from application/deployment admission
```

Общий contract встроенных сервисов:
[`../../contracts/builtin-mcp-service-contract.md`](../../contracts/builtin-mcp-service-contract.md).

Для Service Application сохраняются принятые admission invariants:

- новые builtin definitions используют Streamable HTTP;
- managed service не запускает stdio/executable definitions;
- self-hosted operator может разрешить trusted `instance` stdio только явной
  deployment policy;
- ordinary user/session executable definitions не запускаются в trusted control
  plane;
- пользователь не назначает своей definition scope `builtin` или `instance`.

## Цель v0.6.9

```text
single-process config-backed Service registry
→ PostgreSQL-backed canonical registry
→ worker-visible immutable revisions
→ distributed freshness and reconciliation
→ ownership-ready durable lifecycle metadata
```

Физическое выделение MCP runtime в отдельный service остаётся optional. Durable
registry нужен для согласованности нескольких workers даже при сохранении одного
модульного приложения.

## Durable registry state

PostgreSQL становится source of truth Service Application для:

- server definitions и scopes;
- definition source/owner coordinates;
- enabled/disabled state;
- safe metadata и secret references;
- tool bindings и compatibility metadata;
- committed registry revisions;
- transport-admission-relevant definition metadata;
- ownership-ready `user`/`session` coordinates;
- unresolved lifecycle/cleanup intent, если remote resource переживает process
  boundary.

Operator-owned builtin/instance definitions и per-user definitions имеют разных
application owners, но публикуются в единый authorized effective snapshot.
Per-user configuration не превращается в секции общего `agent.config`.

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
transport admission policy or effective permission change
```

Agent worker получает immutable snapshot и сохраняет revision в discovery/task
context. Перед execution он повторно проверяет freshness exact binding и
действующий admission/authorization decision.

Stale worker:

- не вызывает disabled/rebound tool;
- не подменяет binding одноимённым tool другого scope;
- не запускает transport, запрещённый новой policy revision;
- не повышает user definition до operator/builtin trust;
- выполняет controlled rediscovery/replan либо завершает operation с structured
  stale-binding outcome.

## Precedence и conflict policy

Distributed implementation обязана сохранить deterministic precedence v0.4.
Порядок получения events, restart worker-а или локальный cache не меняют
выбранный binding.

Conflict разрешается registry policy, namespace или явным administrator/user
choice, но не случайным порядком rows или network responses.

Scope precedence не заменяет authorization и transport admission. Binding может
быть видимым в metadata snapshot, но недоступным конкретному principal/task по
policy.

## Active run/task snapshots

`TaskContextManifest`/run state фиксируют:

```text
registry revision
selected server/tool binding coordinates
trusted execution semantics version
required capability/authorization decision
transport admission/policy revision when relevant
```

Hot update не меняет committed task snapshot молча. Policy может:

- разрешить завершение на прежнем совместимом binding;
- потребовать controlled rediscovery;
- отменить unsafe/disabled operation;
- отклонить transport, который больше не admitted;
- создать новую task/workflow revision.

Новая configuration/registry revision не превращает active task в другой
application profile и не расширяет его capability ceiling.

## Remote resource lifecycle

Если stateful MCP resource переживает agent process:

- ownership coordinates и cleanup intent становятся durable;
- agent worker restart не теряет handle accounting;
- cleanup claim использует lease/idempotency;
- повтор cleanup безопасен;
- unavailable service оставляет `unresolved`/`lost`, а не ложный `closed`;
- сервис остаётся окончательным владельцем expiration/orphan cleanup.

MCP transport connection и remote-resource lifecycle остаются независимыми.
Cleanup worker повторно проверяет exact binding, owner и effective policy и не
использует arbitrary operation из remote tool output.

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

Definition source и requested scope не доверяются пользовательскому payload.
Application service назначает owner/scope на основании authenticated principal и
operator boundary.

## Recovery и reconciliation

Startup/recovery должны:

- загрузить последнюю committed revision;
- invalidировать stale local caches;
- восстановить enabled bindings;
- восстановить effective admission policy revision;
- сопоставить live server generations с durable definitions;
- пометить потерянные/несовместимые/rejected bindings;
- восстановить unresolved cleanup intents;
- не публиковать partially committed revision;
- не запускать executable transport до повторной проверки source/scope/policy.

## Compatibility с single-process Service Application

Self-hosted single-process Service Application продолжает использовать тот же
registry contract через config/filesystem/in-process adapters:

```text
same definitions and policy inputs
→ immutable registry snapshot
→ ToolDispatcher binding
```

Этот compatibility path не называется Local Agent Application. Он остаётся
серверным application profile, даже когда все компоненты запущены на одной
машине разработчика.

Future Local Agent сможет позднее переиспользовать registry models с отдельным
persistence и admission policy, но такая реализация не входит в v0.6.9.

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
transport admission changed
→ stale worker cannot connect/spawn using previous permission
```

```text
worker restart
→ definitions, revision, effective policy and unresolved lifecycle metadata recovered
```

```text
hot change during active task
→ committed snapshot preserved or controlled rediscovery
→ no silent binding substitution or capability escalation
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
single-process self-hosted Service Application
→ same registry contract suite through config/in-process adapter
```

```text
managed service + user/session stdio definition
→ rejected before process spawn
```

```text
self-hosted operator-approved instance stdio
→ admitted only under explicit deployment policy
→ remains operator-owned instance binding
```

## Non-goals

- новая scope-модель, несовместимая с v0.4;
- полноценный account authorization до v0.8;
- automatic trust для user/session metadata;
- обязательное выделение MCP runtime в отдельный сервис;
- хранение raw secrets в registry snapshots;
- привязка remote resource к MCP connection lifetime;
- превращение Redis в единственный source of truth;
- реализация Future Local Agent Application или его local registry backend;
- запуск user-provided executable MCP внутри Service Application;
- изменение service application profile через обычную registry/config revision.
