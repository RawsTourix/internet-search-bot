---
id: design.v0.6.index
version: v0.6
spec_status: draft
implementation_status: planned
last_reviewed: 2026-08-05
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

`CycleInbox` сохраняет durable delivery/admission boundary из
[`v0.4-input-runtime`](../v0.4/v0.4-input-runtime/README.md). Поверх неё новые
inputs получают intervention semantics относительно active run/workflow.

Линейные `CycleContextRevision` v0.4 становятся основой task-local revisions и
controlled merge, но v0.6 не переносит полные sibling LLM histories в общую
ветку. Scheduler объединяет structured results, exact refs, constraints и
provenance.

Локальная scope-модель и config-backed MCP registry foundation создаются в
[`v0.4-mcp-registry-foundation`](../v0.4/v0.4-mcp-registry-foundation/README.md).
`v0.6` не вводит scopes заново: он делает registry durable, multi-process и
ownership-ready.

Application/hosting profiles определены в
[`../../runtime-and-deployment-profiles.md`](../../runtime-and-deployment-profiles.md).
`v0.6` развивает Service Application от single-process к multi-process/distributed
topology. Agent Runtime worker/service использует тот же переиспользуемый
`AgentRuntime`, а не отдельную реализацию agent loop.

Single-process self-hosted development остаётся Service Application и не
считается Future Local Agent Application.

## Читать

1. [`../../runtime-and-deployment-profiles.md`](../../runtime-and-deployment-profiles.md)
   — application profiles, hosting modes и execution boundaries.
2. [`distributed-runtime.md`](distributed-runtime.md) — полный архитектурный
   overview сервисов, workers, task contexts, scheduler, interventions,
   delivery и observability.
3. [`implementation-plan.md`](implementation-plan.md) — последовательные updates
   и acceptance/preparation gates.
4. [`distributed-capability-registry.md`](distributed-capability-registry.md) —
   каноническая спецификация `v0.6.9`; явно supersedes старый пункт 127.7 overview.
5. [`../../contracts/builtin-mcp-service-contract.md`](../../contracts/builtin-mcp-service-contract.md)
   — общий контракт builtin MCP-сервисов.

## Именованные updates

| Порядок | Update | Результат |
|---:|---|---|
| 1 | `v0.6.1-job-runtime-foundation` | Redis/arq, durable jobs, leases и retry |
| 2 | `v0.6.2-agent-run-lifecycle` | Durable request/run API и deadlines |
| 3 | `v0.6.3-task-runtime` | Execution modes, TaskRun и bounded context |
| 4 | `v0.6.4-workflow-orchestration` | Workflow scheduler, revisions и fork/join |
| 5 | `v0.6.5-interventions-and-cycle-inbox` | User input во время active run поверх v0.4 admission/watermarks |
| 6 | `v0.6.6-event-bus-and-delivery` | Progress/emission bus и Notification/Delivery boundary |
| 7 | `v0.6.7-background-workers` | Extraction/conversion/summarization/cleanup workers |
| 8 | `v0.6.8-object-storage-and-payload-runtime` | Multi-process payload transport |
| 9 | [`v0.6.9-distributed-capability-registry`](distributed-capability-registry.md) | Durable MCP registry, worker-visible revisions и ownership-ready scopes |
| 10 | `v0.6.10-service-boundary-stabilization` | Process/service hardening и readiness |

## Наследуемые контракты input runtime

v0.6 расширяет, но не заменяет:

```text
CommittedInputBatch → InputAdmissionRecord
ordered CycleInbox delivery
session/cycle sequence and watermarks
idempotent application
control priority/generation fencing
context revision identity
AgentEmission identity
finalization barrier
```

Distributed additions:

```text
worker lease/fencing token
agent_run_id/workflow_id/task_run_id
UserIntervention classification
workflow revision
parallel task branch
controlled structured merge
```

Redis/event bus является signal/acceleration layer; PostgreSQL остаётся source of
truth для admission, interventions, task state, results и terminal commit.

## Зависимости

Сначала прочитайте:

- [`../../runtime-and-deployment-profiles.md`](../../runtime-and-deployment-profiles.md);
- [`../v0.5/README.md`](../v0.5/README.md);
- [`../v0.4/v0.4-runtime-modularization/README.md`](../v0.4/v0.4-runtime-modularization/README.md);
- [`../v0.4/v0.4-mcp-registry-foundation/README.md`](../v0.4/v0.4-mcp-registry-foundation/README.md);
- [`../v0.4/v0.4-dag-planning.md`](../v0.4/v0.4-dag-planning.md);
- [`../v0.4/v0.4-input-runtime/README.md`](../v0.4/v0.4-input-runtime/README.md);
- [`../v0.4/v0.4-input-runtime/domain-models-and-state-machines.md`](../v0.4/v0.4-input-runtime/domain-models-and-state-machines.md);
- [`../v0.4/v0.4-input-runtime/checkpoints-and-context-revisions.md`](../v0.4/v0.4-input-runtime/checkpoints-and-context-revisions.md);
- [`../v0.4/v0.4-file-artifacts-advanced/output-delivery.md`](../v0.4/v0.4-file-artifacts-advanced/output-delivery.md).

Skills используют orchestration boundaries v0.6 и описаны в
[`../v0.7/README.md`](../v0.7/README.md). Isolated execution начинается в
[`../v0.9/README.md`](../v0.9/README.md), а не является скрытым обязательным
условием первого distributed release.

Future Local Agent Application не входит в scope v0.6. Его возможное появление
позднее переиспользует AgentRuntime и общие ports через отдельный composition
root.
