---
id: design.v0.4.input-runtime
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-06
---

# v0.4-input-runtime

## Статус реализации

`IR-1` и `IR-2` реализованы и подтверждены CI. Этапы `IR-3`—`IR-10` остаются
planned.

Evidence для завершённого filesystem foundation:

- code HEAD: `c7ed199deb0dfe042cff6055989a76e371537755`;
- `Validate Input Runtime` run #67 — success;
- `Validate v0.4 file artifacts PR` run #494 — success;
- targeted input-runtime и общий configuration audit: `164 passed`.

IR-2 добавил filesystem implementations repository ports, атомарные записи,
bounded reference-counted coordination, authoritative session-local
pre-allocation repair, coordinated sequence allocation, claim fencing/recovery,
authoritative `payload_size_bytes`, generation cancellation и root-global
identity fencing с порядком `root identity → session`.

Global create/append/prepare paths теперь используют crash-recoverable protocol
`lookup/recovery → durable record write → index/cycle-authority writes`. Потерянные
или dangling indexes перед competing create сверяются редким exact-identity scan:
единственный authoritative record восстанавливает все relations, отсутствие
record очищает незавершённую reservation, а неоднозначность возвращает managed
consistency error. Crash после durable record и до любого или части indexes не
создаёт второй record после restart.

Production runtime behaviour пока не изменён: filesystem repositories не
подключены к `Api`, committed batches ещё не проходят новый admission service,
safe checkpoints, `/stop`/`/continue`, emissions и finalization barrier не
подключены к рабочему agent loop.

## Назначение

Обновление превращает текущий последовательный `AgentCycle` в управляемый
двусторонний runtime активной задачи.

Пользователь сможет:

- отправлять дополнительные сообщения и файлы во время работы агента;
- видеть, какие дополнения приняты, ожидают применения и уже учтены;
- приостанавливать работу командой `/stop` без удаления состояния;
- накопить уточнения во время паузы и продолжить тот же цикл через `/continue`.

Агент сможет:

- применять новый committed input только в protocol-safe checkpoints;
- отправлять устойчивые промежуточные смысловые сообщения без завершения задачи;
- подавлять устаревший `DONE` или `WAITING_USER`, если уже принят новый input;
- сохранять согласованное resumable state после controlled pause или restart.

```text
CommittedInputBatch
→ InputAdmission
→ один active AgentCycle либо CycleInbox
→ safe checkpoint
→ новая context revision
→ дальнейший LLM/tool step
```

В `v0.4` runtime остаётся single-process и линейным: один active `AgentCycle` на
session, без scheduler, параллельных LLM-веток и fork/join. Контракты при этом
проектируются так, чтобы filesystem repositories в `v0.5` заменялись PostgreSQL
implementations, а `v0.6` мог добавить `AgentRun`, `TaskRun`, interventions,
workflow revisions и scheduler поверх той же admission boundary.

## Текущий baseline

В ветке `feature` уже существуют важные переходные foundations:

- immutable `CommittedInputBatch` и durable ingress;
- `SessionExecutionCoordinator` с in-process FIFO lane, run lease и generation;
- один active cycle на session на уровне текущего API orchestration;
- continuation suspended `WAITING_USER`/`interrupted` cycle новым committed batch;
- `ProgressEvent`, durable `OutputBatch`, client capabilities и delivery receipts;
- `/reset`, который инвалидирует queued work и ждёт runtime boundary перед
  очисткой памяти.

Эти foundations не являются полным input runtime:

- очередь `SessionExecutionCoordinator` живёт только в памяти;
- committed batch обычно запускает отдельную run operation, а не дополняет уже
  работающий cycle;
- `stop_requested` не является durable control state;
- active-cycle snapshot не хранит applied input watermarks;
- intermediate agent message пока не является отдельной durable interaction;
- finalization не имеет общего input/control barrier.

Обновление должно мигрировать существующее поведение постепенно, без большого
rewrite `src/mcp/mcp_client.py`. Полная декомпозиция ownership выполняется в
следующем update
[`v0.4-runtime-modularization`](../v0.4-runtime-modularization/README.md).

## Целевая архитектура

```text
Ingress
→ InputBatchDraft
→ CommittedInputBatch

Input Runtime
→ InputAdmissionService
→ CycleInbox / SessionControlInbox
→ SafeCheckpointService
→ CycleContextRevision
→ FinalizationCoordinator

Agent Runtime
→ LLM / tools / planning / memory / artifacts
→ canonical ProgressEvent
→ durable AgentEmission

Interaction / Delivery
→ progress projection
→ intermediate message delivery
→ question delivery
→ final OutputBatch delivery
```

### Короткая coordination boundary

Долгий execution lease и короткая session coordination boundary разделяются.

Долгий lease гарантирует только один выполняющийся AgentCycle на session.
Короткая boundary используется для:

- назначения session/cycle sequence;
- admission committed batch;
- записи `/stop`, `/continue`, `/reset`;
- final input/control recheck;
- terminal commit.

Она не удерживается во время LLM request, tool call, compaction, чтения больших
payloads или client delivery.

## Основные инварианты

1. Ingress заканчивается на immutable `CommittedInputBatch`; runtime не принимает
   transport fragments и незавершённые drafts.
2. Один `input_batch_id` допускается в active runtime не более одного раза.
3. Один AgentCycle получает дополнения в строгом порядке session/cycle sequence.
4. Новый input не вставляется между `assistant.tool_calls` и matching
   `role=tool` results.
