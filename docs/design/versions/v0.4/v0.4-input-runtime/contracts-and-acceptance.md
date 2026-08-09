---
id: design.v0.4.input-runtime.contracts-acceptance
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-09
---

# Contracts и acceptance

## Current implementation status

Observable stage status:

- IR-1 — implemented;
- IR-2 — implemented;
- IR-3 — implemented;
- IR-4 — implemented;
- IR-5 — implemented;
- IR-6 — implemented;
- IR-7 — implemented;
- IR-8 — implemented;
- IR-9 — implemented;
- IR-10 — planned.

`v0.4-input-runtime = partial`: release-final IR-10 acceptance ещё не выполнялась.

Этот документ снова является общей observable acceptance specification для всего
IR-1—IR-9, а не только IR-9 projection document.

## IR-9 implementation evidence

Code/test boundary:

`068f8f6682e7b7b805b60dbb640b53b671cc8565`

Exact GitHub Actions evidence:

- `Validate Input Runtime` #685 — completed / success;
- production compile — success;
- focused IR-8 restart contracts — `49 passed`, `0 failed`;
- focused IR-9 projection contracts — `101 passed`, `0 failed`;
- full `tests/test_input_runtime_*.py + tests/test_artifact_configuration_examples.py`
  — `537 passed`, `0 failed`;
- `Validate v0.4 file artifacts PR` #803 — completed / success;
- workflow token permissions — `Contents: read`, `Metadata: read`.

Relevant stdout не печатает skipped summary; skipped count не заявляется.

## Authority contract

Semantic authority IR-1—IR-8 остаётся durable:

```text
SessionInputRuntimeState
InputAdmissionRecord
CycleInboxItem
SessionControlCommand
ActiveCycleSnapshot
CycleContextRevision
RuntimeHandoffRecord
AgentEmission
CycleFinalizationRecord
OutputBatch
recovery evidence
```

IR-9 query path:

```text
durable authority
→ coherent exact-session read
→ InputRuntimeDiagnosticsService
→ RuntimeStatusSnapshot / RuntimeTimeline
→ client renderer
```

Status/timeline/presentation — `READ / DERIVE only`; они не являются admission,
control, finalization, output-delivery или recovery authority.

---

# Functional contracts

## Initial input

```text
idle session + committed batch
→ exactly one admission, cycle_sequence=0
→ exactly one AgentCycle / RuntimeHandoff relation
→ existing final-output behavior
```

Committed input остаётся durable при временной admission failure и может быть
reconciled без создания второй semantic relation.

## Addition during active cycle

```text
running/active cycle + committed batch
→ no second process_query()/AgentCycle
→ one durable admission + FIFO inbox relation
→ accepted watermark advances
→ safe checkpoint apply
→ same cycle continues
```

Transport acknowledgement не является proof of apply.

## Multiple additions / FIFO

```text
ibat_2, ibat_3, ibat_4
→ cycle_sequence 1,2,3
→ contiguous ordered apply
→ bounded ranges preserve exact batch boundaries
→ no skip over missing sequence
```

Checkpoint freezes accepted-at-entry watermark. Input admitted after entry waits
for next safe checkpoint and cannot retroactively expand the current range.

## WAITING continuation

Before WAITING commit:

```text
candidate WAITING + already durable input/control
→ stale question suppressed
→ apply/reduce authority
→ cycle continues/pauses/resets as required
```

After WAITING commit:

```text
WAITING_USER + real committed reply
→ RESUME_WAITING admission
→ same cycle/context/question lineage
→ common FIFO CP-RESUME
```

Earlier queued additions cannot be bypassed. `/continue` without real answer does
not synthesize input and returns `still_waiting_for_input` semantics.

IR-7 late waiting barrier:

```text
candidate question
→ CP-BEFORE-WAITING
→ exact input/control recheck
→ one durable waiting question authority
→ WAITING_USER
```

