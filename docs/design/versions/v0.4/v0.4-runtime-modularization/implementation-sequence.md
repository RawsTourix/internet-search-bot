---
id: design.v0.4.runtime-modularization.sequence
version: v0.4
update: v0.4-runtime-modularization
spec_status: accepted
implementation_status: planned
last_reviewed: 2026-08-02
---

# Последовательность модульного рефакторинга runtime

Cross-version граница application profiles:
[`../../../runtime-and-deployment-profiles.md`](../../../runtime-and-deployment-profiles.md).

## 1. Предварительная инвентаризация

До перемещения кода фиксируются ответственности `mcp_client.py`, production
subclasses/mixins и прямые callers.

Минимальные группы:

```text
configuration and models
LLM provider transport and retry
MCP transport/runtime/registry
manager tools and dispatch
session/cycle lifecycle
context accounting and compaction
progress/trace events
finalization and grounding
planning/artifact extensions
archive/persistence compatibility
application composition and entrypoints
```

Для каждой группы определяются current owner, mutable state, входы, выходы,
errors и side effects.

Отдельно инвентаризируются:

- все loaders `mcp.config`;
- import-time application/config construction;
- environment fallbacks, дублирующие LLM или MCP configuration;
- legacy builtin stdio MCP entrypoints и их private environment parameters;
- FastAPI/Telegram/Gateway dependencies текущего composition root;
- direct host-process/terminal assumptions;
- параметры, которые фактически относятся к hosting mode, topology или
  environment, а не к AgentRuntime.

Инвентаризация фиксирует текущий profile как single-process self-hosted Service
Application. Она не проектирует Future Local Agent Application.

## 2. Characterization baseline

Перед рефакторингом добавляются или закрепляются tests для:

- JSON `AgentAction` и tool-call sequence;
- main LLM retry и context-overflow recovery;
- result/cycle compaction;
- `WAITING_USER`, resume и infrastructure interruption;
- MCP reconnect/recovery;
- progress/trace events;
- planning guards/reconciliation;
- artifact access, candidate promotion и delivery;
- final processing и forced final answer;
- `CycleInbox` safe checkpoints после реализации input runtime;
- current configuration loading and validation;
- equivalent startup через compatibility `mcp.config` filename;
- explicit Service Application startup/shutdown without import-time singleton;
- отсутствие зависимости core contracts от Telegram/FastAPI adapters.

Tests проверяют observable contracts, а не private method layout.

## 3. Модели, ConfigProvider и configuration

Из центрального файла выносятся pure models и validated settings:

```text
config/models.py
config/provider.py
llm/models.py
llm/config.py
mcp/models.py
mcp/config.py
sessions/models.py
runtime/models.py
finalization/models.py
```

Вводятся generic contracts:

```python
class ConfigProvider(Protocol):
    async def get_snapshot(self) -> AgentConfigSnapshot: ...
    async def reload(self) -> AgentConfigSnapshot: ...
```

```text
AgentConfigSnapshot
ConfigRevision
ConfigurationValidationError
```

Требования:

- отсутствие runtime side effects при import;
- одна модель не объявляется в нескольких packages;
- compatibility imports временно допускаются через re-export;
- configuration loading отделяется от concrete service construction;
- весь файл валидируется до публикации новой revision;
- invalid reload сохраняет предыдущий valid snapshot;
- публикация snapshot атомарна для readers;
- один AgentCycle фиксирует configuration revision на старте;
- отдельные services не перечитывают configuration file самостоятельно.

Filename migration:

```text
mcp.config
→ compatibility alias

agent.config
→ canonical Service Application filename
```

Переименование не выполняется как silent breaking change: loader сначала
поддерживает оба имени с явным precedence и диагностикой, после migration всех
entrypoints старое имя удаляется.

`agent.config` остаётся operator-owned service deployment configuration. Per-user
settings/MCP credentials не становятся его динамическими секциями.

Общие config submodels проектируются переиспользуемыми, но root schema Future
Local Agent Application и имя его config файла не определяются этим update.

## 4. LLM port

Определяются:

```python
class LLMClient(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...
```

Adapters:

```text
OpenAICompatibleLLMClient
ProviderInputAdapter
ResilientLLMClient
```

Agent loop не знает `httpx`, URL, headers и provider response shape. Token
accounting использует то же prompt-bearing representation, что provider adapter.

LLM adapter создаётся из validated snapshot. Новая операция может получить
новую LLM configuration revision без обязательного restart Gateway; активный
AgentCycle продолжает использовать revision, с которой был начат.

## 5. MCP runtime как самостоятельный owner

