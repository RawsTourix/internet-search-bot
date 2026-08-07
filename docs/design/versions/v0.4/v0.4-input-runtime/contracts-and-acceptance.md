---
id: design.v0.4.input-runtime.contracts-acceptance
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-07
---

# Contracts и acceptance

## Статус реализации

Observable contracts IR-1—IR-5 реализованы. IR-6—IR-10 остаются planned, поэтому
общий `v0.4-input-runtime` остаётся `partial`.

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
transition реализовано, но это **не** IR-7 durable finalization barrier: late race
`последний checkpoint → новый control/input → terminal commit` остаётся IR-7.
Startup reconstruction/reconciliation остаётся IR-8; полная
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
send_user_message
→ durable emission
→ client delivery intent
→ cycle remains running
```

Этот contract остаётся IR-6 и не считается реализованным IR-5.

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

IR-4/IR-5 реализуют checkpoint-level suppression для уже наблюдаемого input/control.
Полный durable terminal commit contract, включая late race после последнего
checkpoint recheck, остаётся IR-7.

## Protocol integrity

For every prompt-bearing history:

- each assistant tool call has exactly one matching `role=tool` result;
- no user/runtime update appears inside open tool block;
- no orphan tool result;
- runtime-generated input update marked and schema-valid;
- duplicate replay does not append second update;
- compaction output passes existing tool-sequence validation;
- manager emission call/result sequence remains valid.

IR-5 stop не force-cancel arbitrary LLM/tool await. Stop during LLM применяется
после завершения bounded attempt и до следующего tool/LLM block. Stop внутри
assistant multi-tool block применяется только после всех matching `role=tool`
results, сохраняя protocol-valid history. Production-loop test удерживает второй
tool call, принимает pause, завершает весь assistant block и подтверждает pause на
`CP-AFTER-TOOL-BLOCK` до следующего LLM.

## Persistence contract

- committed input is never deleted because admission temporarily failed;
- every applied batch has admission and context revision evidence;
- session/cycle watermarks are monotonic within generation;
- reset advances generation and fences old work;
- pause/wait/interrupted snapshot survives repository recreation;
- final output is not delivery-claimable before terminal commit;
- result/output persistence can recover without rerunning full cycle;
- repository writes are atomic per record;
- cross-record crash windows have deterministic reconciliation.

IR-5 реализует retry/idempotent repair для control publication/application
crash windows. Before any new control allocation exact-session durable records
repair authoritative pending frontier; different durable records with one
sequence are a managed consistency conflict, not a silent winner. For continue,
frozen input target is part of the already durable command and survives a crash
between record publication and pending-watermark state write. Startup-wide
reconstruction/reconciliation этого durable evidence остаётся IR-8.

## Idempotency contract

Stable keys:

```text
input_batch_id → one admission
admission_id → one inbox item
(cycle_id, cycle_sequence) → one logical apply
control idempotency key → one command outcome
emission idempotency key → one semantic emission
finalization_id → one terminal attempt lifecycle
output_batch_id → existing commit-once output contract
```

At-least-once signals/callbacks/claims must not duplicate:

- LLM input update;
- artifact activation/version;
- AgentCycle;
- intermediate message intent;
- final output intent;
- external mutating side effect.

Control idempotency IR-5 связывает stable key ровно с одной command delivery;
повтор возвращает тот же control ID/sequence/outcome, а повтор reset не повышает
generation второй раз. Duplicate continue additionally возвращает тот же frozen
resume target, даже если session accepted watermark уже вырос из-за позднего
input; старую boundary нельзя расширить ретроактивно.

## Ordering contract

Authoritative order within session/cycle is durable coordination/sequence, not
client message timestamp or arrival completion.

- contiguous FIFO apply;
- no skip over missing sequence;
- bounded drain preserves remaining order;
- controls have explicit priority but preserve audit sequence;
- terminalization observes all accepted sequences up to current watermark.

IR-5 control sequence выделяется под короткой session coordination boundary.
`pending_control_sequence` продвигается durable acceptance, а
`applied_control_sequence` — только contiguous terminal control records без
пропуска head command. Effective priority: `reset > pause > continue > input`,
при сохранении всех durable audit records и их sequence.

Continue/input tie-break использует ту же coordination authority: кто первым
получил durable session coordination, тот раньше находится относительно frozen
continue target. State, прочитанный вне этой boundary, не определяет ordering.

## Backpressure contract

Required config:

```text
max_queued_batches_per_session
max_queued_bytes_per_session
max_batches_per_checkpoint
max_batch_bytes_per_checkpoint
claim_lease_seconds
```

Behavior:

- limit violation produces explicit retryable/capacity state;
- committed input remains durable;
- no hidden second cycle fallback;
- no unbounded in-memory buffering;
- diagnostics expose counts/age, not raw content.

## Control contract

### `/stop`

- idempotent;
- does not delete messages/state;
- does not promise rollback;
- applies at safe boundary;
- wins before terminal commit when visible to that checkpoint;
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
- prevents old-generation checkpoint writer from regaining current authority;
- preserves immutable audit/retention evidence according to policy;
- mutable session memory очищается только после safe in-process execution lease
  boundary, не под выполняющимся old runner.

IR-5 подавляет stale terminal candidate, если reset/pause уже наблюдаем на
`CP-BEFORE-TERMINAL-COMMIT`. Гарантия не распространяется на IR-7 late window
после последнего checkpoint и до durable terminal persistence.

### `/cancel`

Remains InputBatch collection command and is not accepted as AgentCycle stop.

## Emission/delivery contract

- progress, intermediate, question and final are distinct;
- canonical runtime state independent from Telegram/Web rendering;
- intermediate delivery failure does not fail cycle;
- question state and question delivery observable separately;
- final execution success and final delivery success observable separately;
- unknown delivery not blind-retried where duplication possible;
- LLM cannot select arbitrary client route;
- client adapters escape/render content safely.

Durable semantic `AgentEmission`/delivery lifecycle остаётся IR-6. IR-5 не
объявляет этот раздел implemented.

## Recovery contract

On startup before readiness:

- reconcile committed-unadmitted batches;
- repair admission/session watermarks;
- expire/reconcile claims;
- apply generation/control fencing;
- classify active snapshots;
- reconcile finalization/result/output;
- expose resumable/interrupted state;
- do not auto-repeat ambiguous side effects.

Expected state:

```text
running → interrupted
pause_requested → paused or interrupted with pause intent
paused_by_user → paused_by_user
waiting_user → waiting_user
terminal_committed → terminal, no rerun
```

Этот startup-wide contract остаётся IR-8. IR-5 реализует только retry/idempotent
repair, необходимый текущей command delivery/application, и не выполняет startup
reconstruction после process death. Exact-session control frontier repair,
необходимый для record-first IR-5 publication windows, не считается полной IR-8
corruption/startup matrix.

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
- runtime admitted;
- inbox applied;
- cycle execution;
- emission/output persistence;
- client delivery.

IR-5 добавил control watermarks/generation в current runtime status projection;
полный IR-9 diagnostics/client projection completion остаётся planned.

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
- session/generation mismatch.

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

- policy/rate/length;
- persistence/delivery;
- duplicate/unknown;
- reply relation;
- no terminal state change.

Planned for IR-6.

### Finalization/recovery

- phase matrix;
- result/output reuse;
- stale output cancellation;
- startup order/readiness;
- ambiguous side-effect policy.

Planned for IR-7/IR-8 except checkpoint-level stale candidate suppression already
covered by IR-4/IR-5.

## Race test matrix

Deterministic barriers must cover:

```text
commit vs admission
admission vs initial cycle creation
input vs before-LLM checkpoint
input vs tool-block completion
input vs waiting commit
input vs final processing
input vs output ready
input vs terminal commit
stop vs LLM/tool/finalization
continue vs pause application
input admission vs continue durable acceptance
reset vs any active/final state
claim expiry vs apply persist
shutdown vs queued/claimed/applying
```

Race tests assert state/IDs and authoritative ordering, not only absence of
exception.

IR-5 закрывает control races на доступной checkpoint/acceptance boundary,
включая atomic continue target ordering. Late input/control-vs-terminal-commit
race после последнего checkpoint остаётся IR-7.

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
- no delivery before terminal commit;
- no protocol-invalid history;
- reset generation fences old work.

Full randomized/restart roast остаётся IR-10 и не является IR-5 evidence.

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

Полная synthetic roast acceptance остаётся IR-10.

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

IR-5 содержит deterministic Telegram adapter/composition tests, но maintainer live
Telegram acceptance остаётся IR-10.

## Performance/safety gates

- no repository-wide unbounded scan on every checkpoint;
- short coordination lock duration instrumentable;
- no LLM/tool/delivery under coordination lock;
- bounded queue/drain/history projections;
- no raw file content copied into inbox/admission record;
- no increased duplicate delivery/side effects;
- full current baseline remains within reasonable regression bounds.

## Documentation gate

Before Ready for review:

- all files reachable from v0.4 index;
- implementation statuses match code/tests;
- current/release-plan/roadmap updated;
- deferred Telegram rewind remains explicitly out of scope;
- v0.5/v0.6 links point to folder README;
- PR description lists exact acceptance evidence and known gaps.

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

IR-5 completion alone не переводит общий update из `partial` в `implemented`.
Optional client UX refinements may remain follow-up only if domain contracts,
state consistency and stale-finalization protection are complete.