## Admission ↔ terminal commit

Durable repository coordination is tie-break, not optimistic application read,
wall clock or task creation.

Admission first:

```text
stale-looking finalizing cycle A
→ admission allocation to A persists
→ accepted watermark advances
→ terminal second recheck sees accepted > applied
→ ABORTED_NEW_INPUT
→ RuntimeHandoff remains HANDED_OFF
→ A continues
```

Terminal first:

```text
terminal command wins durable coordination
→ RuntimeHandoff COMPLETED
→ terminal snapshot/session
→ TERMINAL_COMMITTED
→ stale CONTINUE_RUNNING(A) candidate reaches admission repository
→ dedicated stale-decision conflict before writes
→ same admission call re-reads latest authority
→ START_CYCLE(B), cycle_sequence=0
```

Before reclassification there is no old-cycle admission/index/inbox/watermark
mutation for the late batch. Same `input_batch_id` ends with one admission.
Arbitrary corruption/consistency errors are not silently retried.

## Pause / continue / reset

### Stop

```text
/stop accepted
→ pause_requested
→ current bounded atomic block finishes protocol-valid
→ safe checkpoint reducer
→ PAUSED_BY_USER
```

Acceptance must distinguish command accepted from actually paused. Stop does not
delete history or promise rollback of already-started external work.

### Paused input

Input while pause requested/paused is durable FIFO, advances accepted frontier and
does not implicitly continue or wake a new runner.

### Continue

Continue resumes same cycle only. Durable command includes frozen resume target:

```text
input coordinated before continue
→ included in frozen target
→ drained through CP-RESUME before resumed LLM

continue coordinated before input
→ later input excluded from target
→ waits for ordinary running checkpoint
```

Duplicate same source/idempotency key returns same command ID/sequence/target.
Record-first crash cannot widen/recompute the frozen target.

### Reset

Reset advances durable generation exactly once per logical command, fences old
admission/inbox/control/snapshot/finalization/emission work and synchronizes
process-local coordinator after durable authority. Already-advanced partial reset
is converged by IR-8 without second increment.

Checkpoint reducer priority:

```text
reset > pause > continue > ordinary input
```

## Intermediate semantic AgentEmission

```text
assistant tool_call(send_user_message)
→ runtime-owned exact execution context
→ trusted route + semantic policy
→ durable AgentEmission READY
→ matching role=tool result
→ AgentCycle continues
→ independent delivery lifecycle
```

LLM cannot set session/cycle/generation/context/route/client/reply/emission/
idempotency authority. Runtime-owned tool-call identity makes replay stable.

Delivery lifecycle:

```text
READY
→ DELIVERING
→ DELIVERED | FAILED | UNKNOWN | CANCELLED
```

READY is durable before manager-tool success. `DELIVERED` requires reliable
receipt. Potentially delivered/expired/in-flight reset outcomes are `UNKNOWN`, not
known failure; UNKNOWN is not blindly requeued.

## Finalization

```text
candidate DONE
→ CP-BEFORE-FINAL-PROCESSING
→ exact candidate + handoff authority
→ immutable final processing outside lock
→ PREPARED
→ authoritative recheck
→ FINALIZING
→ RESULT_PERSISTED
→ OutputBatch persisted / OUTPUT_READY
→ second authoritative terminal recheck
IF mismatch:
    ABORTED_NEW_INPUT | ABORTED_CONTROL
    RuntimeHandoff remains HANDED_OFF
    stale output fenced
ELSE:
    RuntimeHandoff COMPLETED
    → terminal ActiveCycleSnapshot
    → terminal SessionInputRuntimeState
    → TERMINAL_COMMITTED last
    → output claim/delivery eligibility
```

Persisted result or OUTPUT_READY is not terminal authority. Successful terminal
projection/delivery requires matching exact handoff/finalization/output evidence.

---

# Protocol integrity

For every prompt-bearing history:

