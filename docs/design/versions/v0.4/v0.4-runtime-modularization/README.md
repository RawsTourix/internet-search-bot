---
id: design.v0.4.runtime-modularization
version: v0.4
update: v0.4-runtime-modularization
spec_status: accepted
implementation_status: planned
last_reviewed: 2026-08-02
---

# v0.4-runtime-modularization

## Назначение

Обновление выполняется после `v0.4-input-runtime` и декомпозирует разросшийся
`src/mcp/mcp_client.py` и наследственную production composition без изменения
принятых runtime contracts и пользовательского поведения.

```text
текущий MCPClient orchestration core
→ compatibility facade
→ AgentRuntime + независимые components/ports
```

После этого update следует
[`v0.4-mcp-registry-foundation`](../v0.4-mcp-registry-foundation/README.md),
который применяет выделенные ports к MCP scopes, trusted metadata и lifecycle
remote resources.

Cross-version граница application profiles:
[`../../../runtime-and-deployment-profiles.md`](../../../runtime-and-deployment-profiles.md).

## Главный результат

- `AgentRuntime` владеет agent loop.
- MCP runtime является одним из tool backends.
- LLM transport, context management, finalization, events и session/cycle state
  имеют отдельные contracts.
- Planning, artifacts и будущие skills подключаются композиционно.
- `ToolDispatcher`, registry и lifecycle hook interfaces готовы к последующему
  MCP registry foundation.
- `ConfigProvider` владеет validated configuration snapshots и их revision.
- Канонический конфигурационный файл Service Application переименовывается из
  `mcp.config` в `agent.config`; старое имя временно остаётся compatibility alias.
- Concrete adapters создаются в composition root.
- Создаётся явный Service Application composition root поверх переиспользуемого
  AgentRuntime.
- Application profile, hosting mode, topology и execution backend не смешиваются
  в один runtime flag.
- v0.5 может заменить persistence backend без переписывания agent loop.
- v0.6 может запускать тот же runtime в worker process или Agent Runtime Service.
- Будущий Local Agent Application сможет использовать тот же runtime через
  отдельный composition root, но не реализуется в этом update.

## Application boundary

`v0.4-runtime-modularization` обслуживает текущий Service Application и выделяет
границы, достаточные для будущей повторной композиции AgentRuntime.

```text
Agent Core contracts
→ AgentRuntime
→ Service Application composition root
```

Service Application может запускаться self-hosted или managed, single-process
или в будущей multi-process topology. Эти варианты не создают разные agent loops.

Future Local Agent Application остаётся отдельным будущим profile:

```text
AgentRuntime
→ separate local composition root
→ local permissions, host execution and local configuration
```

Обновление не обязано создавать local entrypoint, desktop/CLI packaging,
permission broker или local config schema. Оно обязано не привязывать AgentRuntime
к FastAPI, Telegram, server filesystem или Service Application singleton.

Security-critical profile не включается значением обычного service config вида
`mode=local`. Composition root передаёт runtime уже выбранные ports, providers и
policies.

## Configuration boundary

Конфигурацию не загружают отдельно `Gateway`, `MCPClient`, LLM adapter и другие
services. Единственный owner чтения и валидации service operator configuration —
`ConfigProvider`.

```text
agent.config
→ ConfigProvider
→ immutable AgentConfigSnapshot(revision)
→ Service Application composition/runtime consumers
```

При изменении файла provider:

1. читает полный документ;
2. валидирует все root sections;
3. при успехе атомарно публикует новый snapshot/revision;
4. при ошибке оставляет предыдущий snapshot активным.

Новые операции получают актуальную revision. Один активный `AgentCycle` работает
с одним snapshot и не меняет LLM/runtime/tool settings посередине выполнения.
Компонент, которому требуется reconnect или пересоздание adapter, реагирует на
новую revision через собственную boundary, а не требует перезапуска Gateway как
универсального механизма применения конфигурации.

На переходном этапе:

```text
mcp.config
→ compatibility filename

agent.config
→ canonical Service Application filename после modularization
```

Общие submodels LLM/runtime/memory/artifacts/MCP могут позднее использоваться
Future Local Agent Application. Его root configuration и ConfigProvider snapshot
не обязаны совпадать с Service Application и не проектируются этим update.

Per-user service settings/MCP definitions не становятся секциями общего
`agent.config`; их repositories и authorization развиваются в последующих
версиях.

## Граница со следующим update