5. Один LLM/tool atomic block использует неизменяемую context revision.
6. `/stop` сохраняет сообщения, рабочую память, plan state, artifacts и results.
7. `paused_by_user` не возобновляется обычным input; требуется `/continue`.
8. `waiting_user` возобновляется новым committed input автоматически.
9. Intermediate agent message является durable emission, а не transient progress.
10. Финальный ответ не фиксируется, пока accepted input/control watermarks не
    согласованы с applied watermarks.
11. Execution outcome, emission/output persistence и client delivery являются
    разными состояниями.
12. Filesystem backend может иметь crash windows, но recovery всегда
    идемпотентен и не теряет committed input.
13. Неоднозначный внешний side effect после crash не повторяется автоматически.
14. Domain IDs не зависят от Telegram/Web/CLI message IDs.
15. В `v0.4` context revisions линейны, но identity допускает будущие multiple
    parents без реализации merge semantics.

## Состав обновления

| Документ | Ответственность |
|---|---|
| [`domain-models-and-state-machines.md`](domain-models-and-state-machines.md) | Domain entities, IDs, watermarks и переходы состояний |
| [`admission-and-cycle-inbox.md`](admission-and-cycle-inbox.md) | Commit-to-admission protocol, FIFO, claims, backpressure и queue drain |
| [`checkpoints-and-context-revisions.md`](checkpoints-and-context-revisions.md) | Safe checkpoints, input application, context revisions, planning/compaction/artifact integration |
| [`control-plane-pause-resume.md`](control-plane-pause-resume.md) | `/stop`, `/continue`, `/reset`, command priority и pause semantics |
| [`agent-emissions-and-client-projections.md`](agent-emissions-and-client-projections.md) | Progress, intermediate messages, questions, final answers и client-neutral delivery |
| [`finalization-and-recovery.md`](finalization-and-recovery.md) | Terminal barrier, crash windows, startup reconciliation и resumability |
| [`implementation-sequence.md`](implementation-sequence.md) | Пошаговое обновление текущего кода и обязательные tests по этапам |
| [`contracts-and-acceptance.md`](contracts-and-acceptance.md) | Configuration, observability, end-to-end contracts и release gates |
| [`deferred-telegram-history-revision.md`](deferred-telegram-history-revision.md) | Отложенная Telegram-specific history rewind по edited message |

## Scope v0.4

В update входят:

- durable admission committed batches;
- filesystem repositories и repository ports;
- durable active-cycle snapshot и input watermarks;
- `CycleInbox` с FIFO, leases и replay-safe application;
- safe checkpoint hooks в существующем agent loop;
- linear `CycleContextRevision`;
- `/stop`, `/continue`, расширенный `/reset`;
- `paused_by_user`, `pause_requested` и resumable `interrupted`;
- durable intermediate `AgentEmission`;
- client-neutral addendum/emission projections;
- finalization barrier;
- startup recovery и diagnostics;
- unit, race, randomized, restart, synthetic и live acceptance.

## Non-goals

В update не входят:

- PostgreSQL, SQLAlchemy и Alembic;
- Redis/arq, distributed locks и workers;
- `AgentRun`/`TaskRun` runtime;
- workflow scheduler и parallel task execution;
- semantic intervention routing;
- fork/join или merge конфликтов context branches;
- автоматическая классификация дополнения как новой task;
- полноценный Web chat/branch UI;
- автоматическая синхронизация одной session между Telegram и first-party clients;
- Telegram history rewind после edited message;
- насильственная отмена уже подтверждённого внешнего side effect;
- большой rewrite/разделение `MCPClient`;
- изменение базового `AgentAction` protocol без необходимости.

## Подготовка v0.5

Все authoritative stores оформляются как ports:

```text
InputAdmissionRepository
CycleInboxRepository
SessionControlRepository
ActiveCycleSnapshotRepository
ContextRevisionRepository
AgentEmissionRepository
FinalizationRepository
```

Filesystem implementations принадлежат `v0.4`. PostgreSQL implementations
`v0.5` сохраняют те же IDs, transitions, idempotency keys и ownership relations,
но заменяют короткую filesystem coordination boundary транзакцией/row locking.

## Подготовка v0.6

`CycleInbox` остаётся durable delivery/admission boundary. Поверх существующих
records `v0.6` сможет добавить:

```text
agent_run_id
workflow_id
workflow_revision_id
task_run_id
intervention_id
```

Новый `UserIntervention` будет решать, применять input непосредственно, создать
параллельную task, пересмотреть workflow, отложить или отменить устаревшую
работу. Scheduler не меняет факта, что каждый committed batch сначала durable и
идемпотентно admitted.

## Зависимости

- [`../v0.4-batch-workflows/README.md`](../v0.4-batch-workflows/README.md);
- [`../v0.4-file-artifacts-advanced/README.md`](../v0.4-file-artifacts-advanced/README.md);
- [`../v0.4-cycle-compaction.md`](../v0.4-cycle-compaction.md);
- [`../v0.4-dag-planning.md`](../v0.4-dag-planning.md);
- [`../../../runtime-and-deployment-profiles.md`](../../../runtime-and-deployment-profiles.md).

## Release boundary

Update считается завершённым только после:

- реализации всех mandatory stages из
  [`implementation-sequence.md`](implementation-sequence.md);
- прохождения contracts из
  [`contracts-and-acceptance.md`](contracts-and-acceptance.md);
- live проверки Telegram `/stop`, `/continue`, addenda и intermediate emissions;
- подтверждения отсутствия regression в batch/artifact/output flows;
- актуализации v0.4/current/roadmap documentation.
