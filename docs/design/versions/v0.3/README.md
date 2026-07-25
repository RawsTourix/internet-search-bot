---
id: design.v0.3.index
version: v0.3
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-07-25
---

# v0.3 — реестр обновлений

`v0.3` — реализованный baseline agent runtime. Основной способ работы с этой
версией — последовательность именованных обновлений.

## Именованные обновления

| Порядок | Обновление | Результат |
|---:|---|---|
| 1 | [`v0.3-agent-protocol-foundation`](v0.3-agent-protocol-foundation.md) | JSON `AgentAction` и dynamic MCP discovery |
| 2 | [`v0.3-agent-memory-runtime`](v0.3-agent-memory-runtime.md) | Разделение dialog memory, LLM context и trace |
| 3 | [`v0.3-cycle-memory`](v0.3-cycle-memory.md) | `pending_cycle`, resume и interrupted cycle |
| 4 | [`v0.3 cycle terminology cleanup`](v0.3-cycle-terminology-cleanup.md) | `task_*` → `cycle_*` |
| 5 | [`v0.3-progress-events`](v0.3-progress-events.md) | Live progress tracking |
| 6 | [`v0.3-progress-events refinements`](v0.3-progress-events-refinements.md) | Локализация, ошибки и единая эмиссия |
| 7 | [`v0.3-mcp-server-manager`](v0.3-mcp-server-manager.md) | Lifecycle-aware MCP runtime |
| 8 | [`v0.3-prompt-optimization`](v0.3-prompt-optimization.md) | Delivery constraints вне system prompt |
| 9 | [`v0.3-final-processing-pipeline`](v0.3-final-processing-pipeline.md) | Grounding и форматирование final answer |
| 10 | [`v0.3-final-processing-progress`](v0.3-final-processing-progress.md) | User-visible final processing progress |

[`v0.3-update-sequence.md`](v0.3-update-sequence.md) фиксирует назначение
хронологической последовательности.

## Общие документы версии

| Документ | Назначение |
|---|---|
| [`memory-model.md`](memory-model.md) | Базовая модель слоёв памяти |
| [`context-budget.md`](context-budget.md) | Context window и compaction ratios |
| [`release-summary.md`](release-summary.md) | Feature freeze и граница перехода к v0.4 |

## Как читать

Для полного анализа v0.3 идите по таблице обновлений сверху вниз. Для локальной
задачи открывайте конкретное обновление и перечисленные в нём общие документы.

Следующая последовательность начинается в
[`../v0.4/README.md`](../v0.4/README.md).