- every assistant tool call has exactly one matching `role=tool` result;
- no user/runtime insertion occurs inside an open assistant tool block;
- no orphan `role=tool` result exists;
- runtime-generated input update is marked/schema-valid;
- duplicate replay does not append a second semantic input update;
- compaction keeps tool/message protocol valid;
- `send_user_message` remains a normal manager tool call + one matching tool
  result;
- runtime does not insert a second assistant message containing the same semantic
  emission text.

IR-5 stop waits until the bounded LLM attempt or complete multi-tool assistant
block reaches protocol-safe boundary. It does not pause between matching tool
results.

IR-6 emission persistence itself is not a checkpoint and does not permit input or
control insertion inside the open tool block.

IR-7 waiting/finalization suppression does not synthesize unmatched tool results.
IR-8 validates stored message/tool protocol before installing a fresh
`ActiveAgentCycle`; invalid snapshot is not converted into a guessed partial
context.

---

# Persistence contract

Mandatory durable invariants:

- committed input is not deleted because admission temporarily failed;
- IR-8 startup scans all durable committed batches without admission, not only the
  current `commit_ready_drafts()` result;
- live admission-vs-terminal race resolves inside the same admission call rather
  than intentionally leaving committed-but-unadmitted work;
- stale optimistic admission decision is rejected before admission/index/inbox/
  session-watermark writes;
- one committed `input_batch_id` → exactly one durable admission relation;
- existing admission with missing inbox repairs one relation without new sequence;
- every applied batch has admission + context revision/snapshot evidence;
- accepted/applied session/cycle watermarks are monotonic within generation;
- reset advances generation exactly once and fences old work;
- partial reset with already-advanced generation converges before generic stale
  snapshot validation and without second increment;
- PAUSED/WAITING/interrupted safe snapshot survives repository recreation;
- exact terminal finalization binds to exact admitted RuntimeHandoff identity;
- final OutputBatch is not claimable before terminal authority;
- normal `TERMINAL_COMMITTED` requires matching handoff `COMPLETED` and output
  evidence;
- RuntimeHandoff completion failure cannot be masked by terminal snapshot/session
  or finalization marker;
- result/output partial persistence can recover without whole-cycle rerun;
- per-record filesystem writes are atomic and derived indexes are repairable;
- cross-record crash windows have deterministic reconciliation;
- AgentEmission READY is durable before manager-tool success;
- emission persistence does not create context revision;
- delivery receipt is authority for DELIVERED and optional reply binding;
- `UNKNOWN` never automatically becomes `READY`.

Record-first publication is required where a durable intent precedes derived
indexes/frontiers. Missing derived publication may repair from authoritative
record; contradictory immutable authorities produce managed failure, not an
arbitrary winner.

---

# Idempotency contract

Stable logical mappings:

```text
input_batch_id
→ one durable admission

admission_id
→ one inbox relation

(cycle_id, cycle_sequence)
→ one logical input apply

admission_id + handoff_token
→ one exact runtime invocation handoff

control idempotency key
→ one durable SessionControlCommand outcome

cycle + generation + assistant tool_call_id
→ one semantic AgentEmission

claim token
→ one emission delivery attempt

receipt identity
→ one external delivery relation

finalization_id
→ one finalization lifecycle

output_batch_id
→ one output aggregate / existing commit-once identity
```

At-least-once transport signals/retries/claims must not duplicate:

- AgentCycle;
- LLM input update/context revision;
- artifact activation/version;
- RuntimeHandoff invocation/completion;
- recovered runner owner;
- control command or reset generation increment;
- semantic intermediate intent;
- client delivery after acknowledged/ambiguous attempt;
- finalization/result/final output intent;
- external mutating side effect.

Control duplicate preserves sequence and frozen resume target. Emission same logical
tool call returns same emission after recreation/cancellation. Same claim token
returns same attempt; different token conflicts. Same receipt confirms same
relation; changed receipt conflicts. Finalization retry reuses same logical IDs and
repeated handoff completion does not allocate new token/completed_at.

