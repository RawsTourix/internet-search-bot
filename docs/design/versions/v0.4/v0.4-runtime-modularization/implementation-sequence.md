---
id: design.v0.4.runtime-modularization.sequence
version: v0.4
update: v0.4-runtime-modularization
spec_status: accepted
implementation_status: planned
last_reviewed: 2026-08-01
---

# Последовательность модульного рефакторинга runtime

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
```

Для каждой группы определяются current owner, mutable state, входы, выходы,
errors и side effects.

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
- `CycleInbox` safe checkpoints после реализации input runtime.

Tests проверяют observable contracts, а не private method layout.

## 3. Модели и configuration

Из центрального файла выносятся pure models и validated settings:

```text
llm/models.py
llm/config.py
mcp/models.py
mcp/config.py
sessions/models.py
runtime/models.py
finalization/models.py
```

Условия:

- отсутствие runtime side effects при import;
- одна модель не объявляется в нескольких packages;
- compatibility imports временно допускаются через re-export;
- configuration loading отделяется от concrete service construction.

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
```

Manager tools, MCP tools, artifact tools и planning tools регистрируются как
providers. Dispatcher отвечает за единый invocation envelope, trust marking,
progress metadata, result handling и error normalization.

На этом этапе достаточно generic contracts и compatibility defaults. Concrete
MCP scopes, approved binding profiles, side-effect-aware retry registry и remote
resource integration реализуются следующим update
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

Он координирует lifecycle, но не реализует concrete transport/persistence.

## 13. Composition root и entrypoints

Вводится:

```text
build_application(settings)
→ ApplicationContainer
```

Container создаётся FastAPI/CLI/worker entrypoint и управляется explicit
lifecycle. Удаляется необходимость import-time `API = Api(...)` как единственного
скрытого owner.

`MessageProcessor` и adapters получают application service через constructor.

## 14. Compatibility cleanup

После migration callers:

- старый `MCPClient` переименовывается в compatibility facade либо удаляется;
- re-exports сохраняются только на объявленный deprecation period;
- dead wrappers и duplicate models удаляются;
- architecture tests фиксируют direction imports;
- документация обновляет canonical owners.

## Допустимая параллельность

После characterization baseline параллельно могут выполняться:

- extraction pure models/config;
- LLM port;
- event/trace contracts.

MCP ownership, tool dispatcher и AgentRuntime migration требуют
последовательной интеграции. Finalization extraction выполняется после
стабилизации extension contracts.

`v0.4-mcp-registry-foundation` не начинается до стабилизации generic Dispatcher,
MCP runtime и lifecycle hook contracts.

## Acceptance criteria

```text
same scenario before/after refactor
→ equivalent AgentResult/status/can_resume
→ equivalent protocol-valid LLM/tool sequence
→ equivalent durable refs and delivery intent
→ no new infrastructure dependency
```

Дополнительно:

- основной AgentRuntime не импортирует FastAPI/Telegram/SQLAlchemy/Redis/Docker;
- MCP runtime самостоятельно владеет connection state;
- все production tool calls могут быть направлены через `ToolDispatcher`;
- lifecycle hooks не требуют subclass центрального runtime;
- planning/artifacts подключены без нового subclass agent loop;
- no import-time application startup;
- filesystem/local mode проходит полный regression suite;
- PostgreSQL adapter может быть добавлен за ports без изменения run loop;
- worker entrypoint v0.6 сможет создать тот же ApplicationContainer;
- следующий MCP registry update может добавить scopes, profiles и remote handles
  без изменения публичного agent loop.
