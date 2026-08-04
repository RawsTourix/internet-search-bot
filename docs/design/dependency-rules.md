---
id: design.dependency-rules
version: cross-version
spec_status: accepted
implementation_status: mixed
last_reviewed: 2026-08-02
---

# Правила зависимостей и модульных границ

## Цель

Правила позволяют развивать проект от модульного монолита до distributed
runtime без изменения направления зависимостей при каждом новом infrastructure
backend или application composition root.

Application/hosting profiles определены в
[`runtime-and-deployment-profiles.md`](runtime-and-deployment-profiles.md).

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
- `agent_runtime` не импортирует Service Application или Future Local Agent
  composition modules.
- `agent_runtime` не импортирует SQLAlchemy/Alembic models как domain contracts.
- `agent_runtime` не импортирует Redis/arq, Docker, Kubernetes или конкретный
  object-storage SDK.
- `agent_runtime` не импортирует клиент или schema конкретного builtin MCP-сервиса.
- AgentRuntime не читает строковый application `mode` для включения host shell,
  stdio admission или другой security-critical capability.
- Domain packages не обращаются к global application singleton.
- Client adapters не вызывают `MCPClient` как скрытый service locator.
- MCP runtime не владеет session, conversation, authorization, workflow,
  application profile или remote-resource ownership state агента.
- MCP transport adapter не принимает admission decision самостоятельно.
- MCP tool output не регистрирует trusted cleanup/presentation/retry policy
  напрямую.
- Repository не содержит orchestration policy, а router не реализует domain
  transaction.
- LLM, skill или sandbox не получают прямой unrestricted database access.
- Service Application не выполняет user-provided executable MCP или terminal
  command напрямую в trusted control-plane process.

## Ports перед infrastructure

Перед добавлением нового backend определяется нейтральный contract:

```text
LLMClient
ConfigProvider
SessionRepository
CycleRepository
RunRepository
ContentStore
ArtifactStore
EventSink
JobQueue
ExecutionBackend
CommandExecutionPort
ToolRegistry
ToolDispatcher
CapabilityPolicy
MCPTransportAdmissionPolicy
RemoteResourceRegistry
RemoteResourceLifecyclePort
```

Первой реализацией может быть in-memory/filesystem/local adapter. PostgreSQL,
Redis, Docker и remote runner добавляются как совместимые implementations.

## AgentRuntime и application composition

```text
Agent Core contracts
→ AgentRuntime
→ profile-specific composition root
```

AgentRuntime получает готовые dependencies и policy bundle. Он не создаёт
Gateway, Telegram/Web, host terminal, sandbox manager или configuration loader.

Текущий composition root:

```text
build_service_application(settings)
→ ServiceApplicationContainer
```

Future Local Agent Application сможет использовать отдельный root:

```text
build_local_agent_application(settings)
→ LocalAgentApplicationContainer
```

Второй root остаётся future/provisional и не входит в текущую реализацию. Общий
AgentRuntime contract не должен требовать его существования.

Self-hosted/managed hosting, environment и topology выбирают adapters/default
policies Service Application, но не меняют направление зависимостей.

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
- `MCPTransportAdmissionPolicy` решает допустимость definition до connect/spawn.
- `ToolRegistry` владеет immutable definitions/snapshots/bindings.
- `RemoteResourceRegistry` хранит opaque ownership coordinates, но не внутреннее
  состояние внешнего сервиса.
- `RemoteResourceLifecyclePort` запрашивает cleanup через declared binding и не
  импортирует concrete service client в AgentRuntime.
- Presentation/cleanup/retry metadata поступает только из trusted registry
  source.
- Scope, transport, trust и admission являются отдельными characteristics.

## Terminal и execution boundary

```text
terminal manager tool
→ CommandExecutionPort
→ profile-specific executor
```

В Service Application:

```text
CommandExecutionPort
→ approved sandbox/execution adapter
→ ExecutionBackend
```

В Future Local Agent Application host executor сможет быть отдельным adapter с
permission/approval policy.

Manager tool и AgentRuntime не импортируют subprocess, shell, Docker SDK или
physical host paths как часть domain contract.

Файлы execution workspace импортируются через ContentStore/ArtifactStore и
передаются пользователю через delivery adapters. Execution backend не пишет
напрямую в Telegram/Web и не становится authoritative artifact store.

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

Skill/tool не получает ссылку на composition root и не может изменить
application profile или capability ceiling.

## Configuration boundaries

Service `ConfigProvider` читает operator-owned `agent.config` и публикует
validated immutable snapshot. Per-user service configuration загружается через
owner-aware application repositories, а не через общий service config provider.

Future Local Agent Application сможет иметь собственный root config provider,
переиспользующий общие submodels.

AgentRuntime зависит от revision-bound settings/policies, а не от physical config
file или environment loader.

## Composition root

Создание concrete adapters и связывание зависимостей выполняется в явном
composition root:

```text
ConfigProvider
→ build_service_application(snapshot)
→ ServiceApplicationContainer
→ start lifecycle
→ serve interfaces/workers
→ stop lifecycle
```

Import модуля не должен незаметно запускать HTTP clients, подключать MCP servers,
создавать database connections или регистрировать global mutable runtime.

Composition root является owner:

- application profile и security ceiling;
- transport admission policy;
- concrete configuration snapshot type;
- persistence/event/execution adapters;
- client surfaces;
- lifecycle startup/shutdown.

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
applications/service/
```

Будущий `applications/local_agent/` добавляется только при проектировании
реального Local Agent Application.

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
InProcessAgentRuntime → AgentRuntimeWorker/Service adapter
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
- imports concrete infrastructure из agent loop;
- imports Service Application из AgentRuntime;
- direct adapter-to-adapter coupling;
- direct production MCP call в обход Dispatcher/admission policy;
- user-provided executable spawn в Service Application;
- direct host shell fallback при unavailable sandbox;
- shared mutable singleton как источник runtime state;
- неявную передачу physical filesystem paths через domain contracts;
- привязку remote resource lifecycle к объекту MCP connection;
- смешение operator config и per-user settings repositories.

Исключения оформляются как временная migration boundary с явным сроком удаления,
а не становятся новым постоянным правилом.
