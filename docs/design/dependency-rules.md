---
id: design.dependency-rules
version: cross-version
spec_status: accepted
implementation_status: mixed
last_reviewed: 2026-08-01
---

# Правила зависимостей и модульных границ

## Цель

Правила позволяют развивать проект от модульного монолита до distributed
runtime без изменения направления зависимостей при каждом новом infrastructure
backend.

## Базовое направление

```text
domain models and invariants
        ↓
application services and orchestration
        ↓
ports / protocols
        ↓
infrastructure and interface adapters
```

Верхние слои могут вызывать нижние через contracts. Domain/application код не
должен импортировать конкретные transport, persistence или execution frameworks.

## Обязательные запреты

- `agent_runtime` не импортирует FastAPI, Telegram SDK или Web adapters.
- `agent_runtime` не импортирует SQLAlchemy/Alembic models как domain contracts.
- `agent_runtime` не импортирует Redis/arq, Docker, Kubernetes или конкретный
  object-storage SDK.
- `agent_runtime` не импортирует клиент или schema конкретного builtin MCP-сервиса.
- Domain packages не обращаются к global application singleton.
- Client adapters не вызывают `MCPClient` как скрытый service locator.
- MCP runtime не владеет session, conversation, authorization, workflow или
  remote-resource ownership state агента.
- MCP tool output не регистрирует trusted cleanup/presentation/retry policy
  напрямую.
- Repository не содержит orchestration policy, а router не реализует domain
  transaction.
- LLM, skill или sandbox не получают прямой unrestricted database access.

## Ports перед infrastructure

Перед добавлением нового backend определяется нейтральный contract:

```text
LLMClient
SessionRepository
CycleRepository
RunRepository
ContentStore
ArtifactStore
EventSink
JobQueue
ExecutionBackend
ToolRegistry
ToolDispatcher
RemoteResourceRegistry
RemoteResourceLifecyclePort
```

Первой реализацией может быть in-memory/filesystem/local adapter. PostgreSQL,
Redis, Docker и remote runner добавляются как совместимые implementations.

## Tool и MCP boundaries

```text
AgentRuntime
→ ToolDispatcher
→ ToolProvider/MCP adapter
→ MCP runtime/server
```

- `ToolDispatcher` владеет invocation policy, normalized outcome и canonical
  progress metadata.
- MCP runtime владеет connection, generation, reconnect и transport result.
- `ToolRegistry` владеет immutable definitions/snapshots/bindings.
- `RemoteResourceRegistry` хранит opaque ownership coordinates, но не внутреннее
  состояние внешнего сервиса.
- `RemoteResourceLifecyclePort` запрашивает cleanup через declared binding и не
  импортирует concrete service client в AgentRuntime.
- Presentation/cleanup/retry metadata поступает только из trusted registry
  source.

## Композиция расширений

Planning, artifacts, RAG, skills и execution policies подключаются через
композиционные интерфейсы:

```text
ToolProvider
RuntimeProjectionProvider
ActionGuard
EvidenceContributor
LifecycleHook
CapabilityPolicy
```

Новый extension не должен требовать очередного production subclass центрального
runtime или зависеть от порядка MRO.

Lifecycle hook работает с typed lifecycle context и generic ports. Он не должен
получать весь `ApplicationContainer`, concrete MCP connection или client adapter
как service locator.

## Composition root

Создание concrete adapters и связывание зависимостей выполняется в одном
composition root:

```text
build_application(settings)
→ ApplicationContainer
→ start lifecycle
→ serve interfaces/workers
→ stop lifecycle
```

Import модуля не должен незаметно запускать HTTP clients, подключать MCP servers,
создавать database connections или регистрировать global mutable runtime.

## Модульная структура

Предпочтительна организация по предметным областям:

```text
agent_runtime/
runs/
sessions/
llm/
tools/
mcp/
context/
finalization/
planning/
artifacts/
memory/
ingress/
delivery/
execution/
```

Внутри domain module могут находиться models, service, ports и adapters. Одна
общая папка `services/` или `repositories/` для всей системы не является
обязательной и при росте часто скрывает реальные bounded contexts.

## Переход к процессам и сервисам

Сначала contract используется in-process. После стабилизации можно заменить
вызов transport adapter:

```text
InProcessEventSink → RedisEventSink
LocalJobQueue → ArqJobQueue
LocalExecutionBackend → DockerExecutionBackend → RemoteRunnerBackend
LocalMCPRegistry → PostgreSQLDistributedMCPRegistry
```

Domain/application caller при этом не меняется.

Внутренняя архитектура отдельного MCP-сервиса не становится зависимостью
AgentRuntime. Стороны связываются через
[`contracts/builtin-mcp-service-contract.md`](contracts/builtin-mcp-service-contract.md)
и MCP schemas.

## Проверка архитектуры

CI или architecture tests должны постепенно запрещать:

- обратные imports из domain в adapters;
- циклические зависимости package-level;
- imports конкретной infrastructure из agent loop;
- direct adapter-to-adapter coupling;
- direct production MCP call в обход Dispatcher policy;
- shared mutable singleton как источник runtime state;
- неявную передачу physical filesystem paths через domain contracts;
- привязку remote resource lifecycle к объекту MCP connection.

Исключения оформляются как временная migration boundary с явным сроком удаления,
а не становятся новым постоянным правилом.
