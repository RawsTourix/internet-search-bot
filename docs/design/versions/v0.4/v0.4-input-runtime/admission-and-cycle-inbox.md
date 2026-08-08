---
id: design.v0.4.input-runtime.admission
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-08-08
---

# Admission и CycleInbox

## Статус реализации

IR-3 реализован поверх IR-1/IR-2 и после contract hardening подтверждён CI:

- основной IR-3 code SHA: `4929b703d7f6e200392661b2b66205b8fa4ca034`;
- hardening commit: `d11db7f2a2f8caae900f3bc94ed91de020059231`;
- итоговый hardened code HEAD: `5441250069c0b2984461e8dd63429f3928c7918c`;
- `Validate Input Runtime` run #79 — success, `187 passed`;
- `Validate v0.4 file artifacts PR` run #500 — success;
- every production `CommittedInputBatch` проходит `InputAdmissionService`;
- второй committed batch во время `running` получает durable admission и FIFO
  `CycleInboxItem` того же cycle, но не запускает второй `process_query()`;
- deterministic no-parallel test подтверждает `process_query call count == 1`
  при трёх последовательных additions;
- duplicate/capacity/crash windows replay-safe и recoverable;
- capacity reservation остаётся authoritative даже при crash между admission и
  inbox publication;
- durable runtime handoff marker запрещает blind replay после неоднозначного
  `process_query()` или последующего persistence failure;
- late wake несовпадающего cycle не изменяет wake event нового cycle.

IR-4 позже перевёл queued additions и `WAITING_USER` reply на общий
`CycleInputApplier`; IR-5 добавил durable controls; IR-6 — semantic emissions;
IR-7 — durable finalization barrier. Corrective IR-7 дополнительно linearizes
**admission decision ↔ terminal commit** на durable repository coordination и
подтверждён на code boundary
`6bd0dce0018b20520ed28236211fccdf0a8075fb`:

