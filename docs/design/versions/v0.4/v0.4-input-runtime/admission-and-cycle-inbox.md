---
id: design.v0.4.input-runtime.admission
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-08-06
---

# Admission и CycleInbox

## Статус реализации

IR-3 реализован поверх IR-1/IR-2 и подтверждён CI на code SHA
`4929b703d7f6e200392661b2b66205b8fa4ca034`:

- `Validate Input Runtime` run #73 — success, `181 passed`;
- `Validate v0.4 file artifacts PR` run #497 — success;
- every production `CommittedInputBatch` проходит `InputAdmissionService`;
- второй committed batch во время `running` получает durable admission и FIFO
  `CycleInboxItem` того же cycle, но не запускает второй `process_query()`;
- deterministic no-parallel test подтверждает `process_query call count == 1`
  при трёх последовательных additions;
- duplicate/capacity/crash windows replay-safe и recoverable;
- `WAITING_USER` временно использует compatibility adapter того же cycle.

IR-3 заканчивается на admission/queue boundary. Queued additions ещё не
применяются к LLM context: checkpoints, общий `CycleInputApplier` и context
revisions относятся к IR-4. `/stop`, `/continue`, emissions и finalization
barrier также ещё не реализованы.

## Назначение

Документ определяет переход от immutable `CommittedInputBatch` к active runtime,
FIFO ordering, backpressure, leases и replay-safe queue drain.

Ingress и Input Runtime разделены жёстко:

```text
Ingress владеет приёмом, группировкой, upload и commit.
Input Runtime владеет назначением committed batch активной задаче.
```

Transport не выбирает `cycle_id` и не вызывает `MCPClient.process_query()`
напрямую после каждого commit.

## Admission decision

`InputAdmissionService.admit_committed_batch(...)` получает:

```text
input_batch_id
session_id
request/source metadata
current SessionInputRuntimeState
```

Под короткой session coordination boundary он:

1. повторно читает authoritative committed batch;
2. проверяет ownership `batch.session_id == session_id`;
3. выполняет idempotency lookup по `input_batch_id`;
4. читает active runtime state/generation;
5. выбирает admission kind;
6. назначает monotonic session/cycle sequence;
7. сохраняет admission record;
8. при addition создаёт `CycleInboxItem`;
9. обновляет accepted watermark;
10. возвращает typed outcome для caller/UI.

Typed outcome:

```python
class InputAdmissionOutcome(BaseModel):
    admission_id: str | None
    input_batch_id: str
    session_id: str
    target_cycle_id: str | None
    session_sequence: int | None
    cycle_sequence: int | None

    action: Literal[
        "start_cycle",
        "queued_running",
        "resume_waiting",
        "queued_paused",
        "resume_interrupted",
        "duplicate",
        "capacity_blocked",
    ]

    should_start_runner: bool
    should_wake_runner: bool
    user_projection_key: str
    retryable: bool
    reason_code: str
```

## Decision table

| Runtime state | Admission action | Runner behavior |
|---|---|---|
| `idle`, no active cycle | `start_cycle` | создать/запустить новый cycle |
| `running` | `queued_running` | не запускать второй cycle; signal/wakeup |
| `pause_requested` | `queued_paused` | сохранить, не мешать достижению паузы |
| `paused_by_user` | `queued_paused` | сохранить, не возобновлять |
| `waiting_user` | `resume_waiting` | wake/resume тот же cycle |
| `interrupted`, resumable | `resume_interrupted` | wake/resume тот же cycle |
| `finalizing` | `queued_running` | accepted watermark aborts stale finalization |
| terminal/no resumable cycle | `start_cycle` | новый cycle после terminal boundary |

Если state содержит terminal cycle, но terminal commit ещё не reconciled,
admission не угадывает. Он ждёт/повторяет coordination operation после
finalization reconciliation.

## Commit-to-admission protocol на filesystem

Настоящей транзакции между ingress batch store и input-runtime store в `v0.4`
нет. Используется recoverable protocol:

```text
A. CommittedInputBatch persisted
B. InputAdmissionRecord persisted
C. Session watermark persisted
D. UI acknowledgement allowed
```

### Crash windows

#### После A, до B

Batch committed, но admission отсутствует.

Recovery:

```text
scan committed batches eligible for runtime
→ lookup admission by input_batch_id
→ admit_if_absent()
```

#### После B, до C

Admission существует, но session watermark отстаёт.

Recovery вычисляет expected watermark из ordered admission records и выполняет
compare-and-swap repair.

#### После B/C, до UI acknowledgement

Повтор transport request/idempotency key возвращает существующий admission
outcome. Новый AgentCycle не создаётся.

### Acknowledgement rule

Фраза «дополнение принято в текущую задачу» разрешена только после durable
admission. До этого transport может показывать только ingress status вида
«сообщение принято/пакет собирается».

## Session sequence и cycle sequence

`session_sequence` монотонен для всех admitted batches одной session.

`cycle_sequence` монотонен внутри конкретного cycle:

```text
0 initial InputBatch
1 first addition
2 second addition
...
```

