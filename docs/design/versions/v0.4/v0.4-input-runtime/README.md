---
id: design.v0.4.input-runtime
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-07
---

# v0.4-input-runtime

## Статус реализации

`IR-1`, `IR-2`, `IR-3`, `IR-4` и `IR-5` реализованы и подтверждены CI. Этапы
`IR-6`—`IR-10` остаются planned, поэтому общий update сохраняет статус
`partial`.

Финальный IR-5 code/test evidence:

- итоговый code/test HEAD:
  `85c52d4b60a60786bdb10732eb0a52893a422eee`;
- `Validate Input Runtime` #173 — success, compile success, `278 passed`,
  `0 failed`;
- `Validate v0.4 file artifacts PR` #547 — success.

IR-5 добавил transport-neutral durable control service и сделал
`SessionInputRuntimeState + SessionControlCommand + ActiveCycleSnapshot`
semantic authority для `/stop`, `/continue` и `/reset`. Control sequence
выделяется под короткой session coordination boundary, stable idempotency не
создаёт второй logical command, а `pending_control_sequence` и
`applied_control_sequence` работают как monotonic contiguous watermarks.

Checkpoint reducer фиксирует pending-control watermark на входе и применяет
controls до ordinary input. Cooperative `/stop` не force-cancel LLM/tool await:
текущий bounded LLM attempt или полный assistant multi-tool block завершается
protocol-valid, после чего snapshot фиксируется как `paused_by_user` без потери
messages, context lineage, plan/artifact/result refs. Input во время
`pause_requested/paused_by_user` получает `QUEUE_PAUSED`, durable FIFO admission
и не будит runner.

`/continue` возобновляет **тот же** cycle. Continue boundary сохраняет accepted
input target; все paused additions, accepted до этой boundary, drain-ятся bounded
chunks через `CP-RESUME` до первого meaningful post-resume LLM. Более поздний
input остаётся следующему running checkpoint. Continue без additions не создаёт
фиктивный input update или лишнюю context revision. `WAITING_USER` без нового
ответа возвращает `still_waiting_for_input`.

`/reset` повышает durable session generation ровно один раз на logical reset,
fences old-generation writer/wake, отменяет старые queued admissions/inbox/
pending controls/snapshot/finalization/emission records и очищает mutable session
memory только после safe in-process execution lease boundary. Coordinator лишь
синхронизируется с уже durable generation и не является authority.

Telegram `/stop` и `/continue` — отдельные high-priority runtime handlers. Они
используют тот же conversation/thread resolution, stable source command identity
и общий Gateway/application service; `/cancel` остаётся ingress collection
command.

IR-5 намеренно закрывает только checkpoint-level control suppression. Если
pause/reset уже виден `CP-BEFORE-TERMINAL-COMMIT`, stale candidate подавляется.
Late race `последний checkpoint/recheck → новый control/input → terminal commit`
остаётся IR-7 и требует durable `CycleFinalizationRecord` barrier. Startup-wide
reconstruction/reconciliation остаётся IR-8. Полная corruption/recovery matrix
вокруг `recover_cycle_authority()` остаётся IR-8/IR-10.

Финальный IR-4 code/test evidence:

- итоговый code/test HEAD:
  `1d31b6fbd1d5e88966d3964dc35cf4680f32f522`;
- `Validate Input Runtime` #115 — success, compile success, `241 passed`,
  `0 failed`;
- `Validate v0.4 file artifacts PR` #518 — success, validation suites и status
  enforcement success;
- regression-fix проход после `224911a…` изменял только tests; production code
  на этом проходе не менялся.

Финальный IR-3 evidence сохраняется как предыдущая implementation boundary:

- основной admission implementation:
  `4929b703d7f6e200392661b2b66205b8fa4ca034`;
- crash-safe capacity/runner handoff hardening:
  `d11db7f2a2f8caae900f3bc94ed91de020059231`;
- cancellation-safe/storage-neutral handoff commit:
  `e8192380cc3104668ea9b0f3f017d3c962fd65e4`;
- итоговый IR-3 code HEAD после узкого test-fixture fix:
  `c36e4cc38095e15f54f63ae81c29b4829defec1f`;
