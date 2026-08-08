---
id: design.v0.4.input-runtime.contracts-acceptance
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-08
---

# Contracts и acceptance

## Статус реализации

Observable contracts IR-1—IR-7 реализованы. IR-8—IR-10 остаются planned,
поэтому общий `v0.4-input-runtime` остаётся `partial`.

Финальный corrected IR-7 code/test boundary:

- code/test HEAD: `6bd0dce0018b20520ed28236211fccdf0a8075fb`;
- `Validate Input Runtime` #417 — success, production compile success,
  `387 passed`, `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #669 — success;
- workflow сохраняет `permissions: contents: read`;
- focused tests используют explicit `asyncio.Event`, injected persistence faults,
  repository recreation и production-like admission/handoff/output-claim paths;
- previous eight handoff-ordering tests остаются green;
- три новых deterministic tests закрывают terminal-first reclassification,
  admission-first terminal abort и pre-write stale-decision/no-raw-ValidationError;
- real Agent/LLM/MCP/Telegram/Web/internet calls для IR-7 tests не выполняются.

Corrected IR-7 активирует existing
`CycleFinalizationRecord`/`FinalizationRepository`, сохраняет stable logical
finalization identity, exact admitted `RuntimeHandoff` relation,
`PREPARED → RESULT_PERSISTED → OUTPUT_READY → TERMINAL_COMMITTED`, repeated
authoritative input/control rechecks, WAITING commit barrier и transport-neutral
final `OutputBatch` claim fence.

Successful terminal ordering соответствует IR-3/IR-4 invariant:

```text
second terminal recheck
→ RuntimeHandoff COMPLETED
→ terminal snapshot/session convergence
→ TERMINAL_COMMITTED
→ final OutputBatch delivery eligibility
```

Late durable input/control abort-ит **до** handoff completion. Persisted
result/`OutputBatch READY` не являются terminal authority. Concurrent
`AgentEmission READY claim ↔ terminal commit` linearizable через общую
exact-session coordination; network send остаётся вне lock. Direct
repository-recreation retry собственного finalization protocol идемпотентен,
включая completed-handoff/incomplete-terminal window.

Corrected IR-7 также linearizes `admission durable allocation ↔ terminal commit`:
optimistic application state read не является tie-break. Admission-first
продвигает same-cycle accepted watermark и заставляет stale finalization abort;
terminal-first invalidates stale non-start classification до admission writes и
тот же call reclassifies committed batch как новый `START_CYCLE`. Это normal
in-process contract, не IR-8 startup recovery.

Финальный IR-6 code/test boundary:

- code/test HEAD: `4447d1bfe487bfd764829e701f274655aa8c3c50`;
- `Validate Input Runtime` #297 — success, compile success, `350 passed`,
  `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #609 — success.

IR-6 реализует explicit durable semantic `send_user_message`, runtime-owned exact
cycle/generation/context-revision/tool-call identity, stable replay idempotency,
linearizable intermediate-message policy, trusted sanitized response route,
READY-before-tool-success persistence, fenced claim/receipt delivery lifecycle,
conservative `FAILED`/`UNKNOWN`, no blind UNKNOWN replay, Telegram separate
semantic delivery и optional server-resolved reply binding. Emission persistence
не меняет context revision, WAITING state или input/control watermarks, а delivery
failure не завершает AgentCycle и не блокирует later final `OutputBatch`.

Sequential terminal fencing IR-6 сохранён: already-terminal cycle отвергает новый
intermediate intent, а READY emission не начинает delivery после already-visible
terminal state. IR-7 закрывает concurrent `claim ↔ terminal/finalization commit`.
Startup reconstruction/reconciliation, включая UNKNOWN delivery, остаётся IR-8;
полные projections/diagnostics — IR-9; full randomized/restart/synthetic/live
acceptance — IR-10.

Финальный IR-5 code/test boundary:

