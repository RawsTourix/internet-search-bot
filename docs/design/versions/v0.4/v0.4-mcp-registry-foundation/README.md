---
id: design.v0.4.mcp-registry-foundation
version: v0.4
update: v0.4-mcp-registry-foundation
spec_status: accepted
implementation_status: planned
last_reviewed: 2026-08-01
---

# v0.4-mcp-registry-foundation

## Назначение

Обновление выполняется после `v0.4-runtime-modularization` и завершает локальный
фундамент MCP capability registry. Оно не добавляет конкретный поисковый или
другой предметный сервис, а готовит Agent Runtime к подключению встроенных,
администраторских, пользовательских и session-scoped MCP-серверов через единый
Dispatcher.

```text
AgentRuntime
→ ToolDispatcher
→ MCP registry snapshot
→ MCP runtime/server binding
```

Общий контракт встроенных MCP-сервисов:
[`../../../contracts/builtin-mcp-service-contract.md`](../../../contracts/builtin-mcp-service-contract.md).

## Главный результат

- MCP servers получают scopes `builtin`, `instance`, `user`, `session`.
- Registry предоставляет immutable snapshot/revision и deterministic precedence.
- `ToolDispatcher` использует trusted execution metadata, а не только имя remote
  tool.
- Builtin и одобренные instance tools могут иметь semantic presentation
  profiles.
- Retry зависит от side-effect semantics инструмента.
- Неопределённый исход mutating operation представлен `unknown` и не вызывает
  blind retry.
- Stateful MCP tools могут возвращать opaque remote handles.
- Agent Runtime регистрирует lifecycle ownership handles и выполняет bounded
  best-effort cleanup через общие lifecycle hooks.
- MCP transport lifecycle остаётся независимым от lifecycle remote resource.
- Streamable HTTP и stdio/executable остаются поддерживаемыми adapters MCP
  runtime.
- Новые builtin definitions используют Streamable HTTP; существующие builtin
  stdio/executable registrations мигрируют постепенно.
- Текущие `mcp.config` и `MCPServerManager` мигрируют без поломки local mode.

## Граница ответственности

Этот update реализует только сторону агента:

- models/configuration registry;
- snapshot/revision и binding coordinates;
- precedence/visibility;
- trusted tool execution/presentation metadata;
- dispatch, retry и normalized outcomes;
- remote handle ownership metadata;
- lifecycle hook integration;
- compatibility migration.

Он не описывает внутреннюю архитектуру MCP-сервисов, их базы данных, очереди,
workers, кэш, браузеры, поисковые движки или deployment.

## Scope model

```text
builtin
  поставляется и поддерживается кодом проекта

instance
  подключён администратором конкретного deployment

user
  принадлежит account/principal; до v0.8 остаётся schema-ready или local scope

session
  временно подключён к одной conversation/session/run boundary
```

Scope определяет visibility и precedence, но не заменяет capability или
authorization policy.

До v0.8 `user` не объявляется полноценно изолированным между accounts. Реализация
должна сохранять owner-ready fields без ложного security claim.

## Transport policy

Transport и scope являются разными характеристиками server definition.

MCP runtime продолжает поддерживать:

```text
Streamable HTTP
stdio/executable
```

Для новых builtin definitions применяется правило:

```text
scope=builtin
→ transport=Streamable HTTP
```

Существующие builtin stdio/executable entries являются migration input. Их
observable behavior сохраняется на переходном этапе, после чего они удаляются
или заменяются отдельными Streamable HTTP MCP-сервисами.

Для user MCP-серверов stdio/executable остаётся обычным поддерживаемым
транспортом. Registry не делает transport источником trust: trusted metadata,
permissions, retry и presentation определяются scope/policy и approved binding.

## Registry contracts

Минимальные модели:

```text
MCPServerDefinition
MCPServerScope
MCPRegistrySnapshot
MCPServerBinding
MCPToolBinding
RegistryRevision
```

`MCPServerDefinition` хранит только безопасную metadata и reference на secret
configuration. Registry listing не возвращает LLM raw credentials.

Snapshot:

- immutable внутри одной discovery/execution decision;
- имеет monotonically changing revision;
- изменяется при add/remove, enable/disable, reconnect generation или tool-list
  change;
- сохраняет exact server/tool binding coordinates;
- повторно проверяется перед фактическим tool execution.

Предварительный precedence должен быть deterministic. Конфликт public tool names
не разрешается случайным порядком загрузки; используется явная namespace/binding
policy либо controlled conflict.

## Tool execution metadata

Для trusted tool registry хранит:

```text
capability and operation kind
side-effect class
retry policy
timeout profile
result handling
presentation profile
remote-resource behavior
required permissions/budgets
schema/integration compatibility
```

Минимальные retry classes:

```text
safe
idempotent
never_automatic
```

Минимальные outcomes:

