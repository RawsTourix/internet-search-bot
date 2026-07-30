---
id: design.current
version: cross-version
spec_status: accepted
implementation_status: mixed
last_reviewed: 2026-07-30
---

# Текущий архитектурный baseline

Этот файл определяет, какие версии следует применять при анализе текущего
проекта. Release-решение дополнительно проверяется по коду, tests и live gates.

## Реализованный baseline

`v0.3` остаётся реализованной основой:

- JSON-протокол `AgentAction`;
- dialog memory, LLM context и cycle trace;
- `pending_cycle` и resumable cycle;
- progress events;
- lifecycle-aware MCP Server Manager;
- final processing pipeline.

Канонический индекс: [`versions/v0.3/README.md`](versions/v0.3/README.md).

## Активное развитие v0.4

`v0.4` является принятой архитектурой agent workspace.

Реализованы либо доведены до устойчивого filesystem runtime:

- `v0.4-storage-foundation`;
- `v0.4-result-compaction`;
- `v0.4-cycle-compaction`;
- `v0.4-dag-planning`;
- `v0.4-file-artifacts`;
- кодовые slices `v0.4-file-artifacts-advanced`;
- `v0.4-batch-workflows`.

`v0.4-file-artifacts-advanced` сохраняет partial acceptance status только из-за
оставшихся live gates AF-25/AF-26. Durable reservation, recovery, forwarded
sequencing, native Telegram document albums, delivery receipts и commit/run
boundary реализованы и покрыты regression suites.

## v0.4-batch-workflows

Кодовый update реализован. Он завершает client-facing workflow до admission в
AgentCycle:

- AUTO text-only input запускается без artificial delay;
- files-first открывает durable draft;
- explicit `/collect → /send | /cancel` поддерживает text-first/files-first/mixed;
- `/batch` и `/done` отсутствуют как дублирующие aliases;
- shared HTTP control plane проверяет exact client-instance authority;
- persisted canonical grouping mode — `explicit_collection`;
- rollout-era `immediate_text` drafts/indexes migration-ятся при reconcile;
- presentation relocation использует generations и
  `create → durable bind → supersede → best-effort delete`;
- failed deletion оставляет старое сообщение архивным и невritable;
- `artifact_list(scope=current|session|workspace)` активирует exact historical
  versions в bounded current-cycle manifest;
- historical read/search/delivery разрешены после explicit activation;
- output grouping и native Telegram multipart delivery сохраняют порядок.

Thematic CI:

```text
compile: success
artifact suite: 224 tests, OK
storage suite: 41 tests, OK
plans suite: 45 tests, OK
planning suite: 19 tests, OK
API suite: 1 test, OK
```

Перед переводом PR из draft остаются:

- новый полный Windows suite;
- live Telegram `/collect`, `/send`, `/cancel`;
- live relocation status message;
- финальный acceptance audit.

Канонический документ:
[`versions/v0.4/v0.4-batch-workflows/README.md`](versions/v0.4/v0.4-batch-workflows/README.md).

## Следующий функциональный этап

```text
v0.4-input-runtime
```

Он добавит `CycleInbox`, safe checkpoints, control inbox и finalization races для
сообщений, поступающих уже во время active AgentCycle. Эти обязанности не входят в
`v0.4-batch-workflows`.

После него запланирован:

```text
v0.4-runtime-modularization
```

Он декомпозирует orchestration core без изменения принятых контрактов и готовит
ports/repositories/composition root для v0.5 и v0.6.

Канонический индекс v0.4: [`versions/v0.4/README.md`](versions/v0.4/README.md).

## Будущие версии

| Версия | Роль |
|---|---|
| `v0.5` | PostgreSQL, lazy indexing, embeddings и RAG |
| `v0.6` | AgentRun/TaskRun, workers, queues и workflow orchestration |
| `v0.7` | Skills и extension platform |
| `v0.8` | Identity, authorization и multi-user workspace |
| `v0.9` | Single-node isolated execution через sandbox backend |
| `v0.10` | Distributed execution plane и runner fleet |

Будущая версия не должна использоваться как описание текущего поведения без code,
test или migration evidence.

## Правило анализа

1. используйте v0.3 как реализованный baseline;
2. применяйте отмеченные реализованные updates v0.4;
3. учитывайте AF-24–AF-26 по их code/live status;
4. учитывайте `v0.4-batch-workflows` как code-complete, acceptance-pending;
5. не приписывайте active-cycle additions до `v0.4-input-runtime`;
6. проверяйте затронутый код и tests для точного implementation status;
7. используйте v0.5–v0.10 только как будущие ограничения;
8. не смешивайте `AgentCycle`, будущий `AgentRun` и `TaskRun`.
