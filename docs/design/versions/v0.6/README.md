---
id: design.v0.6.index
version: v0.6
spec_status: draft
implementation_status: planned
last_reviewed: 2026-07-27
---

# v0.6 — Distributed runtime

Версия вводит workers, durable queues, независимый `AgentRun` lifecycle,
execution-mode routing, task-scoped contexts, workflow orchestration и разделение
execution, delivery и result retrieval.

Предварительная runtime-модель развивает текущий `AgentCycle`, а не заменяет его
одним переписыванием:

```text
DIRECT | SINGLE_TASK | PLANNED_TASK | WORKFLOW
→ isolated TaskRun execution
→ structured task handoff
→ scheduler, safe fork/join and adaptive workflow revisions
```

`CycleInbox` остаётся durable delivery/admission boundary. Поверх него `v0.6`
может классифицировать новые пользовательские inputs как intervention текущего
run: применить в safe checkpoint, создать отдельную task, изменить workflow
revision или запросить cancellation.

## Читать

- [`distributed-runtime.md`](distributed-runtime.md) — полная спецификация
  сервисов, workers, execution modes, task context runtime, scheduler,
  interventions, outbox и observability.

## Зависимости

Сначала прочитайте:

- [`../v0.5/README.md`](../v0.5/README.md);
- [`../v0.4/v0.4-dag-planning.md`](../v0.4/v0.4-dag-planning.md);
- [`../v0.4/v0.4-input-runtime.md`](../v0.4/v0.4-input-runtime.md);
- [`../v0.4/v0.4-file-artifacts-advanced/output-delivery.md`](../v0.4/v0.4-file-artifacts-advanced/output-delivery.md).

Skills Library использует orchestration boundaries v0.6 и описана в
[`../v0.7/README.md`](../v0.7/README.md).