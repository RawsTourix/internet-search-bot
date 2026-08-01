---
id: design.v0.4.runtime-modularization
version: v0.4
update: v0.4-runtime-modularization
spec_status: accepted
implementation_status: planned
last_reviewed: 2026-08-01
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

## Главный результат

- `AgentRuntime` владеет agent loop.
- MCP runtime является одним из tool backends.
- LLM transport, context management, finalization, events и session/cycle state
  имеют отдельные contracts.
- Planning, artifacts и будущие skills подключаются композиционно.
- `ToolDispatcher`, registry и lifecycle hook interfaces готовы к последующему
  MCP registry foundation.
- `ConfigProvider` владеет validated configuration snapshots и их revision.
- Канонический конфигурационный файл переименовывается из `mcp.config` в
  `agent.config`; старое имя временно остаётся compatibility alias.
- Concrete adapters создаются в composition root.
- v0.5 может заменить persistence backend без переписывания agent loop.
- v0.6 может запускать тот же runtime в worker process.

## Configuration boundary

Конфигурацию не загружают отдельно `Gateway`, `MCPClient`, LLM adapter и другие
services. Единственный owner чтения и валидации — `ConfigProvider`.

```text
agent.config
→ ConfigProvider
→ immutable AgentConfigSnapshot(revision)
→ composition/runtime consumers
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
→ canonical filename после modularization
```

## Граница со следующим update

`v0.4-runtime-modularization` создаёт generic contracts и переносит ownership:

```text
ConfigProvider / AgentConfigSnapshot / ConfigRevision
ToolDefinition / ToolRequest / ToolResult
ToolProvider / ToolRegistry / ToolDispatcher / ToolPolicy
LifecycleHook
RuntimeEventSink / TraceStore
independent MCP runtime
```

Он не обязан реализовывать:

- concrete scopes `builtin|instance|user|session`;
- config-backed scope precedence;
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
- произвольное применение невалидной конфигурации;
- изменение active AgentCycle посередине выполнения из-за reload файла.

## Порядок чтения

1. [`implementation-sequence.md`](implementation-sequence.md) — пошаговый
   strangler-refactor, target boundaries и acceptance criteria.
2. [`../v0.4-mcp-registry-foundation/README.md`](../v0.4-mcp-registry-foundation/README.md)
   — следующий update, использующий созданные ports.
3. [`../../../contracts/builtin-mcp-service-contract.md`](../../../contracts/builtin-mcp-service-contract.md)
   — общий integration contract.
4. [`../../../dependency-rules.md`](../../../dependency-rules.md) — допустимое
   направление зависимостей.
5. [`../../../release-gates.md`](../../../release-gates.md) — общий gate v0.4.

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
→ agent.config становится canonical
→ старый filename удаляется после migration entrypoints
```

Большой одномоментный rewrite `mcp_client.py` запрещён этой спецификацией.

## Release gate

Обновление завершено, когда основной runtime не зависит напрямую от client
adapters и concrete infrastructure, полный v0.4 regression suite подтверждает
эквивалентность поведения, а `v0.4-mcp-registry-foundation` может быть реализован
поверх новых ports без возврата ответственности в compatibility facade.

Дополнительно:

- application использует один validated configuration snapshot source;
- invalid reload не заменяет последнюю рабочую конфигурацию;
- изменение поддерживаемых reloadable settings применяется без обязательного
  restart Gateway;
- один AgentCycle сохраняет исходную configuration revision до terminal state.