`v0.4-runtime-modularization` создаёт generic contracts и переносит ownership:

```text
ConfigProvider / AgentConfigSnapshot / ConfigRevision
ToolDefinition / ToolRequest / ToolResult
ToolProvider / ToolRegistry / ToolDispatcher / ToolPolicy
LifecycleHook
RuntimeEventSink / TraceStore
independent MCP runtime
application policy/composition ports
```

Он не обязан реализовывать:

- concrete scopes `builtin|instance|user|session`;
- config-backed scope precedence;
- profile-specific MCP transport admission matrix;
- trusted presentation profiles конкретных bindings;
- side-effect-aware retry registry;
- opaque remote resource registry;
- automatic cleanup integration.

Эти задачи принадлежат `v0.4-mcp-registry-foundation` и не должны раздувать
strangler-refactor дополнительными product semantics.

## Non-goals

В обновление не входят:

- PostgreSQL и Alembic;
- Redis/arq и distributed workers;
- изменение `AgentAction` protocol;
- новый scheduler;
- полный rewrite текущей логики;
- изменение semantics compaction, planning, artifacts, delivery или
  `WAITING_USER`;
- физическое выделение микросервисов;
- реализация конкретного builtin MCP-сервиса;
- реализация Future Local Agent Application;
- local executable packaging, host terminal permission UX или local config root;
- произвольное применение невалидной конфигурации;
- изменение active AgentCycle посередине выполнения из-за reload файла.

## Порядок чтения

1. [`../../../runtime-and-deployment-profiles.md`](../../../runtime-and-deployment-profiles.md)
   — cross-version application/hosting/profile границы.
2. [`implementation-sequence.md`](implementation-sequence.md) — пошаговый
   strangler-refactor, target boundaries и acceptance criteria.
3. [`../v0.4-mcp-registry-foundation/README.md`](../v0.4-mcp-registry-foundation/README.md)
   — следующий update, использующий созданные ports.
4. [`../../../contracts/builtin-mcp-service-contract.md`](../../../contracts/builtin-mcp-service-contract.md)
   — общий integration contract.
5. [`../../../dependency-rules.md`](../../../dependency-rules.md) — допустимое
   направление зависимостей.
6. [`../../../release-gates.md`](../../../release-gates.md) — общий gate v0.4.

## Зависимости

- [`../v0.4-input-runtime.md`](../v0.4-input-runtime.md);
- [`../v0.4-file-artifacts-advanced/README.md`](../v0.4-file-artifacts-advanced/README.md);
- [`../v0.4-dag-planning.md`](../v0.4-dag-planning.md);
- [`../v0.4-cycle-compaction.md`](../v0.4-cycle-compaction.md).

## Migration strategy

Используется strangler-подход:

```text
старый public entrypoint
→ делегирует extracted component
→ characterization tests подтверждают parity
→ ownership state переносится из facade
→ compatibility method удаляется только после migration callers
```

Для configuration migration применяется тот же принцип:

```text
старый mcp.config loader
→ compatibility adapter к ConfigProvider
→ agent.config становится canonical Service Application config
→ старый filename удаляется после migration entrypoints
```

Текущий import-time `API = Api(...)` заменяется явным Service Application
composition/lifecycle. Это не создаёт Future Local Agent entrypoint автоматически.

Большой одномоментный rewrite `mcp_client.py` запрещён этой спецификацией.

## Release gate

Обновление завершено, когда основной runtime не зависит напрямую от client
adapters и concrete infrastructure, полный v0.4 regression suite подтверждает
эквивалентность поведения, а `v0.4-mcp-registry-foundation` может быть реализован
поверх новых ports без возврата ответственности в compatibility facade.

Дополнительно:

- application использует один validated service configuration snapshot source;
- invalid reload не заменяет последнюю рабочую конфигурацию;
- изменение поддерживаемых reloadable settings применяется без обязательного
  restart Gateway;
- один AgentCycle сохраняет исходную configuration revision до terminal state;
- Service Application создаёт AgentRuntime через явный composition root;
- AgentRuntime не импортирует Service Application, Telegram/Web или будущие
  local-agent adapters;
- hosting/topology differences выбирают adapters, а не ветвят agent loop;
- следующий MCP registry update может применять profile-specific transport
  admission без изменения AgentRuntime API;
- архитектура не обещает реализованный Local Agent, но не требует fork/rewrite
  ядра для его будущего composition root.
