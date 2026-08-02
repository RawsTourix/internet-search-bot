---
id: design.contracts.builtin-mcp-service
version: cross-version
spec_status: accepted
implementation_status: mixed
last_reviewed: 2026-08-02
---

# Контракт встроенных MCP-сервисов

## Назначение

Документ определяет общую интеграционную границу между Agent Runtime и
отдельными MCP-сервисами, которые поставляются и тестируются как встроенные
возможности системы.

```text
Agent Runtime
→ MCP registry и ToolDispatcher
→ builtin MCP service
```

`builtin` означает доверенную системную регистрацию и управляемый контракт, но
не означает in-process реализацию, постоянную доступность или автоматическое
разрешение каждому пользователю.

Документ не задаёт внутренний стек, хранилище, очереди, workers, способ
масштабирования или предметную реализацию конкретного MCP-сервиса.

Application/hosting profiles и общая transport admission model определены в
[`../runtime-and-deployment-profiles.md`](../runtime-and-deployment-profiles.md).

## Область действия

Контракт применяется к MCP-сервисам, которые:

- поставляются или явно поддерживаются проектом;
- подключаются через MCP registry со scope `builtin`;
- имеют versioned tool schemas и trusted metadata со стороны агента;
- работают как отдельные deployable services за network boundary;
- могут быть optional и временно недоступными;
- при необходимости создают stateful remote resources и возвращают opaque
  handles.

## Transport policy

Общий MCP runtime поддерживает несколько transport adapters, включая:

```text
Streamable HTTP
stdio/executable
```

Поддержка stdio/executable не считается legacy-функцией MCP runtime.

Для scope `builtin` действует отдельное архитектурное правило:

```text
новая builtin integration
→ Streamable HTTP
```

Существующие builtin MCP-серверы, запускаемые через stdio/executable, являются
legacy именно как builtin integrations. Они поддерживаются на время миграции и
постепенно:

- удаляются, если больше не нужны;
- либо выделяются в отдельные MCP-сервисы со Streamable HTTP boundary.

Transport support не определяет admission. Service Application не запускает
user/session-provided executable MCP code в trusted control plane. Self-hosted
operator может отдельно разрешить operator-managed `instance` stdio definition
через deployment policy. Future Local Agent Application сможет использовать
stdio под собственной host permission model.

Выбор транспорта сам по себе не задаёт scope, trust, permissions или retry
semantics.

## Deployment и network boundary

Builtin MCP service является отдельным service workload. Его endpoint не
считается публичным пользовательским API только потому, что использует HTTP.

Конкретный deployment должен:

- ограничивать network exposure согласно infrastructure policy;
- проверять service identity/credentials вызывающей стороны;
- применять quotas, timeouts и abuse protection;
- не полагаться только на скрытность URL или `Mcp-Session-Id`;
- не передавать service secrets через LLM context или обычный registry listing.

Точный механизм service authentication — token, short-lived credential, mTLS или
workload identity — выбирается deployment infrastructure и пока не фиксируется
этим общим контрактом.

## Граница ответственности

### Agent Runtime отвечает за

- регистрацию MCP-сервера и его scope;
- discovery и binding доступных tools;
- проверку registry revision/server generation перед вызовом;
- capability, authorization, admission и budget policy;
- dispatch, transport timeout и controlled recovery;
- нормализацию результатов и ошибок;
- классификацию side effects и retry policy;
- canonical progress/trace events;
- локализованное пользовательское представление через trusted presentation
  profiles;
- регистрацию ownership opaque remote handles;
- bounded best-effort cleanup requests по lifecycle hooks;
- исключение raw secrets и недоверенного tool output из trusted metadata.

### MCP-сервис отвечает за