`MCPServerManager` перестаёт получать весь `MCPClient` как owner.

Самостоятельный MCP runtime владеет:

- server configurations;
- connections/runtime generations;
- reconnect locks;
- tool bindings и registry revision;
- transport timeouts/recovery;
- normalized transport execution result.

Agent runtime видит `ToolCatalog`/`ToolExecutor`, а не внутренние connection
objects. MCP runtime не владеет `AgentCycle`, conversation или remote-resource
ownership.

Изменение MCP configuration публикуется через ConfigProvider revision и
обрабатывается MCP runtime как controlled definition diff. Оно не требует
перезапуска Gateway только ради перечитывания файла.

MCP runtime поддерживает transport adapters, но не определяет самостоятельно,
какие scope/transport combinations разрешены конкретному application profile.
Admission policy поступает из composition root и реализуется следующим update.

## 6. Tool registry и dispatcher

Вводятся composition contracts:

```text
ToolDefinition
ToolRequest
ToolResult
ToolProvider
ToolRegistry
ToolDispatcher
ToolPolicy
ToolExecutionSemantics
ToolOutcome
ToolPresentationProfile
CapabilityPolicy
```

Manager tools, MCP tools, artifact tools и planning tools регистрируются как
providers. Dispatcher отвечает за единый invocation envelope, trust marking,
progress metadata, result handling и error normalization.

Manager tool contract не вызывает host terminal напрямую. Future terminal tools
должны зависеть от нейтрального execution port, выбранного composition root.

На этом этапе достаточно generic contracts и compatibility defaults. Concrete
MCP scopes, approved binding profiles, profile-aware transport admission,
side-effect-aware retry registry и remote resource integration реализуются
следующим update
[`v0.4-mcp-registry-foundation`](../v0.4-mcp-registry-foundation/README.md).

## 7. Context management

Отдельные components получают ответственность за:

- request/token accounting;
- result persistence and compaction policy;
- cycle segment selection;
- `CycleWorkingMemory` generation;
- provider overflow recovery;
- runtime context projection.

Context manager не владеет session lifecycle и не вызывает client delivery.
Compactor получает LLM port как dependency.

## 8. Event и trace contracts

Определяются:

```python
class RuntimeEventSink(Protocol):
    async def publish(self, event: ProgressEvent) -> None: ...

class TraceStore(Protocol):
    async def append(self, event: TraceEvent) -> None: ...
```

Local implementation сохраняет callback/list/file compatibility. В v0.5
появится PostgreSQL adapter, в v0.6 — event-bus transport.

Canonical event создаёт runtime; client-specific coalescing/rendering находится
в delivery adapter. Event envelope должен позволять Dispatcher добавить
semantic operation/binding metadata без surface-specific текста.

Configuration reload имеет отдельный structured event с old/new revision и
result `applied|rejected`, без публикации raw secrets или полного config payload.

Application profile/hosting metadata может входить в safe diagnostic context, но
не используется AgentRuntime как условие включения скрытых capabilities.

## 9. Finalization pipeline

В pipeline выделяются:

```text
FinalProcessingModeSelector
EvidenceContributor registry
GroundingService
FormattingService
ActionGuard chain
ForcedAnswerService
FinalCommitPreparation
```

Planning, artifacts и RAG добавляют evidence/guards через contracts, а не через
переопределение общей финализации.

## 10. Session, cycle и run repositories

На v0.4 вводятся ports с in-memory/filesystem implementations:

```text
SessionRepository
CycleRepository
RunRepository
RuntimeStateStore
```

`RunRepository` пока может хранить compatibility representation одного cycle,
но identity и contract готовятся к durable `AgentRun` v0.6.

Нельзя дописывать authoritative state повторным открытием архивного JSON после
основного commit. Один runtime snapshot сохраняется через repository boundary.

Cycle metadata фиксирует использованную configuration revision, достаточную для
диагностики и воспроизводимости без копирования secrets в runtime state.

Repository contracts не предполагают, что operator config и per-user settings
являются одним хранилищем.

## 11. Композиционные runtime extensions

Наследственная production chain постепенно заменяется:

```text
RuntimeProjectionProvider
ActionGuard
EvidenceContributor
LifecycleHook
ToolProvider
```

Порядок вызова extension points явный, deterministic и покрыт tests. Mixin может
временно оставаться compatibility adapter, но не получает новую
ответственность.

`LifecycleHook` получает typed lifecycle context, cancellation/deadline budget и
доступ только к объявленным ports. Конкретный registry remote handles и cleanup
policy остаются задачей следующего update, но не должны требовать нового
subclass или возврата lifecycle logic в `MCPClient`.