- corrected code/test HEAD: `0fabe15c6730a4e8db6be8b54ecec2c13ea773c7`;
- `Validate Input Runtime` #219 — success, compile success, `291 passed`,
  `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #570 — success.

IR-5 реализует durable control acceptance/idempotency/sequence и watermarks,
cooperative `/stop`, same-cycle `/continue`, paused admission без auto-resume,
generation-authoritative `/reset`, checkpoint control reduction и Telegram
`/stop`/`/continue` через общий application service. Continue resume target
фиксируется атомарно внутри shared durable session coordination, а не из state,
прочитанного до lock. Checkpoint-level control suppression перед terminal
transition остаётся первой линией защиты; IR-7 добавляет repeated authoritative
recheck и закрывает late race `последний checkpoint → новый control/input →
terminal commit`. Startup reconstruction/reconciliation остаётся IR-8; полная
`recover_cycle_authority()` corruption matrix — IR-8/IR-10.

## Назначение

Документ фиксирует observable contracts обновления. Tests должны проверять
поведение и durable state, а не private method names или конкретную структуру
filesystem folders.

## Functional contract

### Initial input

```text
idle session + committed batch
→ one admission sequence 0
→ one AgentCycle
→ existing final output behavior preserved
```

### Addition during active work

```text
running cycle + committed batch
→ no second AgentCycle
→ durable admission/inbox
→ user acknowledgement
→ safe checkpoint application
→ same cycle continues
```

### Multiple additions

```text
ibat_2, ibat_3, ibat_4 admitted
→ cycle sequences 1,2,3
→ one or bounded contiguous input_batch_update projections
→ exact order and boundaries retained
```

### Admission vs terminal commit

Authoritative tie-break — durable repository coordination, не optimistic state
read и не task creation/wall clock.

Admission-first:

```text
optimistic FINALIZING → CONTINUE_RUNNING(A)
→ durable admission allocation to A
→ accepted watermark advances
→ terminal second recheck sees accepted > applied
→ ABORTED_NEW_INPUT
→ RuntimeHandoff remains HANDED_OFF
→ cycle A RUNNING/continues
```

Terminal-first:

```text
optimistic FINALIZING → CONTINUE_RUNNING(A)
→ terminal second recheck clean
→ RuntimeHandoff COMPLETED
→ terminal snapshot/session
→ TERMINAL_COMMITTED
→ stale candidate reaches repository
→ managed stale-decision conflict before writes
→ same admission call re-reads latest state
→ START_CYCLE(B), cycle_sequence=0
→ should_start_runner=true, should_wake_runner=false
```

Before reclassification there must be no late-batch admission record/index,
old-cycle inbox item or accepted-watermark mutation. Same `input_batch_id` ends
with exactly one durable admission; duplicate replay returns it.

Retry/reclassification is bounded and only recognizes the dedicated stale-decision
conflict. Raw Pydantic `ValidationError` must not leak from this race and
arbitrary corruption/consistency conflict must not be silently retried.

Raw `IDLE` is first reconciled against IR-2 authoritative admission records, so
record-first START crash repair and gap/duplicate corruption remain visible.
Normal `DONE/ERROR/CANCELLED` terminal authority fences stale non-start admission
before writes.

### WAITING_USER

```text
candidate waiting + already accepted input
→ waiting suppressed
→ apply and continue
```

```text
waiting committed + new batch
→ same cycle resumes
→ no second dialog turn/cycle
```

IR-7 дополнительно закрывает late waiting race:

```text
candidate question
→ CP-BEFORE-WAITING
→ short exact input/control recheck
→ one durable waiting question authority
→ WAITING_USER
```

Input/control, durable accepted до waiting commit, suppress stale question. Input
после successful waiting commit использует existing same-cycle
`RESUME_WAITING` semantics; второй durable question lifecycle не создаётся.
Admission-vs-terminal correction WAITING path не перерабатывает.

### Pause/resume

```text
/stop during active block
→ command acknowledged
→ block finishes protocol-valid
→ paused_by_user snapshot
→ no next LLM/tool block
```

```text
paused + additions
→ durable queued
→ no auto-resume
→ /continue applies and resumes same cycle
```

Этот slice реализован IR-5. `/continue` без additions сохраняет тот же cycle и не
создаёт фиктивный user input/context revision. Continue target фиксируется внутри
той же short durable `root identity → session` coordination, которая упорядочивает
input admission и control publication:

```text
input coordinated before continue
→ included in frozen resume target
→ drained before first meaningful post-resume LLM