- фактическое выполнение операции;
- валидацию входной schema и opaque handles;
- аутентификацию/авторизацию service caller согласно deployment contract;
- внутреннее состояние и изоляцию ресурсов;
- собственную concurrency, resource и failure policy;
- структурированные domain/transport-independent outcomes;
- идемпотентный cleanup, если сервис создаёт remote resources;
- окончательную серверную очистку и expiration независимо от Agent Runtime;
- корректное поведение после reconnect клиента;
- совместимость declared integration metadata и versioned schemas;
- отсутствие зависимости application state от lifetime одного MCP-соединения.

Агент не знает внутреннее устройство remote resource и не становится его
фактическим runtime owner. Сервис не владеет `AgentCycle`, conversation,
workflow, delivery или authorization state агента.

## Scope и trust

Общая scope-модель:

```text
builtin
instance
user
session
```

Этот документ задаёт полный trusted integration contract для `builtin`.
Те же механизмы могут быть разрешены `instance`-серверу администраторской
policy. `user` и `session` не получают trusted lifecycle или presentation
metadata только на основании собственного tool output.

Scope определяет видимость и precedence registry, но сам по себе не выдаёт
разрешение на вызов tools или доступ к secrets/resources.

Scope `builtin` назначается доверенной operator/runtime boundary. Пользователь не
получает builtin trust передачей поля в собственном MCP definition.

## Обязательная metadata инструмента

Для встроенного инструмента Agent Runtime хранит trusted descriptor, который
может включать:

```text
public tool identity
remote server/tool binding
capability and operation kind
read-only / mutating / external-side-effect class
retry policy and timeout profile
result handling policy
presentation profile
remote-resource behavior
required permissions and budgets
schema/integration compatibility range
```

Metadata берётся из versioned code/configuration агента или другого доверенного
registry source. MCP tool output не может назначить себе cleanup operation,
trusted presentation, permissions или retry class.

## Tool execution semantics

Минимальные retry classes:

```text
safe
  операция может быть автоматически повторена в пределах общего budget

idempotent
  повтор допустим только с declared idempotency semantics

never_automatic
  автоматический повтор после неопределённого transport outcome запрещён
```

Минимальные normalized outcomes:

```text
succeeded
failed
rejected
cancelled
unknown
```

`unknown` используется, если внешнее действие могло завершиться, но Agent Runtime
не получил достоверный результат. Для mutating/side-effecting operations такой
outcome не превращается в blind retry. Runtime должен проверить состояние другим
безопасным способом, запросить решение пользователя либо завершить действие с
явным ограничением.

## Progress и presentation

Canonical progress event создаёт Agent Runtime. MCP-сервис может передавать
структурированный технический progress, но не определяет финальный
пользовательский текст или client-specific UI lifecycle.

```text
MCP progress/result
→ MCP integration adapter
→ ToolDispatcher
→ canonical ProgressEvent
→ Telegram/Web/CLI renderer
```

Для trusted builtin tool Agent Runtime может применять semantic presentation
profile. Неизвестный или неразмеченный tool получает generic safe fallback.
Server-supplied text считается недоверенными данными и не становится trusted UI
instruction.

## Stateful remote resources

Stateful tool может вернуть opaque handle, например handle browser session,
remote workspace или длительной service operation. Конкретный тип ресурса не
важен для общего контракта.

Agent-side registration хранит только необходимые coordinates:

```text
resource type
opaque resource ID
owning MCP registry/server identity
lifecycle owner type and ID
cleanup operation declared trusted metadata
state: active | closing | closed | expired | lost | unresolved
created/last-seen timestamps
```

Handle:

- не раскрывает внутреннюю топологию или credentials сервиса;
- проверяется сервисом при каждом использовании;
- не считается действительным только из-за наличия строки в LLM context;
- не связывается с объектом MCP transport session;
- не переиспользуется между owner boundaries без policy.

## Lifecycle ownership и hooks

Lifecycle hooks являются общей возможностью Agent Runtime, а не специальной
функцией одного сервера. Автоматическая реакция на hook разрешается только через
trusted registry contract.

Поддерживаемые owner scopes развиваются по версиям:

```text
tool_call
cycle
run
task_run
session
explicit
```

В v0.4 основной owner — `AgentCycle`; `AgentRun` и `TaskRun` становятся
полноценными durable boundaries позднее.

Cleanup может запрашиваться при:

- successful terminal completion;
- controlled failure;
- cancellation/interruption;
- session reset;
- runtime shutdown;
- expiration owner-а;
- policy переходе из `WAITING_USER`.

Agent cleanup:

- bounded по времени;
- идемпотентен на уровне вызова declared cleanup tool;
- best effort;
- не отменяет уже подготовленный корректный final result;
- фиксирует unresolved/lost state при недоступности сервиса.

MCP-сервис остаётся окончательным владельцем фактической очистки и обязан иметь
собственную expiration/orphan policy. Потеря MCP-соединения не доказывает потерю
remote resource; restart сервиса не должен молча превращать lost resource в
успешно закрытый.

## Failure model

Agent Runtime различает как минимум:

```text
server unavailable
transport interrupted
schema/permission rejection
tool execution failed
tool outcome unknown
remote resource expired
remote resource lost
cleanup failed or timed out
schema/integration incompatible
```

Эти состояния не сводятся к одному универсальному сообщению без structured
reason и retryability metadata.

Optional builtin server при несовместимости или недоступности может быть
controlled disabled. Его отказ не должен разрушать unrelated Agent Runtime
capabilities.

## Compatibility и versioning

Builtin registration фиксирует:

- versioned trusted integration metadata;
- tool schema/version snapshot;
- supported compatibility range;
- registry revision;
- server generation/health state.

Это compatibility metadata registry, а не обязательное свободно придуманное
поле вроде имени предметного «контракта» в пользовательской конфигурации.
Конкретная модель и названия полей определяются вместе с `MCPServerDefinition`.

Startup/discovery проверяет совместимость до предоставления tool binding модели.
Backward-incompatible schema change требует новой version boundary или
controlled migration. Stale discovery snapshot повторно проверяется перед
execution.

## Security invariants

- Tool output, webpages, documents и remote progress считаются недоверенными.
- Builtin scope не отменяет capability/authorization/admission policy.
- Secrets передаются только через scoped configuration/reference, не через LLM
  context или обычный registry listing.
- Cleanup вызывается только на сервере и operation, закреплённых trusted
  descriptor.
- Opaque handle не даёт доступ к чужому resource без owner/policy checks.
- Transport reconnect не расширяет permissions и не меняет lifecycle owner.
- Mutating operation с `unknown` outcome не повторяется автоматически.
- Service Application не выполняет user-provided executable MCP в trusted
  control plane.
- HTTP endpoint builtin service защищается service identity и network policy, а
  не скрытностью адреса.

## Acceptance contract

Каждый новый builtin MCP-service integration должен иметь проверки:

```text
Streamable HTTP connect and discovery
schema/integration compatibility
service authentication/authorization failure
normal tool invocation
server unavailable and optional degradation
transport reconnect and server restart
safe/idempotent/never-automatic retry behavior
semantic and generic progress presentation
result-size and secret redaction boundaries
remote handle registration and owner isolation
normal, failed, cancelled and reset cleanup
cleanup timeout and unavailable service
expired/lost resource
unknown mutating outcome without blind retry
```

Миграция существующего builtin stdio/executable server дополнительно проверяет
parity доступных tools и observable behavior до удаления старого запуска.

Version-specific implementation и release gates могут усиливать этот набор, но
не должны ослаблять перечисленные invariants.

## Non-goals

Контракт не определяет:

- предметные tools конкретного сервиса;
- внутренние database/queue/cache модели;
- конкретные web frameworks или MCP SDK;
- container orchestration и deployment topology;
- конкретный механизм service authentication;
- browser/search/document processing implementation;
- multi-user authorization до соответствующей версии агента;
- distributed persistence registry до v0.6;
- Future Local Agent Application или его host permission model.