---

# Crash and cancellation contract

Canonical crash windows and required convergence:

## Authoritative record persisted / index missing

Repair derived identity/index from exact durable record. Do not create a second
semantic record or pick competing identity by timestamp.

## Claim response lost

Retry same claim token returns same `DELIVERING` attempt. No second client attempt
is started solely because claim HTTP response was lost.

## Apply snapshot persisted / markers missing

If `CycleContextRevision` + `ActiveCycleSnapshot` watermark proves applied range,
recovery completes only inbox/admission markers. No duplicate
`input_batch_update`, revision or semantic apply.

## RuntimeHandoff HANDED_OFF crash

Without stronger completion evidence, restart classifies conservatively as
interrupted/ambiguous and does not automatically repeat `process_query()` or
unknown external side effects.

## RuntimeHandoff COMPLETED / terminal incomplete

Known finalization direct retry or IR-8 discovery preserves exact handoff token /
`completed_at`, finalization ID, result ref and output ID and performs only local
terminal convergence. No LLM/MCP/tool replay; completed handoff is never downgraded.

## Output READY / finalization incomplete

OUTPUT_READY alone remains non-deliverable. Retry validates current authority,
then either completes same finalization locally or aborts stale output on late
input/control/generation mismatch.

## Reset generation advanced / cleanup incomplete

Existing durable reset generation wins. IR-8 finishes same cleanup first, no
second generation increment, and fences remaining old-generation work.

## Delivery ambiguous / receipt missing

Started transport side effect without reliable receipt becomes/remains `UNKNOWN`.
No blind duplicate send or automatic READY rearm.

## Cancellation

- before RuntimeHandoff marker: retryable admission;
- after HANDED_OFF: preserve ambiguity/no-rerun evidence;
- after handoff COMPLETED: never downgrade completed relation; finish/recover local
  convergence only;
- during checkpoint claim/apply: use pre-snapshot requeue or post-snapshot marker
  reconciliation according to durable authority;
- after AgentEmission READY before tool result: durable intent survives and replay
  returns same emission;
- shutdown cancellation owns tracked tasks and waits cancellation-safe cleanup
  before MCP teardown.

---

# Ordering and coordination contract

Authoritative order is durable sequence/coordination, never client timestamp,
wall clock or asyncio scheduling.

- admission sequence is monotonic;
- FIFO apply is contiguous, no skip over missing sequence;
- bounded drain preserves remainder order;
- accepted-at-entry freezes current checkpoint target;
- controls retain audit sequence while reducer priority is explicit;
- continue/input ordering is exact-session durable coordination around frozen
  target;
- terminalization observes authoritative accepted/applied input + control
  frontiers;
- successful terminal marker cannot precede exact handoff completion;
- admission allocation vs terminal commit is repository coordination;
- emission READY claim vs terminal commit uses compatible exact-session ordering;
- existing terminal marker preflight precedes projection repair at startup;
- safe recovered runner ownership installs after MCP connect and before READY.

Network/LLM/tool/final processing outside short authoritative write portions are
not executed under session coordination.

---

# Backpressure contract

Configured limits include queue count/bytes, checkpoint batch count/bytes, claim
lease and intermediate message count/interval/chars.

Limit violation yields explicit capacity/policy outcome; committed input remains
durable; there is no hidden second-cycle fallback or unbounded in-memory buffer.
Stale terminal-race reclassification recomputes target/capacity rather than
carrying old-cycle result into new-cycle START.

IR-9 diagnostics may expose safe counts/ages but not raw content.

---

# Control acceptance contract

## `/stop`

- idempotent source identity;
- no history/state deletion;
- no rollback promise;
- applies at safe boundary;
- `pause_requested != PAUSED_BY_USER`;
- waiting/interrupted resumable state can be paused without losing question /
  recovery metadata;