- `Validate Input Runtime` #84 — success, `198 passed`;
- `Validate v0.4 file artifacts PR` #503 — success.

IR-3 подключил IR-1/IR-2 repositories в production composition и ввёл общий
`InputAdmissionService` для каждого immutable `CommittedInputBatch`. Initial batch
получает назначенный admission service `cycle_id` и запускает ровно один runner.
Второй batch во время `running` получает durable admission, monotonic sequence и
FIFO `CycleInboxItem` того же cycle, возвращает structured acknowledgement и не
вызывает второй `MCPClient.process_query()`.

Hardened IR-3 закрывает admission/runner boundary:

- count/byte capacity authority определяется pending/admitted admissions активного
  cycle, поэтому admission с ещё не восстановленным inbox уже занимает
  reservation и не позволяет обойти limit после crash;
- между pre-run setup и `process_query()` записывается durable runtime handoff
  marker: ошибка до handoff остаётся retryable, а неоднозначность после handoff
  переводит cycle в `interrupted` и запрещает blind replay LLM/tool side effects;
- `SessionExecutionCoordinator.wake()` выставляет event только для exact
  reserved/active `cycle_id`; поздний wake старого cycle не будит новый cycle;
- `start_admitted_cycle()` и временный `resume_admitted_cycle()` отдельно
  обрабатывают `asyncio.CancelledError`, выполняют durable cleanup в отдельной
  task, ожидают её через `asyncio.shield`, переживают повторную cancellation и
  затем повторно выбрасывают исходный `CancelledError`;
- initial cancellation до marker оставляет admission retryable; cancellation
  после marker переводит marker в `AMBIGUOUS`, session cycle в `interrupted`, а
  duplicate не вызывает второй `process_query()`;
- WAITING cancellation после claim, но до marker, requeue-ит claim; после marker
  claim не requeue-ится, остаётся durable evidence для будущего IR-8
  reconciliation, marker становится `AMBIGUOUS`, cycle — `interrupted`;
- handoff persistence вынесен за application boundary: нейтральный
  `RuntimeHandoffRepository` предоставляет command-oriented
  `get/begin/complete/mark_ambiguous`, а concrete filesystem adapter создаётся
  только в `create_filesystem_input_runtime_repositories(...)`;
- stale handoff token не завершает другую attempt; terminal timestamps не могут
  предшествовать `handed_off_at`; recreation filesystem bundle читает прежний
  marker.

Duplicate replay возвращает существующую relation, capacity block является typed
и retryable, а record-first crash windows admission/inbox/runner-start покрыты
reconciliation tests. После durable runtime handoff неоднозначный failure или
cancellation не requeue-ит input для автоматического повторного
`process_query()`. `interrupted` не выполняет unsafe automatic replay.

IR-4 добавил active-context ownership поверх этой admission boundary:

- initial execution до первого main LLM/result проходит `CP-RESUME`, создаёт
  initial linear context revision `R1` и durable `ActiveCycleSnapshot`;
- каждый successful apply создаёт следующую linear `CycleContextRevision`;
- additions применяются bounded contiguous FIFO ranges в `cycle_sequence` order;
- checkpoint фиксирует accepted-at-entry watermark и не затягивает в текущий
  drain input, admitted уже после входа в checkpoint;
- на один applied range создаётся ровно один runtime-owned
  `input_batch_update` и ровно одна новая context revision;
- protocol-safe checkpoint matrix встроена в create/resume, main LLM, завершённый
  tool block, WAITING, final-processing, terminal-candidate и interruption
  boundaries без вставки input внутрь незакрытого tool-call block;
- `WAITING_USER` reply больше не владеет отдельным legacy semantic path: он
  проходит общий FIFO `CP-RESUME`, не обходит более ранние queued additions и не
  подменяет initial `original_input_batch_id`;
- snapshot-first crash protocol делает persisted snapshot watermark authority:
  если snapshot уже сохранён, а inbox/admission marking не завершился, retry
  домаркировывает records без второго update/revision;
- claim acquisition и apply cancellation-safe: cancellation не теряет durable
  claim authority и не создаёт duplicate application;
- runtime handoff сначала durable завершается, и только затем выполняется
  terminal snapshot synchronization; terminal snapshot не используется как
  замена handoff completion;
