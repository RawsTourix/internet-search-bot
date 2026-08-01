---
id: design.v0.4.overview
version: v0.4
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-01
---

# v0.4 — Agent Workspace и модульный runtime foundation

## 1. Главная идея v0.4

`v0.4` превращает текущий agent runtime в рабочее пространство, способное
безопасно выполнять длинные, составные, файловые и tool-heavy задачи.

Главная архитектурная формула:

```text
полные данные, файлы и история выполнения
→ внешнее storage/workspace

видимый LLM-контекст
→ только актуальная рабочая информация,
   компактные представления и устойчивые ссылки

разросшийся orchestration core
→ AgentRuntime + ports + ToolDispatcher

MCP integrations
→ scoped registry + trusted metadata + stable service contract
```

`v0.4` работает без обязательных PostgreSQL, Redis и workers. Новые компоненты
проектируются через интерфейсы, совместимые с последующей миграцией.

## 2. Именованные updates

```text
v0.4-storage-foundation
v0.4-result-compaction
v0.4-cycle-compaction
v0.4-dag-planning
v0.4-file-artifacts
v0.4-file-artifacts-advanced
v0.4-batch-workflows
v0.4-input-runtime
v0.4-runtime-modularization
v0.4-mcp-registry-foundation
```

Канонический порядок и implementation status находятся в
[`README.md`](README.md).

## 3. Граница v0.4

В `v0.4` входят:

- filesystem/local storage foundation;
- compaction больших tool results и старой части cycle;
- необязательный runtime-owned DAG-план без scheduler;
- получение, чтение, изменение, версионирование и отправка файлов;
- semantic input/output и durable artifact delivery;
- AUTO/EXPLICIT batch workflows;
- `InputBatch` и `CycleInbox`;
- safe checkpoints, control inbox и per-session coordination;
- progress/trace events;
- декомпозиция `MCPClient` в `AgentRuntime` и независимые ports;
- generic `ToolDispatcher` и extension hooks;
- local/config-backed MCP registry scopes;
- trusted presentation/retry/outcome metadata;
- opaque remote-resource lifecycle ownership.

В `v0.4` не входят:

- PostgreSQL и pgvector;
- embeddings и semantic RAG;
- постоянный chunk index;
- Redis/arq и distributed workers;
- automatic workflow scheduler;
- durable `AgentRun`/`TaskRun`;
- distributed capability registry;
- полноценная account authorization;
- реализация конкретного builtin MCP-сервиса внутри agent repository;
- полный execution sandbox.

Главные инварианты:

```text
Raw content не должен бесконтрольно жить
в messages_for_llm или дублироваться в cycle archive.

MCP transport connection
≠ remote resource lifecycle.
```

## 4. Разделение ответственности

Целевая логическая структура:

```text
storage/
memory/
artifacts/
planning/
ingress/
interaction/
runtime/
llm/
tools/
mcp/
finalization/
delivery/
```

- `AgentRuntime` владеет agent loop.
- `ToolDispatcher` владеет invocation policy, normalized outcomes и canonical
  progress metadata.
- MCP runtime владеет connections, generations и transport recovery.
- MCP registry владеет definitions, scopes, snapshots и bindings.
- Planning/artifacts/input/delivery не реализуются внутри central runtime class.
- Client adapters не владеют application/session state.
- Concrete infrastructure создаётся composition root.

Внешний builtin MCP-сервис остаётся отдельной integration boundary. Агент
документирует только registry, policy, orchestration и public contract; внутренняя
реализация сервиса принадлежит его собственному репозиторию.

Общий contract:
[`../../contracts/builtin-mcp-service-contract.md`](../../contracts/builtin-mcp-service-contract.md).

## 5. Переход к следующим версиям

```text
v0.4 local/filesystem contracts
→ v0.5 PostgreSQL/RAG adapters
→ v0.6 durable runs/workers/distributed registry
```

v0.5 заменяет storage/repository implementations, а не agent loop. v0.6
распределяет уже стабилизированные contracts между processes/workers. Scope model
MCP registry и remote-resource semantics не проектируются заново.