- terminal/idle produces no active-cycle pause.

## `/continue`

- same cycle only;
- does not invent waiting answer;
- command publication atomically freezes accepted input frontier;
- input before continue is drained in initial resume target;
- input after continue waits next running checkpoint;
- duplicate preserves original ID/sequence/target;
- record-first crash retry preserves target;
- no input means no fake input update/context revision;
- fresh-process PAUSED uses same durable contract after IR-8 rehydration.

## `/reset`

- highest reducer priority;
- durable generation advances exactly once;
- old queued/control/finalization/emission work fenced;
- READY emission → CANCELLED, already-started DELIVERING → UNKNOWN;
- old-generation writers cannot regain current authority;
- mutable session state clears only after safe execution lease boundary;
- partial reset converges at startup without second increment.

`/cancel` remains ingress collection command, not AgentCycle stop.

---

# AgentEmission / delivery contract

IR-6 observable acceptance:

- progress, intermediate, question and final remain distinct lifecycles;
- manager schema semantic-only; runtime owns provenance/route/idempotency;
- READY durable before manager-tool success;
- max-count/min-interval/max-chars policy is linearizable;
- trusted route derives from committed input/capability authority and stores no
  secrets;
- lifecycle is `READY → DELIVERING → DELIVERED | FAILED | UNKNOWN | CANCELLED`;
- same-token claim retry idempotent; competing token fenced;
- DELIVERED requires durable receipt/external ref;
- deterministic known-not-delivered may be FAILED;
- potentially delivered / expired / reset in-flight becomes UNKNOWN;
- UNKNOWN not blindly requeued;
- intermediate delivery failure does not terminate AgentCycle or mutate WAITING /
  context revision;
- Telegram semantic emission is a separate plain-text message, not progress edit;
- reply binding is server-resolved by exact external delivery scope;
- IR-7 terminal ordering does not merge AgentEmission with final OutputBatch;
- IR-8 retains READY without startup send and preserves UNKNOWN ambiguity.

---

# Finalization acceptance contract

- every durable input/control before final authority suppresses stale finalization;
- WAITING has repeated input/control barrier before question authority;
- PREPARED / RESULT_PERSISTED / OUTPUT_READY are recoverable non-terminal states;
- final terminal recheck occurs before RuntimeHandoff completion;
- successful handoff completion occurs before terminal snapshot/session/marker;
- `TERMINAL_COMMITTED` is written last as delivery fence;
- completion failure exposes no terminal authority;
- final output not claimable before terminal commit;
- stale/aborted final output never deliverable;
- direct retry of completed-handoff/incomplete-terminal uses same IDs and no
  LLM/tool replay;
- admission-first late input aborts stale finalization;
- terminal-first stale admission reclassifies same committed batch to new cycle in
  same API call;
- emission claim-first vs terminal-first produces one deterministic ordering;
- no client/network await under terminal/session coordination.

---

# Recovery contract — IR-8

Ordinary runtime is blocked until:

```text
RECOVERING
→ deterministic durable reconciliation
→ MCP connect
→ ActiveAgentCycle/reservation/task installation when safe
→ READY
```

Startup recovery includes:

- all committed-but-unadmitted batches;
- missing inbox/session/admission frontier repair;
- expired CLAIMED/APPLYING reconciliation from snapshot authority;
- pending control/reset generation convergence;
- active snapshot/context/tool-protocol/dependency validation;
- RuntimeHandoff and partial finalization classification;
- READY/DELIVERING/UNKNOWN emission recovery without send;
- PAUSED/WAITING same-cycle reconstruction;
- safe RUNNING plan and exact recovered reservation owner;
- strict corruption failure rather than semantic guessing.

Terminal preflight:

```text
TERMINAL_COMMITTED
+ matching RuntimeHandoff COMPLETED
+ matching final OutputBatch
= valid existing terminal authority
```