```text
succeeded
failed
rejected
cancelled
unknown
```

Generic/untrusted MCP tool без approved metadata получает conservative defaults и
обычное отображение.

Документ не вводит произвольное обязательное поле вида
`contract: web-search.v1`. Конкретная модель versioned compatibility metadata
будет определена вместе с `MCPServerDefinition` и не должна дублировать tool
schemas без необходимости.

## Presentation profiles

Красивое отображение не требует превращать каждый известный MCP tool в manager
function. `ToolDispatcher` создаёт canonical progress event на основании trusted
presentation profile реального binding.

```text
known builtin tool
→ semantic localized progress

unknown/unapproved tool
→ generic safe tool progress
```

MCP-server-supplied текст не становится trusted UI instruction. Telegram/Web/CLI
по-прежнему самостоятельно выполняют coalescing и surface-specific rendering.

## Remote resource handles

Опциональный trusted descriptor указывает, что tool создаёт remote resource и
какой declared cleanup operation ему соответствует.

Agent Runtime хранит:

```text
resource type
opaque resource ID
server/registry binding
lifecycle owner type and ID
state
cleanup policy
```

В v0.4 основной lifecycle owner — `AgentCycle`. Contracts `run` и `task_run`
сохраняются forward-compatible для v0.6.

Lifecycle hooks универсальны и могут обслуживать не только builtin scope, однако
automatic integration разрешается только trusted registry policy:

- builtin — versioned system descriptor;
- instance — explicit administrator-approved descriptor;
- user/session — conservative default до отдельного trust/authorization решения.

Cleanup является bounded best effort и не отменяет уже подготовленный корректный
final result. MCP-сервис отвечает за фактическую окончательную очистку и
expiration ресурсов.

## Независимые lifecycle

```text
MCP transport runtime
connected → unhealthy → reconnecting → connected

remote resource
active → closing → closed | expired | lost | unresolved
```

Transport reconnect не закрывает remote resource автоматически. Потеря remote
resource не требует уничтожать весь MCP runtime. Agent Runtime различает эти
состояния и не выводит одно из другого неявно.

## Migration

Текущий `mcp.config` преобразуется без обязательного изменения поведения:

- существующие entries по умолчанию получают `instance` либо explicit legacy
  compatibility scope;
- системные entries переносятся в trusted builtin definitions;
- startup-required/optional semantics сохраняются;
- Streamable HTTP и stdio/executable остаются adapters MCP runtime;
- новые builtin registrations создаются только для Streamable HTTP services;
- старые builtin executable entries сохраняются только до parity migration или
  удаления соответствующего сервера;
- user definitions могут использовать stdio/executable;
- текущий dynamic discovery остаётся доступным;
- compatibility facade старого `MCPClient` делегирует новым registry/dispatcher
  components до migration всех callers.

Не допускается одномоментный rewrite `mcp_client.py` вместе с этим update.
Предварительно должен быть завершён `v0.4-runtime-modularization`.

## Non-goals

- реализация конкретного builtin MCP-сервиса;
- PostgreSQL-backed или distributed registry;
- Redis/event-bus synchronization;
- полноценная account authorization;
- public marketplace/install flow;
- автоматическое доверие внешней metadata;
- общий scheduler или background workers;
- изменение MCP transport protocol без отдельной необходимости.

## Acceptance criteria

```text
legacy config
→ equivalent available MCP servers/tools after migration
```

```text
builtin/instance/user/session definitions
→ deterministic visible registry snapshot and revision
```

```text
new builtin definition
→ Streamable HTTP binding

user stdio definition
→ supported local binding with conservative trust defaults
```

```text
trusted builtin tool
→ semantic localized progress

unknown tool
→ generic safe progress
```

```text
safe operation + recoverable transport failure
→ bounded retry according to policy
```

```text
mutating operation + lost response
→ outcome unknown
→ no automatic repeated side effect
```

```text
remote handle created
→ exact server/resource/owner registration
→ no dependency on MCP connection object
```

```text
cycle completed/failed/cancelled/reset/shutdown
→ bounded cleanup request for owned resources
→ cleanup failure does not invalidate completed AgentResult
```

```text
optional builtin server unavailable/incompatible
→ controlled disabled/degraded capability
→ unrelated Agent Runtime remains operational
```

## Зависимости и продолжение

- [`../v0.4-runtime-modularization/README.md`](../v0.4-runtime-modularization/README.md)
  создаёт `AgentRuntime`, `ToolDispatcher`, registry и hook ports.
- `v0.5` может persist registry-related metadata через существующие ports, но не
  обязан вводить distributed coordination.
- `v0.6.9-distributed-capability-registry` переносит registry revisions,
  visibility и ownership-ready metadata в durable multi-process runtime.
- `v0.7` переиспользует ту же scope-модель для skills.
- `v0.8` добавляет полноценное principal/account authorization enforcement.
