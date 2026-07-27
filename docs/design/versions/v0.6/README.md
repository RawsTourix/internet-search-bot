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

```text
DIRECT | SINGLE_TASK | PLANNED_TASK | WORKFLOW
→ isolated TaskRun execution
→ structured task handoff
→ scheduler, safe fork/join and adaptive workflow revisions
```

`CycleInbox` остаётся durable delivery/admission boundary. Поверх него новые
inputs получают intervention semantics относительно active run/workflow.

## Читать

1. [`distributed-runtime.md`](distributed-runtime.md) — полный архитектурный
   overview сервисов, workers, task contexts, scheduler, interventions,
   delivery и observability.
2. [`implementation-plan.md`](implementation-plan.md) — последовательные updates
   и acceptance/preparation gates.

## Именованные updates

| Порядок | Update | Результат |
|---:|---|---|
| 1 | `v0.6.1-job-runtime-foundation` | Redis/arq, durable jobs, leases и retry |
| 2 | `v0.6.2-agent-run-lifecycle` | Durable request/run API и deadlines |
| 3 | `v0.6.3-task-runtime` | Execution modes, TaskRun и bounded context |
| 4 | `v0.6.4-workflow-orchestration` | Workflow scheduler, revisions и fork/join |
| 5 | `v0.6.5-interventions-and-cycle-inbox` | User input во время active run |
| 6 | `v0.6.6-event-bus-and-delivery` | Progress bus и Notification/Delivery boundary |
| 7 | `v0.6.7-background-workers` | Extraction/conversion/summarization/cleanup workers |
| 8 | `v0.6.8-object-storage-and-payload-runtime` | Multi-process payload transport |
| 9 | `v0.6.9-capability-registry-scopes` | MCP scopes и registry revisions |
| 10 | `v0.6.10-service-boundary-stabilization` | Process/service hardening и readiness |

## Зависимости

Сначала прочитайте:

- [`../v0.5/README.md`](../v0.5/README.md);
- [`../v0.4/v0.4-runtime-modularization/README.md`](../v0.4/v0.4-runtime-modularization/README.md);
- [`../v0.4/v0.4-dag-planning.md`](../v0.4/v0.4-dag-planning.md);
- [`../v0.4/v0.4-input-runtime.md`](../v0.4/v0.4-input-runtime.md);
- [`../v0.4/v0.4-file-artifacts-advanced/output-delivery.md`](../v0.4/v0.4-file-artifacts-advanced/output-delivery.md).

Skills используют orchestration boundaries v0.6 и описаны в
[`../v0.7/README.md`](../v0.7/README.md). Isolated execution начинается в
[`../v0.9/README.md`](../v0.9/README.md), а не является скрытым обязательным
условием первого distributed release.