Порядок определяется admission commit, а не client timestamp, Telegram update ID
или завершение upload. Это даёт deterministic runtime order после race/restart.

Source timestamps и transport order сохраняются как metadata/evidence, но не
заменяют authoritative sequence.

## CycleInbox enqueue

Для `cycle_sequence > 0` admission создаёт один inbox item.

Инварианты:

- unique `admission_id` и `input_batch_id`;
- enqueue идемпотентен;
- item с generation, отличной от active session generation, не применяется;
- item не содержит raw payload; только refs/identity;
- batch content читается из committed batch store при apply;
- unread content не попадает в compaction summary.

## Claim и bounded drain

Checkpoint запрашивает head range:

```python
claim_next(
    cycle_id,
    after_sequence=applied_through_sequence,
    max_items,
    max_total_bytes,
    lease_seconds,
) -> ClaimedInboxRange
```

Обязательные ограничения:

```text
max_queued_batches_per_session
max_queued_bytes_per_session
max_batches_per_checkpoint
max_batch_bytes_per_checkpoint
claim_lease_seconds
```

Optional safety limits:

```text
max_text_chars_per_checkpoint
max_artifact_refs_per_checkpoint
max_checkpoint_drain_seconds
```

Limits принадлежат runtime config/policy и не передаются LLM.

### Fairness

Checkpoint не обязан вычитывать бесконечную очередь полностью. Он применяет
bounded contiguous range и затем даёт агенту выполнить следующий meaningful
step. Остаток остаётся queued.

Однако finalization требует полного drain всех accepted additions активного cycle
или abort finalization. Поэтому при непрерывном input возможна controlled
задержка terminal answer; runtime не должен выдавать заведомо устаревший ответ.

## Backpressure

Queue limits проверяются до durable admission acknowledgement.

### Running/paused cycle

Если limit исчерпан:

- новый committed batch не теряется;
- admission получает controlled state/reason `capacity_blocked` либо остаётся
  в recoverable unadmitted set;
- caller получает явный retryable response;
- runtime не создаёт второй cycle как обход лимита.

Для первой реализации предпочтителен conservative protocol:

```text
committed batch persisted
→ admission capacity unavailable
→ return retryable accepted-but-not-admitted status
→ recovery/admission retry
```

Пользовательский текст не должен утверждать, что дополнение уже будет учтено в
текущем cycle, пока admission не завершён.

### Почему нельзя rejected-and-forgotten

После commit payload уже является durable пользовательским input. Удалить его
из-за временной переполненности очереди нельзя. Retention/cleanup применяется
только после явного terminal decision и audit trail.

## Apply protocol

`CycleInputApplier` выполняет один claimed range:

```text
1. validate claim/generation/cycle
2. load exact CommittedInputBatch records
3. validate contiguous cycle_sequence
4. check snapshot replay state
5. build one input_batch_update message
6. activate runtime-owned artifact refs
7. append protocol-valid user message
8. create CycleContextRevision
9. persist ActiveCycleSnapshot + applied watermark
10. mark inbox items/admissions applied
11. publish trace/progress/projection events
```

Filesystem crash между 9 и 10 reconciles по snapshot watermark: items
помечаются applied без повторного append.

Crash до 9 оставляет claim replayable; повторный apply сначала проверяет
`applied_input_batch_ids` и sequence watermark.

## Wakeup и runner ownership

Durable inbox не запускает параллельный AgentCycle.

In-process signal выполняет только wakeup:

```text
admission queued
→ set event/notify active runner
→ runner читает durable state в checkpoint
```

Потерянный signal безопасен: runner всё равно проверяет inbox перед следующим
LLM request, `WAITING_USER` и finalization. После restart recovery создаёт новый
runner/resume command по policy.

`SessionExecutionCoordinator` остаётся execution lease/wakeup foundation, но не
source of truth очереди.

## WAITING_USER migration

Текущий специальный `WaitingUserBatchContinuationMixin` временно сохраняется как
compatibility adapter.

Migration:

```text
committed reply
→ InputAdmissionService(resume_waiting)
→ durable inbox item
→ resume same cycle
→ common CycleInputApplier
```

После parity tests mixin удаляется. Artifact refs прежнего и нового batch
сохраняются через общий context/application path, а не через временную замену
`original_input_batch_id`.

## Diagnostics

`/status` и internal API должны показывать минимум:

```text
active_cycle_id/status/generation
accepted_through_sequence
applied_through_sequence
queued/claimed/applying counts
oldest queued age
queued bytes estimate
last admission/apply error code
```

Raw user text, file payload и secrets в diagnostics не выводятся.

## Acceptance

- два concurrent commits одной session получают разные последовательные numbers;
- duplicate commit/replay возвращает один admission;
- running cycle не создаёт второй cycle;
- paused cycle принимает additions без auto-resume;
- waiting cycle resumes same cycle;
- queue head применяется строго FIFO;
- claim expiry не дублирует LLM message;
- commit/admission crash windows восстанавливаются;
- capacity limit не теряет committed input;
- lost wakeup не блокирует последующее checkpoint применение.