continue coordinated before input
→ late input excluded from frozen target
→ stays queued for next ordinary running checkpoint
```

Transport/Telegram arrival time, wall clock, порядок создания `asyncio` tasks,
pre-lock state read и post-persistence state reread не определяют эту boundary.

WAITING contract сохраняет то же правило: real input, coordinated до continue,
drain-ится через `CP-RESUME` и после полного target drain снимает active waiting
state; `/continue` без real input остаётся `WAIT/still_waiting_for_input` и не
создаёт fake answer.

### Intermediate agent message

```text
assistant tool_call(send_user_message)
→ runtime-owned exact execution context
→ policy + trusted route
→ durable AgentEmission READY
→ compact role=tool success
→ cycle remains running
→ independent client delivery lifecycle
```

Этот contract реализован IR-6.

Manager schema содержит только semantic `message`, `kind=intermediate` и
`importance`. LLM не задаёт session/cycle/generation/context revision, transport
route, client instance, reply target, emission ID или idempotency key. Stable
logical identity выводится из runtime-owned cycle/generation/native assistant
`tool_call_id`; same replay возвращает тот же `emission_id`, а changed semantic
arguments дают managed conflict.

После безопасной normalization применяются
`max_intermediate_messages_per_cycle`,
`min_intermediate_message_interval_seconds` и
`max_intermediate_message_chars`. Count/rate/persistence acceptance linearizable
под короткой exact-session coordination; delivery failure не освобождает spam
budget.

### Finalization

```text
accepted input/control before terminal commit
→ stale DONE/WAITING_USER/output suppressed
→ cycle continue/pause/reset
```

```text
terminal commit completed
→ later input starts new cycle
```

IR-4/IR-5 checkpoint-level suppression для уже наблюдаемого input/control
сохранён. Corrected IR-7 реализует durable finalization authority:

```text
candidate DONE
→ CP-BEFORE-FINAL-PROCESSING
→ exact candidate authority + exact admitted handoff relation
→ immutable final processing outside lock
→ PREPARED + short authoritative recheck
→ FINALIZING
→ RESULT_PERSISTED
→ final OutputBatch persisted / OUTPUT_READY
→ second authoritative terminal recheck
IF mismatch:
    ABORTED_NEW_INPUT | ABORTED_CONTROL
    RuntimeHandoff remains HANDED_OFF
    same cycle may continue
ELSE:
    RuntimeHandoff COMPLETED
    → terminal ActiveCycleSnapshot
    → terminal SessionInputRuntimeState
    → TERMINAL_COMMITTED written last
    → final output becomes claimable