Missing/contradictory immutable evidence leaves readiness FAILED. Safe
`OUTPUT_READY + COMPLETED + marker missing` may converge locally with same IDs and
no AgentCycle replay.

Shutdown:

```text
READY/RECOVERING
→ STOPPING
→ reject new runner starts
→ cancel/await tracked tasks
→ cancellation-safe durable cleanup
→ retain durable pending state
→ MCP cleanup
→ STOPPED
```

---

# IR-9 diagnostics and client acceptance

## Coherent status

Filesystem diagnostics uses short exact-session coordination. A race with
admission/control/terminal may return coherent before or after view, never torn
watermark/count combinations from different snapshots.

`RuntimeStatusSnapshot` separates process readiness from durable session status and
contains only bounded safe structured metadata. Current counts filter to current
generation.

## Privacy

Generic status/timeline does not expose raw user text, LLM/system messages,
prompts, tool arguments/results, file/artifact contents, tokens/API keys, callback
auth, arbitrary route metadata, filesystem paths or raw traceback.

Cross-session isolation is mandatory.

## Timeline

`RuntimeTimeline`:

- default limit `20`;
- maximum limit `100`;
- deterministic same-timestamp display tie-break;
- per-stream sequence/identity retained;
- cross-stream order is display-only;
- no invented global semantic sequence;
- no new durable event store/global event bus/WebSocket log.

## Addendum projection

```text
input_addendum_admitted
input_addendum_applying
input_addendum_applied
input_addendum_cancelled
input_addendum_failed
```

Queued acknowledgement never promises already applied. Applied presentation event
is transient, exact-input-batch scoped and does not create AgentEmission or mutate
LLM/admission authority.

## Control/recovery projection

```text
pause accepted != PAUSED_BY_USER
continue accepted != resumed != still_waiting_for_input
INTERRUPTED != AMBIGUOUS
UNKNOWN != FAILED
```

Ambiguous external work is not represented as definitely failed and diagnostics
does not initiate replay.

## Finalization/emission projection

Generic diagnostics may expose safe lifecycle state/IDs/counts/reason codes but no
raw semantic message/route secrets. Session DONE alone is not terminal client
authority.

## Telegram `/status`

Read-only, trusted-session, high-priority consumer shared DTO. It creates no
CommittedInputBatch/control sequence/AgentEmission, wakes no runner and changes no
generation/watermark. Existing collection semantic FIFO bypass is preserved.

Presentation failure does not mutate semantic state:

- deterministic not-editable → one bounded fallback send;
- ambiguous edit/send → no blind duplicate;
- generation/revision fencing suppresses stale presentation;
- existing progress terminal barrier prevents stale edit winning after terminal.

## Web/API / CLI / localization / config

Implemented structured endpoints:

```text
GET /runtime/status
GET /runtime/timeline
```

Web consumes structured DTO under existing trusted auth/session scope. Standalone
production runtime CLI framework is absent; shared DTO renderer is the supported
CLI-compatible consumer path.

RU/EN IR-9 projection keys/placeholders are synchronized. IR-9 added no new
config/environment fields; existing config example audit remains source of truth
and part of green `537 passed` regression.

---

# Deterministic acceptance matrix

Canonical deterministic coverage through IR-9 includes:

## Models/repositories

- model/state/sequence/watermark validation;
- serialization/config examples;
- atomic replacement and CAS;
- duplicate IDs and index repair;
- repository recreation;
- cross-session identity conflicts;
- claim token/lease fencing.

## Admission/checkpoints

- initial input and active additions;
- duplicate/capacity/missing inbox;
- no parallel AgentCycle;
- accepted-at-entry and bounded FIFO;
- every checkpoint including tool-block integrity;
- snapshot-first marker repair;
- WAITING common FIFO;
- cancellation-safe apply.

## Controls