- `Validate Input Runtime` #417 — success, compile success,
  `387 passed`, `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #669 — success;
- workflow permission остаётся `contents: read`.

Нормальная live race не зависит от IR-8 committed-but-unadmitted startup repair.

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

Application read и первичная classification являются optimistic. Настоящий
linearization point для relation с terminal commit — короткая durable repository
`root identity → session` coordination boundary.

Обычный flow:

1. повторно читается authoritative committed batch;
2. проверяется ownership `batch.session_id == session_id`;
3. выполняется idempotency lookup по `input_batch_id`;
4. читается current runtime state/generation;
5. оптимистично выбираются admission kind/action/projection/target;
6. capacity проверяется для non-start candidate;
7. repository получает durable coordination и повторно читает latest state;
8. назначает monotonic session/cycle sequence и сохраняет ровно один admission;
9. при addition создаётся `CycleInboxItem`;
10. accepted watermark становится durable;
11. caller получает typed outcome.

Если между steps 5 и 7 terminal authority выиграла ordering, stale non-start
classification **не** разрешается только по совпадающему old `active_cycle_id`.
Repository до admission/index/inbox/session-watermark write возвращает dedicated
managed stale-decision conflict. Application только для этого exact conflict один
раз перечитывает authoritative state и заново вычисляет:

```text
AdmissionKind
InputAdmissionAction
projection key
target cycle
capacity decision
should_start_runner
should_wake_runner
```

Arbitrary consistency/corruption conflict не retry-ится и не маскируется.

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
| `finalizing` | `queued_running` | если admission linearizes first, accepted watermark aborts stale finalization |
| terminal/no resumable cycle | `start_cycle` | новый cycle после terminal boundary |

### Admission ↔ terminal commit tie-break — implemented IR-7

Порядок определяется durable coordination, а не временем application read,
создания tasks, wall clock или transport arrival.

**Admission wins first:**

```text
optimistic FINALIZING → CONTINUE_RUNNING for cycle A
→ durable admission allocation for cycle A
→ accepted watermark advances
→ terminal second recheck observes accepted > applied
→ ABORTED_NEW_INPUT
→ RuntimeHandoff remains HANDED_OFF
→ cycle A returns/remains RUNNING and later applies input
```

**Terminal wins first:**

```text
optimistic FINALIZING → CONTINUE_RUNNING for cycle A
→ terminal second recheck clean
→ RuntimeHandoff COMPLETED
→ terminal snapshot/session
→ TERMINAL_COMMITTED
→ stale non-start candidate reaches repository coordination
→ dedicated stale-decision conflict before admission writes
→ same in-process call re-reads terminal state
→ START_CYCLE for new cycle B, cycle_sequence=0
```

No transport retry требуется. Старый final output остаётся валидным и deliverable;
late committed batch получает ровно один admission нового cycle.

`IDLE` имеет важную IR-2 nuance: raw IDLE может быть state, отставшим после
record-first START admission crash. Поэтому repository сначала разрешает
existing authoritative admission repair/corruption validation. Только если после
этого state действительно остаётся IDLE, stale non-start classification может
быть reclassified. `DONE/ERROR/CANCELLED` normal terminal authority fenced до
нового admission write. Это сохраняет IR-2 crash repair и не превращает
corruption (`gap`, duplicate sequence и т.п.) в retryable stale decision.

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

Startup discovery/repair такого состояния относится к IR-8. Однако normal
in-process admission-vs-terminal race IR-7 разрешает сразу внутри исходного
admission call и не оставляет batch unadmitted только потому, что optimistic
classification устарела.

#### После B, до C

Admission существует, но session watermark отстаёт.

Recovery вычисляет expected watermark из ordered admission records и выполняет
compare-and-swap repair. Existing IR-2 record-first repair сохраняется.

#### После durable admission, до inbox publication

Admission уже является authoritative capacity reservation. Даже если
`CycleInboxItem` ещё отсутствует после crash, новый batch не может обойти
`max_queued_batches_per_session` или `max_queued_bytes_per_session`.

Повтор exact batch:

```text
find existing admission
→ reuse original session/cycle sequence
→ create exactly one missing inbox item
→ return existing relation
```

#### После B/C, до UI acknowledgement

Повтор transport request/idempotency key возвращает существующий admission
outcome. Новый AgentCycle не создаётся.

### Stale decision before B

Dedicated terminal-race stale decision обнаруживается **до** B:

```text
no InputAdmissionRecord
no admission indexes
no CycleInboxItem
no accepted watermark mutation
```

После reclassification тот же `input_batch_id` получает ровно один durable
admission. Нельзя создать одновременно old-cycle `CONTINUE_RUNNING` и new-cycle
`START_CYCLE` records.

### Acknowledgement rule

Фраза «дополнение принято в текущую задачу» разрешена только после durable
admission. До этого transport может показывать только ingress status вида
«сообщение принято/пакет собирается».

Если terminal-first reclassification создала новый cycle, typed outcome обязан
сообщать `START_CYCLE`, а не stale `QUEUED_RUNNING` projection.

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
- unread content не попадает в compaction summary;
- terminal-first stale candidate не создаёт old-cycle inbox item перед
  reclassification.

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

### Authoritative capacity reservation

Count/byte reservation определяется не наличием inbox file, а authoritative
admission records активного cycle. Capacity занимает admission, для которого:

```text
cycle_sequence > 0
cycle_sequence > active_cycle_applied_through_sequence
state == admitted/pending
payload_size_bytes учитывается в byte limit
```

Initial admission с `cycle_sequence == 0` не является addition и capacity не
занимает. Applied/cancelled/failed-terminal records очередь не занимают.
Отсутствующий после crash inbox не освобождает reservation до reconciliation.

При stale-decision reclassification capacity пересчитывается заново относительно
latest state. Terminal-first path становится `START_CYCLE`, поэтому stale
old-cycle capacity decision не переносится в новый cycle.

### Running/paused cycle

Если limit исчерпан:

- новый committed batch не теряется;
- admission получает controlled state/reason `capacity_blocked` либо остаётся
  в recoverable unadmitted set;
- caller получает явный retryable response;
- runtime не создаёт второй cycle как обход лимита.

Для первой реализации conservative protocol остаётся:

```text
committed batch persisted
→ admission capacity unavailable
→ return retryable accepted-but-not-admitted status
→ explicit admission retry/recovery according to policy
```

Это отдельный backpressure contract и не относится к normal terminal race, где
transparent reclassification должна завершиться в том же call.

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

`SessionExecutionCoordinator.wake(session_id, cycle_id=...)` выставляет event
только если `cycle_id` совпадает с exact reserved/active cycle. Несовпадающий
late wake возвращает `False` и не изменяет event нового cycle.

Terminal-first reclassification возвращает `should_start_runner=true` для new
cycle B и `should_wake_runner=false`; stale cycle A не получает wake и второй
old-cycle runner не появляется.

Потерянный signal безопасен: runner всё равно проверяет inbox перед следующим
LLM request, `WAITING_USER` и finalization. После restart recovery создаёт новый
runner/resume command по policy IR-8.

`SessionExecutionCoordinator` остаётся execution lease/wakeup foundation, но не
source of truth очереди.

## Runtime handoff boundary

IR-3 использует минимальный durable marker между безопасным setup и первым
side-effecting вызовом Agent Runtime:

```text
resolve authoritative batch/capabilities
→ durable RuntimeHandoffRecord(handed_off)
→ invoke process_query()
→ complete либо mark ambiguous
```

Ошибка до marker оставляет admission retryable. После marker duplicate не может
повторно вызвать `process_query()`. Исключение после invocation либо после
успешного runtime result, но до следующего persistence step, переводит marker и
cycle в ambiguous/interrupted contract без automatic replay внешних действий.
Этот marker не заменяет IR-4 active snapshot и не реализует IR-8 startup recovery.

Corrected IR-7 сохраняет отдельный terminal invariant:

```text
second terminal recheck
→ RuntimeHandoff COMPLETED
→ terminal snapshot/session
→ TERMINAL_COMMITTED
```

Admission-first accepted watermark выигрывает **до** этого completion и оставляет
handoff `HANDED_OFF`. Terminal-first полностью завершает old handoff, после чего
late batch начинает independent new cycle B.

## WAITING_USER migration

`WAITING_USER` reply теперь admitted в тот же cycle и проходит common FIFO
`CP-RESUME`; legacy compatibility details остаются только историческим IR-3
контекстом.

Обязательный контракт IR-4: если перед `WAITING_USER` reply уже существуют более
ранние queued additions, reply не может обходить их. Общий `CycleInputApplier`
применяет contiguous FIFO range в порядке cycle sequence.

Artifact refs прежнего и нового batch сохраняются через общий context/application
path, а не через временную замену `original_input_batch_id`.

## Finalization integration

IR-7 требует, чтобы pending accepted input подавлял stale `DONE`, question и
output до terminal commit. Durable admission watermark является основанием
abort/recheck finalization, даже если inbox/application ещё не успели завершиться.

Admission-vs-terminal race имеет один durable tie-break:

```text
admission allocation first
→ same-cycle accepted watermark
→ finalization ABORTED_NEW_INPUT

terminal authority first
→ old cycle TERMINAL_COMMITTED
→ stale optimistic admission discarded before writes
→ same committed batch START_CYCLE in new cycle
```

Нормальный live race не должен оставлять committed-but-unadmitted input для
будущего IR-8 scanner.

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
- missing inbox admission продолжает занимать count/byte capacity;
- runtime handoff ambiguity не приводит к automatic replay;
- late wake старого cycle не будит новый cycle;
- capacity limit не теряет committed input;
- lost wakeup не блокирует последующее checkpoint применение;
- terminal-first после stale optimistic `CONTINUE_RUNNING` transparently возвращает
  `START_CYCLE` нового cycle без raw Pydantic `ValidationError` и без transport
  retry;
- admission-first durable allocation к finalizing cycle продвигает accepted
  watermark и заставляет terminal barrier вернуть `ABORTED_NEW_INPUT`, сохраняя
  handoff `HANDED_OFF`;
- stale-decision detection происходит до admission/index/inbox/watermark write;
- один committed batch получает ровно один admission; terminal-first path не
  оставляет old-cycle inbox relation;
- IR-2 record-first state repair и corruption conflicts не маскируются новым
  stale-decision retry.

Startup committed-but-unadmitted discovery/reconciliation остаётся IR-8.