## 12. AgentRuntime

Целевой orchestration object:

```python
class AgentRuntime:
    def __init__(
        self,
        llm: LLMClient,
        tools: ToolDispatcher,
        sessions: SessionRepository,
        cycles: CycleRepository,
        context: ContextManager,
        finalizer: FinalizationPipeline,
        events: RuntimeEventSink,
        policies: RuntimePolicies,
    ): ...

    async def run(self, command: RunCommand) -> AgentResult: ...
```

Он координирует lifecycle, но не реализует concrete transport/persistence и не
читает configuration file напрямую. Run command/composition передаёт ему
validated snapshot или revision-bound dependencies.

AgentRuntime не получает строковый `mode=service|local` для самостоятельного
включения host shell или выбора security policy. Composition root передаёт уже
разрешённые tools, ports и policy bundle.

## 13. Service Application composition root и entrypoints

Вводится:

```text
Service ConfigProvider
→ build_service_application(snapshot)
→ ServiceApplicationContainer
→ start lifecycle
→ serve current interfaces/workers
→ stop lifecycle
```

Container создаётся FastAPI/CLI/worker entrypoint и управляется explicit
lifecycle. Удаляется необходимость import-time `API = Api(...)` как единственного
скрытого owner.

`MessageProcessor` и adapters получают application service через constructor.

ConfigProvider живёт на application boundary. При новой valid revision
composition root применяет только поддерживаемые изменения и выполняет
controlled replacement/reconnect нужных adapters. Ошибка reload не разрушает
работающий container.

Self-hosted/managed hosting, development/production environment и
single-process/multi-process topology являются service deployment metadata и
policy inputs. Они не создают другой AgentRuntime class.

Future Local Agent Application может позднее получить отдельный
`build_local_agent_application(...)`, но его реализация, root config и host
permissions не входят в этот update.

## 14. Compatibility cleanup

После migration callers:

- старый `MCPClient` переименовывается в compatibility facade либо удаляется;
- re-exports сохраняются только на объявленный deprecation period;
- dead wrappers и duplicate models удаляются;
- legacy LLM environment fallbacks удаляются;
- legacy builtin stdio MCP entrypoints и их private env parameters удаляются по
  мере migration;
- `agent.config` становится canonical Service Application filename/example;
- architecture tests фиксируют direction imports;
- документация обновляет canonical owners.

Удаление legacy builtin stdio entrypoints не отменяет поддержку
stdio/executable transport adapter. Его admission определяется application и
hosting profile policy в registry foundation.

## Допустимая параллельность

После characterization baseline параллельно могут выполняться:

- extraction pure models/config и ConfigProvider;
- LLM port;
- event/trace contracts;
- design Service Application composition contracts.

MCP ownership, tool dispatcher и AgentRuntime migration требуют
последовательной интеграции. Finalization extraction выполняется после
стабилизации extension contracts.

`v0.4-mcp-registry-foundation` не начинается до стабилизации generic Dispatcher,
MCP runtime, lifecycle hook и application policy contracts.

## Acceptance criteria

```text
same Service Application scenario before/after refactor
→ equivalent AgentResult/status/can_resume
→ equivalent protocol-valid LLM/tool sequence
→ equivalent durable refs and delivery intent
→ no new infrastructure dependency
```

```text
valid agent.config change
→ new immutable revision
→ new operation uses new revision
→ no mandatory Gateway restart
```

```text
invalid agent.config change
→ reload rejected
→ previous valid snapshot remains active
```

```text
active AgentCycle + configuration file change
→ cycle keeps its original revision
→ next cycle may use the new revision
```

```text
Service Application entrypoint
→ explicit composition/lifecycle
→ no import-time global application startup
```

Дополнительно:

- основной AgentRuntime не импортирует FastAPI/Telegram/SQLAlchemy/Redis/Docker;
- MCP runtime самостоятельно владеет connection state;
- все production tool calls могут быть направлены через `ToolDispatcher`;
- lifecycle hooks не требуют subclass центрального runtime;
- planning/artifacts подключены без нового subclass agent loop;
- configuration loading имеет одного service owner;
- self-hosted single-process Service Application проходит полный regression suite;
- PostgreSQL adapter может быть добавлен за ports без изменения run loop;
- worker entrypoint v0.6 сможет создать тот же AgentRuntime;
- следующий MCP registry update может добавить scopes, admission profiles и
  remote handles без изменения публичного agent loop;
- Future Local Agent Application не реализован, но создание отдельного
  composition root не требует fork/rewrite AgentRuntime.