- stale `WAITING_USER`/final candidate suppression уже существует на
  checkpoint-level: accepted-at-entry mismatch заставляет применить input и
  продолжить cycle вместо публикации устаревшего candidate.

Явно **не реализованы** на текущем baseline:

- IR-6 durable `AgentEmission` и delivery lifecycle;
- IR-7 durable finalization barrier и полный terminal phase machine;
- IR-8 startup recovery/reconciliation и process-restart cycle reconstruction;
- IR-9 complete diagnostics/client projection completion;
- IR-10 full randomized/restart/synthetic/live acceptance;
- scheduler и parallel branches/fork-join semantics;
- Telegram history rewind по edited message.

IR-2 добавил filesystem implementations repository ports, атомарные записи,
bounded reference-counted coordination, authoritative session-local
pre-allocation repair, coordinated sequence allocation, claim fencing/recovery,
authoritative `payload_size_bytes`, generation cancellation и root-global
identity fencing с порядком `root identity → session`.

Global create/append/prepare paths используют crash-recoverable protocol
`lookup/recovery → durable record write → index/cycle-authority writes`. Потерянные
или dangling indexes перед competing create сверяются редким exact-identity scan:
единственный authoritative record восстанавливает все relations, отсутствие
record очищает незавершённую reservation, а неоднозначность возвращает managed
consistency error. Crash после durable record и до любого или части indexes не
создаёт второй record после restart.

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

В рабочей ветке уже существуют важные foundations и IR-1—IR-5 implementation:

- immutable `CommittedInputBatch` и durable ingress;
- `SessionExecutionCoordinator` с defensive in-process run lease/wake/generation
  cache, но без durable semantic authority;
- один active cycle на session на уровне текущего API orchestration;
- durable admission/FIFO `CycleInbox` для additions активного cycle;
- initial `R1`, linear context revisions, durable active-cycle snapshot и applied
  input watermarks;
- protocol-safe checkpoints и common FIFO apply для running/WAITING additions;
- durable `SessionControlCommand`, control watermarks и generation fencing;
- cooperative pause/resume/reset через общий application control service;
- paused additions без auto-resume и same-cycle continue;
- Telegram `/stop`/`/continue` через transport-neutral Gateway boundary;
- `ProgressEvent`, durable `OutputBatch`, client capabilities и delivery receipts.

Эти foundations всё ещё не являются полным input runtime:

- durable intermediate agent message пока не является отдельной completed
  interaction lifecycle (IR-6);
- checkpoint-level stale candidate/control suppression ещё не является durable
  finalization barrier (IR-7);
- startup recovery ambiguous handoff/claim/control authority ещё не реализован
  (IR-8);
- full diagnostics/randomized/restart/live acceptance остаются IR-9/IR-10.

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

IR-5 реализует control acceptance/reset generation transition под этой короткой
boundary. LLM/tool/Telegram delivery и ожидание atomic block выполняются вне неё.

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
13. Неоднозначный внешний side effect после crash или cancellation не повторяется
    автоматически.
14. Domain IDs не зависят от Telegram/Web/CLI message IDs.
15. Application services зависят от repository ports, а не от filesystem root,
    locks или serialization helpers.
16. В `v0.4` context revisions линейны, но identity допускает будущие multiple
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

IR-1—IR-5 уже покрывают первые control/input runtime slices этого scope;
оставшиеся пункты реализуются IR-6—IR-10.

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
SessionInputRuntimeRepository
InputAdmissionRepository
CycleInboxRepository
RuntimeHandoffRepository
SessionControlRepository
ActiveCycleSnapshotRepository
ContextRevisionRepository
AgentEmissionRepository
FinalizationRepository
```

Filesystem implementations принадлежат `v0.4`. PostgreSQL implementations
`v0.5` сохраняют те же IDs, transitions, idempotency keys и ownership relations,
но заменяют короткую filesystem coordination boundary транзакцией/row locking.
IR-5 application control/checkpoint layers не зависят от `Path`, filesystem
layout, Telegram types или concrete lock registry.

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

IR-5 implementation не меняет эту release boundary: общий update остаётся
`partial` до IR-6—IR-10.