```

Canonical eligibility использует authoritative accepted/applied input watermarks,
pending/applied control watermarks, session generation, active-cycle ownership,
exact context/finalization identity и exact runtime-owned
`admission_id + handoff_token`. Любой late durable input/control на second recheck
abort-ит stale finalization до handoff completion. Persisted result и
`OUTPUT_READY` не являются terminal/delivery authority.

Durable handoff completion failure produces no new terminal snapshot, no session
DONE, no `TERMINAL_COMMITTED` and no output eligibility. После successful
`RuntimeHandoff=COMPLETED` этот exact invocation больше не выполняет LLM/tool
side effects; поздний API completion остаётся idempotent compatibility call.

IR-6 sequential emission fencing сохранён, а IR-7 дополнительно linearizes
concurrent `AgentEmission READY claim ↔ terminal commit` тем же exact-session
coordination ordering.

## Protocol integrity

For every prompt-bearing history:

- each assistant tool call has exactly one matching `role=tool` result;
- no user/runtime update appears inside open tool block;
- no orphan tool result;
- runtime-generated input update marked and schema-valid;
- duplicate replay does not append second update;
- compaction output passes existing tool-sequence validation;
- manager emission call/result sequence remains valid;
- `send_user_message` не вставляет второй assistant message с тем же text.

IR-5 stop не force-cancel arbitrary LLM/tool await. Stop during LLM применяется
после завершения bounded attempt и до следующего tool/LLM block. Stop внутри
assistant multi-tool block применяется только после всех matching `role=tool`
results, сохраняя protocol-valid history. Production-loop test удерживает второй
tool call, принимает pause, завершает весь assistant block и подтверждает pause на
`CP-AFTER-TOOL-BLOCK` до следующего LLM.

IR-6 `send_user_message` остаётся обычным manager tool внутри того же native
assistant tool-call block. Durable emission persistence не является checkpoint и
не разрешает применять input/control между matching tool results.

IR-7 finalization/waiting suppression не создаёт synthetic unmatched tool result
и не превращает `send_user_message` в ask-user; OpenAI tool/message protocol
остаётся valid. Handoff completion происходит только после всех side-effecting
runtime blocks и second terminal recheck.

## Persistence contract

- committed input is never deleted because admission temporarily failed;
- normal live admission-vs-terminal race resolves within the same admission call,
  not by leaving the batch committed-but-unadmitted for IR-8;
- stale optimistic admission classification is rejected before admission/index/
  inbox/session-watermark mutation;
- one committed `input_batch_id` ends with exactly one durable admission relation;
- every applied batch has admission and context revision evidence;
- session/cycle watermarks are monotonic within generation;
- reset advances generation and fences old work;
- pause/wait/interrupted snapshot survives repository recreation;
- exact admitted terminal finalization is bound to exact RuntimeHandoff identity;
- final output is not delivery-claimable before terminal commit;
- normal admitted-run `TERMINAL_COMMITTED` implies matching RuntimeHandoff
  `COMPLETED` before output eligibility;
- result/output persistence can recover without rerunning full cycle;
- handoff completion failure cannot be masked by terminal authority;
- repository writes are atomic per record;
- cross-record crash windows have deterministic reconciliation;
- AgentEmission READY is durable before manager-tool success;
- semantic emission persistence does not create a context revision;
- delivery receipt is durable authority for `DELIVERED` and reply binding;
- ambiguous/expired delivery remains `UNKNOWN`, never automatically READY.

IR-5 реализует retry/idempotent repair для control publication/application
crash windows. Before any new control allocation exact-session durable records
repair authoritative pending frontier; different durable records with one
sequence are a managed consistency conflict, not a silent winner. For continue,
frozen input target is part of the already durable command and survives a crash
between record publication and pending-watermark state write. Startup-wide
reconstruction/reconciliation этого durable evidence остаётся IR-8.

IR-6 record-first emission persistence repair восстанавливает missing identity
index после crash. Claim response loss повторяет same durable claim token и не
создаёт второй attempt. Durable delivery receipt повторяется идемпотентно после
lost HTTP response; worker не выполняет второй client send. Full startup policy
для retained UNKNOWN остаётся IR-8.

Corrected IR-7 direct repository recreation/retry сохраняет одну stable logical
finalization. PREPARED replay не создаёт второй identity; persisted result
переиспользует exact payload hash/ref; `OUTPUT_READY` переиспользует exact batch
identity; partial terminal writes не открывают delivery до finalization marker;
lost terminal response возвращает ту же terminal authority.

Отдельное окно `RuntimeHandoff=COMPLETED`, terminal writes incomplete direct
retry-ится по известным IDs: тот же handoff token/completed_at, finalization ID,
result_ref и OutputBatch ID сохраняются, LLM/MCP/tool не replay-ятся. Completed
handoff не понижается обратно в AMBIGUOUS. Startup-wide discovery/reconstruction
этого durable evidence остаётся IR-8.

Если terminal marker ещё не существовал и late durable input/control выиграл до
handoff completion, stale finalization abort-ится; persisted stale result/output
не становится пользовательским final answer.

## Idempotency contract

Stable keys:

```text
input_batch_id → one admission
admission_id → one inbox item
(cycle_id, cycle_sequence) → one logical apply
admission_id + handoff_token → one exact runtime invocation handoff
control idempotency key → one command outcome
cycle + generation + assistant tool_call_id → one semantic AgentEmission
claim token → one active emission delivery attempt
receipt identity → one delivered external message relation
finalization_id → one terminal attempt lifecycle
output_batch_id → existing commit-once output contract
```

At-least-once signals/callbacks/claims must not duplicate:

- LLM input update;
- artifact activation/version;
- AgentCycle;
- RuntimeHandoff completion;
- intermediate message intent;
- intermediate client delivery after acknowledged/ambiguous attempt;
- final output intent;
- external mutating side effect.

Control idempotency IR-5 связывает stable key ровно с одной command delivery;
повтор возвращает тот же control ID/sequence/outcome, а повтор reset не повышает
generation второй раз. Duplicate continue additionally возвращает тот же frozen
resume target, даже если session accepted watermark уже вырос из-за позднего
input; старую boundary нельзя расширить ретроактивно.

IR-6 same logical tool-call replay возвращает existing emission даже после
repository recreation/cancelled manager task. Same claim token возвращает exact
DELIVERING attempt; different token конфликтует. Same receipt повторно подтверждает
тот же DELIVERED state; changed receipt relation конфликтует.

IR-7 stable finalization ID выводится из exact
`session/cycle/generation/context_revision/expected input+control watermarks`.
Exact current RuntimeHandoff relation binding immutable для normal admitted-run
finalization. Повтор той же logical finalization возвращает тот же
identity/state/result/output; repeated handoff completion возвращает тот же
COMPLETED marker без нового token или `completed_at`.

Stale admission reclassification сохраняет `input_batch_id` idempotency: old-cycle
candidate до write не становится admission; new-cycle START relation создаётся
один раз, а последующий duplicate возвращает existing admission.

## Ordering contract

Authoritative order within session/cycle is durable coordination/sequence, not
client message timestamp or arrival completion.

- contiguous FIFO apply;
- no skip over missing sequence;
- bounded drain preserves remaining order;
- controls have explicit priority but preserve audit sequence;
- intermediate policy uses exact cycle/generation durable creation ordering;
- terminalization observes all accepted sequences up to current watermark;
- successful terminal authority cannot precede exact RuntimeHandoff completion;
- admission-vs-terminal tie-break is durable repository coordination, not
  optimistic application state read.

IR-5 control sequence выделяется под короткой session coordination boundary.
`pending_control_sequence` продвигается durable acceptance, а
`applied_control_sequence` — только contiguous terminal control records без
пропуска head command. Effective priority: `reset > pause > continue > input`,
при сохранении всех durable audit records и их sequence.

Continue/input tie-break использует ту же coordination authority: кто первым
получил durable session coordination, тот раньше находится относительно frozen
continue target. State, прочитанный вне этой boundary, не определяет ordering.

IR-6 max-count/min-interval acceptance и persistence используют короткую exact
session coordination, но Telegram/Web network await под ней не выполняется.

Corrected IR-7 terminal command, durable admission allocation и IR-6 READY
emission claim используют compatible exact-session coordination authority.
Terminal-success order внутри boundary:

```text
second recheck
→ handoff completion durable write
→ terminal snapshot
→ terminal session
→ TERMINAL_COMMITTED
```

If admission allocation to the finalizing cycle linearizes first, accepted
watermark changes before this recheck and terminal aborts. If terminal command
linearizes first, a pre-formed stale non-start admission candidate cannot mutate
terminal state and must be reclassified.

Claim-first legitimate emission attempt становится `DELIVERING` до terminal
authority; terminal-first запрещает новый old-cycle READY claim. Ни final
processing, ни client/network delivery под этим lock не выполняются. Handoff
completion выполняется lock-aware infrastructure command без re-enter того же
session lock.

## Backpressure contract

Required config:

```text
max_queued_batches_per_session
max_queued_bytes_per_session
max_batches_per_checkpoint
max_batch_bytes_per_checkpoint
claim_lease_seconds
max_intermediate_messages_per_cycle
min_intermediate_message_interval_seconds
max_intermediate_message_chars
```

Behavior:

- limit violation produces explicit retryable/capacity or semantic policy state;
- committed input remains durable;
- no hidden second cycle fallback;
- no unbounded in-memory buffering;
- diagnostics expose counts/age, not raw content;
- emission READY outbox listing is bounded and route-filtered.

Stale terminal-race reclassification recomputes capacity decision together with
kind/action/target. Terminal-first `START_CYCLE` outcome must not retain stale
old-cycle capacity/projection state.

## Control contract

### `/stop`

- idempotent;
- does not delete messages/state;
- does not promise rollback;
- applies at safe boundary;
- wins before terminal commit when durable control is authoritative;
- running cycle проходит `pause_requested → paused_by_user`;
- waiting/interrupted resumable cycle может быть явно paused без потери вопроса
  или resumability metadata;
- terminal/idle возвращает `no_active_cycle` и не создаёт cycle.

### `/continue`

- resumes same cycle only;
- does not create missing answer for waiting question;
- atomic repository acceptance freezes current accepted input watermark together
  with durable control publication;
- input coordinated before continue is part of initial resume drain;
- input coordinated after continue is not captured by later state reread and waits
  for the next running checkpoint;
- applies every paused addition through frozen target before next meaningful LLM;
- duplicate same source/key preserves original control ID/sequence/frozen target;
- record-first crash retry preserves frozen target and repairs pending watermark;
- rapid preceding durable pause remains visible to reducer even if session status
  projection lagged when continue request began;
- не создаёт fake `input_batch_update` или новый context revision без input;
- после настоящей pause может reacquire in-process execution lease того же
  durable cycle; process-restart reconstruction при этом остаётся IR-8.

### `/reset`

- highest priority;
- advances **durable** session generation exactly once per logical reset command;
- invalidates old queued/control/finalization work;
- IR-6 additionally fences old semantic delivery: `READY → CANCELLED`,
  `DELIVERING → UNKNOWN`;
- prevents old-generation checkpoint or delivery writer from regaining current
  authority;
- preserves immutable audit/retention evidence according to policy;
- mutable session memory очищается только после safe in-process execution lease
  boundary, не под выполняющимся old runner.

IR-5 checkpoint-level suppression сохранён. IR-7 повторно проверяет authoritative
control/input watermarks непосредственно перед handoff completion/terminal
commit, поэтому pause/reset, ставший durable после последнего checkpoint
observation, всё ещё подавляет stale final/question candidate и handoff остаётся
active. Reset generation mismatch fences old finalization/output writer. IR-6
conservative `DELIVERING → UNKNOWN` semantics не ослаблены.

### `/cancel`

Remains InputBatch collection command and is not accepted as AgentCycle stop.

## Emission/delivery contract

IR-6 реализует:

- progress, intermediate, question and final are distinct;
- explicit semantic manager tool only; ordinary progress/`agent_request` не
  auto-promote-ится в AgentEmission;
- runtime-owned exact session/cycle/generation/context revision/tool-call identity;
- persistence before manager-tool success;
- stable tool-call idempotency and changed-semantics conflict;
- linearizable max-count/rate policy and bounded normalized text;
- trusted route from committed input/capability authority, без LLM route override
  и без durable transport secrets;
- `READY → DELIVERING → DELIVERED | FAILED | UNKNOWN`;
- at most one active attempt: same-token retry idempotent, different token fenced;
- `DELIVERED` requires reliable durable receipt with external delivery ref;
- deterministic not-delivered outcome may be `FAILED`;
- potentially-delivered/expired/reset in-flight outcome is `UNKNOWN`;
- UNKNOWN is not blindly requeued/retried;
- intermediate delivery failure does not fail cycle or change WAITING/context;
- question state and question delivery remain separate lifecycle;
- final execution/final OutputBatch delivery remain separate authority;
- exact client instance/session/conversation/thread checks on worker outcome;
- Telegram semantic message is new plain-text message, not progress edit;
- optional reply binding is server-resolved from external receipt and exact scope;
- cross-session/conversation/thread matching external numeric ID cannot spoof bind;
- client adapters escape/render content safely.

IR-7 добавляет shared exact-session emission-claim/terminal ordering, но не
сливает lifecycle AgentEmission и final OutputBatch. Final output claim gate
transport-neutral и основан на durable `TERMINAL_COMMITTED`; normal admitted-run
path additionally verifies matching RuntimeHandoff COMPLETED.

## Recovery contract

On startup before readiness:

- reconcile committed-unadmitted batches;
- repair admission/session watermarks;
- expire/reconcile claims;
- apply generation/control fencing;
- classify active snapshots;
- preserve/reconcile emission READY/UNKNOWN/receipt evidence;
- reconcile finalization/result/output/handoff relations;
- expose resumable/interrupted state;
- do not auto-repeat ambiguous side effects.

Expected state:

```text
running → interrupted
pause_requested → paused or interrupted with pause intent
paused_by_user → paused_by_user
waiting_user → waiting_user
terminal_committed → terminal, no rerun
unknown emission delivery → retain unknown; no blind resend
```

Этот startup-wide contract остаётся IR-8. IR-6 гарантирует durable
repository recreation semantics, expiry→UNKNOWN и no blind READY requeue; IR-7
гарантирует direct retry/repository recreation собственного finalization protocol,
known completed-handoff/incomplete-terminal convergence и live admission-vs-
terminal reclassification. Ни один из этих IR-7 contracts не является startup
reconstruction coordinator.

## Compatibility contract

Existing flows remain valid:

- text-only AUTO input;
- file-first AUTO draft;
- media groups and forwarded batches;
- explicit `/collect`, `/send`, `/cancel`;
- artifact scopes/read/edit/delivery;
- output grouping and receipts;
- progress status relocation;
- session reset observable behavior;
- Web message compatibility endpoint;
- no mandatory PostgreSQL/Redis/new service.

Telegram `/stop` и `/continue` используют exact existing session/thread resolution
и общий Gateway/application control contract. Transport не читает MCP session
state для semantic decision; `/cancel` остаётся ingress collection command.
IR-6 Telegram emission consumer работает рядом с final OutputBatch worker и не
меняет final-output semantic store. IR-7 final-output eligibility остаётся
server-owned и transport-neutral.

## Configuration and examples

Every supported input-runtime setting must be present in canonical config example.
Secrets не добавляются.

Configuration validation rejects:

- non-positive limits/leases;
- per-checkpoint limits above impossible queue bounds where relation required;
- intermediate message size/count below valid minimum;
- incompatible enabled state without storage capability.

Safe config summary may log limits but not paths containing sensitive user data or
runtime records.

## Observability

Canonical events minimum:

```text
input_admitted
input_claimed
input_applying
input_applied
input_requeued
input_admission_failed
pause_requested
cycle_paused
cycle_resumed
control_rejected
context_revision_created
agent_emission_ready
agent_emission_delivered/failed/unknown
finalization_prepared
finalization_aborted
finalization_terminal_committed
input_runtime_recovery_started/completed/failed
```

Each event uses stable IDs and safe structured metadata.

Metrics/diagnostics distinguish:

- ingress committed;
- runtime admitted/handoff;
- inbox applied;
- cycle execution;
- emission/output persistence;
- client delivery.

IR-5 добавил control watermarks/generation в current runtime status projection.
IR-6 добавил transport-neutral emission lifecycle/query foundation. IR-7 добавил
durable finalization/result/output eligibility state, exact handoff relation и
admission-terminal live ordering; полный IR-9 diagnostics/client projection
completion остаётся planned.

## Unit test matrix

### Models/config

- state validation;
- sequence/watermark invariants;
- serialization/migration;
- config examples audit.

### Repositories

- CRUD via command methods;
- CAS conflict;
- atomic replacement;
- duplicate IDs;
- ordering/filtering;
- two instances over same root.

### Admission

- every state decision;
- concurrent commits;
- duplicate batch;
- capacity;
- session/generation mismatch;
- stale non-start candidate after terminal authority rejected pre-write;
- terminal-first same call reclassifies to new `START_CYCLE`;
- admission-first accepted watermark aborts stale finalization;
- no raw Pydantic `ValidationError` in normal terminal race;
- IR-2 IDLE repair/corruption not masked by stale-decision retry.

### Checkpoints

- every checkpoint;
- tool sequence;
- multiple batches;
- compaction/planning/artifact integration;
- persist/mark crash windows.

### Controls

IR-5 deterministic tests cover:

- concurrent monotonic sequence allocation;
- duplicate pause/continue/reset;
- record-first command publication and exact-session pending-watermark repair;
- independent control after record-first crash gets next unique sequence;
- pause/continue state/snapshot effect with later control-marker failure;
- stop during blocked LLM and production complete multi-tool block;
- rapid pause/continue reducer and pause-allocation/continue classification race;
- paused input FIFO/no wake;
- WAITING pause + real queued input + continue drain and no-input WAIT;
- same-cycle continue with several bounded additions;
- deterministic input-before-continue barrier includes input in frozen target;
- deterministic continue-before-input barrier excludes late input until ordinary
  running checkpoint;
- duplicate continue after late input preserves original target;
- continue publication crash/recreation preserves target and sequence and repairs
  pending watermark;
- reset generation/cancellation/stale-writer fencing;
- reset vs terminal checkpoint;
- Telegram production composition, stable source identity and high-priority
  runtime handlers.

### Emissions

IR-6 deterministic tests cover:

- manager schema contains no runtime/route IDs;
- exact scoped runtime context and concurrent-session no-bleed;
- persistence before success and replay after cancellation;
- same-call replay, changed semantics conflict, concurrent same key, distinct calls;
- max chars, empty message, max-per-cycle, fake-clock min interval;
- concurrent policy race cannot exceed configured limit;
- trusted route derivation/sanitization and route-unavailable failure;
- exact context revision; no revision/WAITING mutation;
- record durable/index publication failure + repository recreation repair;
- READY survives recreation;
- same-token claim, competing token, lost claim response;
- successful/duplicate durable receipt and changed-receipt conflict;
- deterministic FAILED vs ambiguous UNKNOWN;
- expired claim → UNKNOWN and no automatic requeue;
- exact client/session/cycle/generation/conversation/thread outcome fencing;
- reset READY/DELIVERING semantics and stale-writer fencing;
- already-terminal reject and terminal-before-claim cancellation;
- Telegram new-message/plain-text behavior and external message receipt;
- lost receipt response does not cause a second Telegram send;
- optional reply projection and cross-session/conversation/thread fencing;
- failed intermediate leaves AgentCycle session state RUNNING.

### Finalization/recovery

Corrected IR-7 deterministic tests cover:

- input at pre-processing/PREPARED/result/output/pre-terminal boundaries;
- control/reset at finalization boundaries;
- duplicate control without watermark transition;
- waiting input/control/reset suppression and one question authority;
- final OutputBatch READY pre-terminal claim/outbox fence;
- stale/aborted output never claimable;
- claim-first vs terminal-first AgentEmission ordering;
- network await outside shared coordination;
- PREPARED/result/output/partial-terminal recreation and retry;
- post-terminal input starts new cycle;
- duplicate finalization retry returns same IDs/state;
- handoff completion durable write fault blocks every terminal marker;
- exact durable write order: handoff COMPLETED before terminal snapshot/session,
  TERMINAL_COMMITTED last;
- output worker list/claim during pre-handoff terminal window remains fenced;
- completed handoff + incomplete terminal repository recreation/direct retry keeps
  same token/completed_at/finalization/result/output IDs without LLM/tool replay;
- late input/control at OUTPUT_READY aborts before handoff completion;
- cancellation before completion keeps no terminal authority;
- cancellation after COMPLETED preserves completed handoff and allows direct retry;
- API compatibility completion after terminal commit is idempotent;
- terminal-first stale admission returns valid new-cycle START_CYCLE from the same
  API admission call;
- admission-first durable late input yields ABORTED_NEW_INPUT with handoff still
  HANDED_OFF and stale output fenced.

IR-8 startup order/readiness, global ambiguous side-effect reconciliation,
committed-but-unadmitted startup discovery and startup reconstruction remain
planned.

## Race test matrix

Deterministic barriers covered through corrected IR-7:

```text
commit vs admission
admission vs initial cycle creation
admission optimistic classification vs terminal commit
input vs before-LLM checkpoint
input vs tool-block completion
input vs waiting commit
input vs final processing
input vs output ready
input/control vs second terminal recheck
RuntimeHandoff completion fault vs terminal authority
output worker vs handoff-before-terminal window
completed handoff vs incomplete terminal retry
stop vs LLM/tool/finalization
continue vs pause application
input admission vs continue durable acceptance
reset vs any active/final state
claim expiry vs apply persist
emission duplicate/policy acceptance
emission claim response loss
emission receipt response loss
emission reset while delivering
emission claim vs terminal commit
```

Race tests assert exact state/IDs/watermarks and authoritative ordering, not only
absence of exception.

IR-6 closes duplicate/policy/claim/receipt/expiry/reset/sequential-terminal
windows. Corrected IR-7 closes atomic concurrent emission claim-vs-terminal,
late input/control-vs-terminal/waiting commit, handoff-before-terminal durable
ordering и stale admission-decision-vs-terminal race. Shutdown/startup-wide
recovery races remain IR-8; randomized/full-system repetition remains IR-10.

## Randomized tests

Random operations:

```text
commit additions
claim/apply/requeue
pause/continue/reset
checkpoint/finalization
restart service instance
emission persist/delivery outcomes
```

Each seed must verify global invariants:

- max one active cycle per session;
- no duplicate sequence/ID;
- applied <= accepted;
- no terminal with pending accepted/control;
- no final OutputBatch delivery before terminal commit;
- terminal admitted-run output never exists with matching handoff non-COMPLETED;
- no committed batch gets both old-cycle continuation and new-cycle START
  admission from one terminal race;
- no blind retry of UNKNOWN semantic emission;
- no protocol-invalid history;
- reset generation fences old work.

Full randomized/restart roast остаётся IR-10 и не является IR-7 evidence.

## Synthetic roast

No real LLM/MCP/network/Telegram.

Use fake:

- controllable LLM barriers;
- read-only/mutating tool outcomes;
- filesystem crash injection;
- delivery sink receipts/unknown;
- clock/lease control;
- restart by rebuilding composition.

Report records:

```text
commit SHA
branch
seed/scenario selectors
baseline counts
race repetitions
gaps
flaky classification
external call count (must be zero)
```

Focused deterministic IR-6/IR-7 tests use fake Telegram/http/network boundaries
and no real LLM/MCP/network. Полная synthetic roast acceptance остаётся IR-10.

## Live Telegram acceptance

Required maintainer evidence:

- addition while visible LLM/tool work;
- multiple message/file additions;
- stop pending/applied UX;
- additions during pause;
- continue same cycle;
- intermediate message while work continues;
- input immediately before final delivery;
- reset while running;
- restart paused/waiting/queued;
- collection/artifact/output regression scenarios.

Live checks verify user-visible messages and backend IDs/statuses. Private content
is not committed to reports.

IR-6 содержит deterministic fake Telegram delivery tests, IR-7 — transport-neutral
final-output/emission/handoff/admission ordering tests, но maintainer live Telegram
acceptance остаётся IR-10.

## Performance/safety gates

- no repository-wide unbounded scan on every checkpoint;
- hot semantic acceptance queries exact cycle/generation rather than application
  filesystem glob semantics;
- short coordination lock duration instrumentable;
- no LLM/tool/delivery under coordination lock;
- bounded queue/drain/history/outbox projections;
- no raw file content copied into inbox/admission record;
- no route secrets copied into AgentEmission;
- no increased duplicate delivery/side effects;
- full current baseline remains within reasonable regression bounds.

Corrected IR-7 terminal recheck использует exact session/cycle/generation/
finalization/handoff authority. Lock-aware handoff completion выполняет только
short durable transition внутри уже удерживаемой session coordination и не
re-enter-ит lock. Admission repository также выполняет только short pre-write
latest-state/recovery validation under the same durable ordering; application
reclassification остаётся storage-neutral и bounded. Final output eligibility
lookup ограничен exact cycle finalization records и не делает repository-wide
scan на каждом checkpoint.

## Documentation gate

Before Ready for review:

- all files reachable from v0.4 index;
- implementation statuses match code/tests;
- current/release-plan/roadmap updated;
- deferred Telegram rewind remains explicitly out of scope;
- v0.5/v0.6 links point to folder README;
- PR description lists exact acceptance evidence and known gaps.

Corrected IR-7 documentation gate обновляет canonical evidence только после
green corrected code gate. PR остаётся draft, потому что IR-8—IR-10 planned.

## Release decision

Update may be declared implemented only when:

```text
mandatory IR-1..IR-10 complete
+ full baseline green
+ deterministic race matrix green
+ restart matrix green
+ synthetic roast green
+ maintainer live Telegram acceptance complete
+ no unresolved severity-blocking gap
```

IR-7 completion alone не переводит общий update из `partial` в `implemented`.
Optional client UX refinements may remain follow-up only if domain contracts,
state consistency and stale-finalization protection are complete.