- monotonic sequence/idempotency;
- record-first pending-frontier repair;
- blocked LLM/multi-tool stop;
- paused FIFO/no wake;
- input-before/after-continue frozen target barriers;
- duplicate/crash target preservation;
- WAITING with/without reply;
- reset generation/fencing/partial recovery.

## Emissions

- runtime-owned identity/no cross-session bleed;
- READY-before-success;
- policy/count/rate;
- record/index crash;
- cancellation after READY;
- same/competing claim token;
- lost claim/receipt response;
- receipt idempotency;
- FAILED vs UNKNOWN;
- reset/terminal fencing;
- Telegram separate message;
- safe reply binding.

## Finalization/recovery

- input/control at finalization/waiting boundaries;
- handoff completion write fault and exact durable order;
- output claim/outbox fence;
- completed-handoff partial terminal retry;
- admission-first/terminal-first ordering;
- emission claim/terminal ordering;
- committed-unadmitted startup repair;
- CLAIMED/APPLYING recovery;
- PAUSED/WAITING fresh continuation;
- ambiguous no-auto-replay;
- strict terminal preflight;
- partial reset convergence;
- READY/UNKNOWN emission restart;
- recovered reservation ownership;
- controlled corruption and shutdown lifecycle.

## IR-9 focused acceptance

- coherent running/paused/waiting/interrupted/terminal status;
- no semantic mutation from diagnostics;
- cross-session isolation;
- status races with admission/control/terminal;
- addendum lifecycle projections;
- stop/continue distinction;
- interrupted/ambiguous distinction;
- UNKNOWN preservation;
- finalization lifecycle projection;
- bounded deterministic timeline;
- no raw-content/secret leakage;
- Web structured DTO and shared CLI renderer;
- Telegram `/status` RU/EN;
- deterministic edit fallback / ambiguous no duplicate;
- generation/revision/progress fencing;
- localization parity and config-example audit.

Focused IR-9 result on exact code boundary: `101 passed`, `0 failed`.
Full input-runtime/config result: `537 passed`, `0 failed`.

---

# Performance and safety gates

- no repository-wide unbounded scan on every checkpoint/hot path;
- startup-only durable discovery may scan according to recovery adapter policy;
- exact-cycle/current-generation queries on hot semantic paths;
- no LLM/tool/client delivery under session coordination;
- no external send during mandatory recovery;
- bounded queue/drain/outbox/status/timeline projections;
- no raw file content in admission/inbox diagnostics;
- no route secrets in AgentEmission/generic diagnostics;
- no increased duplicate external side effects.

---

# IR-10 boundary

IR-9 deterministic acceptance is not IR-10.

IR-10 remains planned and owns randomized race/restart/corruption repetition,
synthetic whole-system marathon, maintainer live Telegram acceptance and
release-final real-service acceptance/report where specified.

This restoration pass does not implement or execute those gates.

## Deferred outside current update

- Telegram edited-message history rewind;
- PostgreSQL / SQLAlchemy / Alembic;
- Redis/distributed workers/leases;
- scheduler / `AgentRun` / `TaskRun`;
- parallel branches / fork-join;
- new durable global event bus.

## Definition of current completion

IR-9 may be stated implemented/validated when factual evidence remains:

```text
code boundary == 068f8f6682e7b7b805b60dbb640b53b671cc8565
focused IR-8 == 49 passed / 0 failed
focused IR-9 == 101 passed / 0 failed
full input-runtime/config == 537 passed / 0 failed
production compile == success
code workflows #685 and #803 == completed/success
IR-1—IR-8 accepted contracts remain canonically documented
IR-9 diagnostics/projections remain canonically documented
all current status says IR-9 implemented / IR-10 planned
a real final documentation HEAD exists
both workflows on that final documentation HEAD complete successfully
PR #6 evidence matches actual history and remains draft/open/unmerged
IR-10 is not started
```

Exact restoration documentation SHA and workflow numbers are recorded only after
GitHub actually creates that SHA and completes the corresponding runs.
