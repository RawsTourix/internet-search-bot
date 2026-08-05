---
id: design.v0.4.input-runtime.contracts-acceptance
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: planned
last_reviewed: 2026-08-05
---

# Contracts и acceptance

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

### Intermediate agent message

```text
send_user_message
→ durable emission
→ client delivery intent
→ cycle remains running
```

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

## Protocol integrity

For every prompt-bearing history:

- each assistant tool call has exactly one matching `role=tool` result;
- no user/runtime update appears inside open tool block;
- no orphan tool result;
- runtime-generated input update marked and schema-valid;
- duplicate replay does not append second update;
- compaction output passes existing tool-sequence validation;
- manager emission call/result sequence remains valid.

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

## Ordering contract

Authoritative order within session/cycle is admission sequence, not client
message timestamp or arrival completion.

- contiguous FIFO apply;
- no skip over missing sequence;
- bounded drain preserves remaining order;
- controls have explicit priority but preserve audit sequence;
- terminalization observes all accepted sequences up to current watermark.

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
- wins before terminal commit.

### `/continue`

- resumes same cycle only;
- does not create missing answer for waiting question;
- applies pre-existing paused additions before next meaningful LLM step;
- idempotent when already running.

### `/reset`

- highest priority;
- advances generation;
- invalidates old queued/control/finalization work;
- prevents stale final delivery;
- preserves immutable audit/retention evidence according to policy.

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

- stop/continue/reset transitions;
- rapid command order;
- idempotency;
- finalization races.

### Emissions

- policy/rate/length;
- persistence/delivery;
- duplicate/unknown;
- reply relation;
- no terminal state change.

### Finalization/recovery

- phase matrix;
- result/output reuse;
- stale output cancellation;
- startup order/readiness;
- ambiguous side-effect policy.

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
reset vs any active/final state
claim expiry vs apply persist
shutdown vs queued/claimed/applying
```

Race tests assert state/IDs, not only absence of exception.

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

Optional client UX refinements may remain follow-up only if domain contracts,
state consistency and stale-finalization protection are complete.
