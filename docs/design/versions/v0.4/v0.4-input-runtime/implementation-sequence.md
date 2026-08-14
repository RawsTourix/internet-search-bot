---
id: design.v0.4.input-runtime.sequence
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-09
---

# Последовательность реализации v0.4-input-runtime

## Current stage status

- `IR-1 — Domain models, config и repository ports`: implemented;
- `IR-2 — Filesystem repositories и coordination service`: implemented;
- `IR-3 — Admission service и initial-cycle integration`: implemented;
- `IR-4 — Active snapshot, checkpoints и CycleInputApplier`: implemented;
- `IR-5 — Durable control plane /stop, /continue, /reset`: implemented;
- `IR-6 — AgentEmission и intermediate messages`: implemented;
- `IR-7 — Finalization barrier`: implemented;
- `IR-8 — Startup recovery и lifecycle`: implemented;
- `IR-9 — Client projections, diagnostics и configuration examples`: implemented;
- `IR-10 — Full randomized/restart/synthetic/live acceptance`: planned.

`v0.4-input-runtime = partial` до IR-10.

## Dependency order

```text
IR-1 domain/ports
→ IR-2 durable filesystem repositories
→ IR-3 admission + one active cycle
→ IR-4 safe checkpoints/context revisions
→ IR-5 durable controls
→ IR-6 semantic AgentEmission
→ IR-7 finalization/terminal authority
→ IR-8 startup recovery/readiness
→ IR-9 safe client projections/diagnostics
→ IR-10 release-final acceptance
```

IR-9 читает уже реализованные IR-1—IR-8 authorities и не заменяет их. Sections
ниже сохраняют принятые implementation boundaries каждого завершённого stage.

---

# IR-1 — Domain models, config и repository ports

## Status

Implemented. IR-1 создал storage-neutral foundation; production integration была
выполнена последующими stages.

## Goal

Создать независимый `src/input_runtime/` package с pure durable models, errors,
configuration и command-oriented repository Protocols без зависимости
application layer от filesystem, Telegram/FastAPI или MCP concrete types.

## Domain models

Canonical model surface включает:

```text
SessionInputRuntimeState
InputAdmissionRecord
InputAdmissionOutcome
CycleInboxItem
ClaimedInboxRange
RuntimeHandoffRecord
SessionControlCommand
ControlOutcome
CycleContextRevision
AgentEmission
CycleFinalizationRecord
CheckpointOutcome
```

Stable IDs/enums определяются один раз. Sequence/watermark/generation relations
валидируются на model boundary. Timestamp fields timezone-aware; terminal handoff
timestamp не может предшествовать `handed_off_at`.

## Repository ports

Application-facing ports:

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

`RuntimeHandoffRepository` имеет command-oriented surface (`get`, `begin`,
`complete`, `mark_ambiguous`). `SessionControlRepository` предоставляет
authority-owning `accept_continue(...)`, чтобы cycle/generation/frozen target
фиксировались атомарно внутри repository coordination.

Ports не экспортируют generic `save(dict)`, filesystem path, lock или serialization
helper. Application semantics должны переноситься на future transactional adapter.

## Configuration

IR-1 закрепил input-runtime limits, впоследствии реально использованные stages:

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

Defaults conservative для current single-process profile.

## Deterministic tests

- model/state/ID validation;
- invalid sequence/watermark rejection;
- serialization round-trip;
- config example coverage;
- command-oriented Protocol contracts;
- no Telegram/FastAPI/MCP concrete imports in domain/interfaces.

## Definition of Done

- package imports without side effects;
- models/ports storage-neutral;
- ports пригодны для filesystem и future SQL adapter semantics;
- no production behavior change within IR-1 itself.

## Historical out-of-scope

IR-1 не подключал stores к API, checkpoints или transport handlers. Эти пункты
были stage boundary, а не current missing work: IR-2—IR-9 реализованы отдельно.

---

# IR-2 — Filesystem repositories и coordination service

## Status

Implemented: durable filesystem adapters, atomic/restart contracts, identity and
index repair, bounded claims и exact-session coordination.

## Goal / ownership

Infrastructure lives in `src/input_runtime/filesystem.py`, `_filesystem_*`,
`coordination.py`, serialization/factory modules. Application services продолжают
зависеть только от IR-1 ports.

## Atomic persistence contract

Per-record write использует crash-safe temporary write / flush / replace semantics
со storage helper policy. Durable authoritative record публикуется раньше derived
indexes/pointers:

```text
write authoritative record
→ fsync/replace according to storage policy
→ publish identity/index relations
```

Если record durable, а index publication потерян, retry/recovery rebuilds relation
from exact durable identity; новый semantic record не создаётся. Dangling index
без authoritative record удаляется/repair-ится deterministic policy. Два
contradictory authoritative records не получают silent winner.

## Coordination and ordering

- normalized root identity + exact session define coordination scope;
- fixed lock order: `root identity → session`;
- one short bounded/ref-counted session coordination boundary;
- no LLM/tool/client/network await under it;
- monotonic deterministic session/cycle sequence allocation;
- compare-and-swap revision semantics where applicable;
- cross-session ownership collision is managed consistency conflict, not last-write
  wins;
- user-controlled IDs are never raw path segments.

## Claim fencing

Inbox claims use contiguous ordered ranges, generation and durable claim token /
lease. Core operations support claim, applying, applied, safe requeue/expiry and
generation cancellation. Stale token cannot complete another attempt.

IR-4 later made snapshot watermark semantic apply authority; IR-8 later added
startup-wide deterministic claim/identity reconciliation. Full randomized
corruption permutations remain IR-10.

## Deterministic tests

- concurrent sequence allocation;
- duplicate admission/identity creation;
- claim conflict/expiry/stale token;
- atomic record/index crash windows;
- dangling/missing derived pointers;
- recreation of repository bundles on same root;
- cross-session/ambiguous ownership failure;
- path traversal rejection;
- RuntimeHandoff marker persistence across recreation.

## Definition of Done

Repositories survive process recreation; ordered reads deterministic; no duplicate
identity/sequence under covered races; partial metadata publication repairs safely;
application code does not import filesystem layout/locks.

## Out-of-scope

No long global transaction emulation, PostgreSQL/Redis, or repository-wide scan on
every hot path. Broader randomized corruption/restart matrix belongs to IR-10.

---

# IR-3 — Admission service и initial-cycle integration

## Status / evidence

Implemented and hardened on historical final IR-3 HEAD
`c36e4cc38095e15f54f63ae81c29b4829defec1f`.

- `Validate Input Runtime` #84 — success, `198 passed`;
- `Validate v0.4 file artifacts PR` #503 — success.

## Goal

Every durable `CommittedInputBatch` goes through one transport-neutral
`InputAdmissionService`:

```text
CommittedInputBatch
→ one authoritative InputAdmissionRecord
→ one active cycle OR ordered CycleInbox relation
```

Active-cycle addition must not start a second `process_query()`.

## Production boundary

Composition injects committed-batch reader, `InputRuntimeRepositories`, exact
execution coordinator/wakeup port, config and ID/time policies. Application
service does not import filesystem `Path`/lock/layout.

Admission outcomes cover initial start, queued running, queued paused,
`RESUME_WAITING`, duplicate/idempotent relation and capacity-blocked retryable
state.

## Initial cycle and active additions

Initial input gets cycle sequence `0`, exact cycle identity and defensive runner
reservation. Active additions get `cycle_sequence > 0`, durable admission + FIFO
inbox relation, advance accepted watermark and signal only exact active cycle.

Count/byte capacity comes from authoritative current-generation admission state,
not merely presence of one inbox file. Missing inbox after record-first crash does
not release capacity; repair recreates exactly one relation with original sequence.

## RuntimeHandoff contract

```text
pre-run resolution
→ RuntimeHandoffRepository.begin(HANDED_OFF)
→ process_query()
→ complete(COMPLETED) OR mark_ambiguous(AMBIGUOUS)
```

- failure before durable handoff marker is retryable;
- after HANDED_OFF, duplicate admission does not invoke runtime again;
- crash/exception/cancellation after handoff boundary is conservative
  interrupted/ambiguous unless stronger durable evidence later proves completion;
- stale handoff token cannot complete another invocation;
- exact-cycle wake cannot wake a newer/different cycle.

IR-7 later clarified successful terminal ordering: final terminal recheck occurs
before exact handoff completion; then terminal snapshot/session/marker may converge.
IR-8 uses handoff evidence during restart and never treats process restart itself as
permission to replay an unknown side effect.

## Cancellation-safe cleanup

Cancellation before marker stays retryable. Post-marker cancellation preserves
ambiguity evidence and no-rerun semantics. Durable cleanup runs in a separate task,
is awaited through shielding, survives repeated cancellation attempts and then
re-raises the original cancellation to caller.

IR-4 later generalized cancellation-safe common claim/apply. A handoff already
durable `COMPLETED` is never downgraded to AMBIGUOUS by later cancellation.

## Deterministic tests / DoD

Covered: idle start, active additions without parallel runner, duplicate/capacity,
missing inbox repair, exact wake, pre/post-handoff failures, cancellation windows,
repository recreation, stale handoff token, no blind rerun.

Done when every production committed batch passes admission, exactly one active
cycle is enforced, additions enter same-cycle FIFO, handoff is storage-neutral and
post-handoff ambiguity never causes blind replay.

## Historical out-of-scope

IR-3 did not yet apply additions to LLM context or own startup recovery; IR-4 and
IR-8 implemented those later. This is historical stage scope, not current missing
functionality.

---

# IR-4 — Active snapshot, checkpoints и CycleInputApplier

## Status / evidence

Implemented on historical final IR-4 code/test HEAD
`1d31b6fbd1d5e88966d3964dc35cf4680f32f522`.

- `Validate Input Runtime` #115 — success, compile success, `241 passed`, `0 failed`;
- `Validate v0.4 file artifacts PR` #518 — success.

## Goal

Apply durable active-cycle additions only at protocol-safe boundaries while
preserving FIFO, one semantic apply and valid assistant/tool history.

## Active snapshot and context

Initial `CP-RESUME` creates `R1` plus durable `ActiveCycleSnapshot`. Snapshot owns
current generation/cycle, original/applied batch identities, accepted/applied
watermarks, active context revision, safe checkpoint and pause/interruption
metadata.

Linear context revisions:

```text
initial batch → R1
range A → R2(parent=R1)
range B → R3(parent=R2)
```

No-op checkpoint creates no revision.

## Protocol-safe checkpoint matrix

Canonical checkpoints:

```text
CP-RESUME
CP-BEFORE-LLM
CP-AFTER-TOOL-BLOCK
CP-BEFORE-WAITING
CP-BEFORE-FINAL-PROCESSING
CP-BEFORE-TERMINAL-COMMIT
CP-AFTER-INTERRUPTION
```

Checkpoint never inserts user/runtime input inside an open assistant tool block
between `assistant.tool_calls` and all matching `role=tool` results.

## Accepted-at-entry watermark and FIFO apply

At checkpoint entry runtime freezes `active_cycle_accepted_through_sequence = N`.
It may apply multiple bounded contiguous ranges through N. Input admitted after
entry does not expand this drain and waits for next checkpoint.

Each applied range:

```text
claim contiguous FIFO range
→ resolve committed batches/artifact refs
→ build exactly one input_batch_update for the range
→ append protocol-valid user/runtime update
→ persist next CycleContextRevision
→ persist ActiveCycleSnapshot + new applied watermark
→ reconcile inbox/admission APPLIED markers
```

Batch boundaries and ordered cycle sequences are retained inside the range.

## Snapshot-first crash reconciliation

Semantic authority order:

```text
persist context revision
→ persist snapshot + applied watermark
→ mark inbox/admission applied
```

If crash occurs after snapshot persistence but before markers, recovery completes
only lagging markers. It must not append a second `input_batch_update`, create a
second context revision or re-execute semantic apply.

## WAITING and cancellation

WAITING reply uses common FIFO `CP-RESUME`; it cannot jump over earlier queued
additions and does not create a separate semantic continuation path.

Claim acquisition/apply are cancellation-safe. Before authoritative snapshot a
claim may requeue/reconcile; after snapshot watermark covers range, cleanup uses
snapshot authority and only finishes markers.

IR-8 fresh-process WAITING reconstruction preserves same cycle/context/question.

## Deterministic tests / DoD

Covered: additions during LLM/tool block/before WAITING, multiple additions,
accepted-at-entry boundary, bounded FIFO, one update/revision per range,
snapshot-persisted/marker-failed window, cancellation, WAITING common FIFO,
compaction/artifact refs, tool protocol integrity, handoff-before-terminal snapshot.

Done when same active cycle consumes input FIFO exactly once at safe checkpoints,
R1/snapshot/context are durable authorities and stale WAITING/final candidates are
suppressed at checkpoint level.

## Historical out-of-scope

No semantic branch classification, scheduler/fork-join, automatic plan reset or
startup-wide reconstruction inside IR-4. Later stages own those boundaries.

---

# IR-5 — Durable control plane `/stop`, `/continue`, `/reset`

## Status / evidence

Implemented and validated on corrected historical code/test HEAD
`0fabe15c6730a4e8db6be8b54ecec2c13ea773c7`.

- `Validate Input Runtime` #219 — success, compile success, `291 passed`, `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #570 — success.

## Goal and authority

`SessionControlCommand` + durable session/snapshot state are semantic authority for
pause/continue/reset. In-process coordinator remains defensive execution plumbing,
not control truth.

Control publication is record-first:

```text
allocate monotonic control sequence under short session coordination
→ persist SessionControlCommand
→ publish identity/index
→ advance pending_control_sequence
→ safe checkpoint reducer applies command
→ contiguous applied_control_sequence advances
```

Stable source idempotency returns same command/sequence/outcome. Record durable /
pending watermark missing is repaired without second command.

## `/stop`

```text
running
→ pause command accepted
→ pause_requested
→ current bounded atomic LLM/tool block finishes protocol-valid
→ control checkpoint
→ PAUSED_BY_USER
```

Stop does not delete history/plan/artifacts/context and does not claim rollback of
already-started external work. In a multi-tool assistant block all matching tool
results are completed before pause at `CP-AFTER-TOOL-BLOCK`.

Input admitted while pause requested/paused remains durable FIFO and does not
implicitly continue the cycle.

## `/continue` and frozen resume target

Continue resumes the same durable `cycle_id` only. Repository `accept_continue`
performs authoritative state read, control-frontier repair and freezes current
accepted watermark under the same exact-session coordination that publishes the
command.

```text
input coordinated before continue
→ included in frozen resume target
→ drained via CP-RESUME before next meaningful LLM

continue coordinated before input
→ later input excluded from frozen target
→ waits for ordinary running checkpoint
```

Wall clock, Telegram arrival order or asyncio task creation are not authority.
Duplicate same-key continue preserves original command ID/sequence/frozen target,
even if later input advanced accepted watermark. Record-first crash preserves that
target and retry only repairs publication markers.

`WAITING_USER` without actual user reply returns `still_waiting_for_input`; continue
cannot synthesize an answer. Real reply included in target drains FIFO then resumes
same cycle.

## `/reset`

Durable session `generation` is authority and advances exactly once per logical
reset. Old admissions/inbox/pending controls/snapshot/finalization/emissions are
cancelled/fenced according to their lifecycle. Stale old-generation checkpoint,
finalization or delivery writer cannot regain current authority.

IR-8 later closes partial-reset crash window: if generation already advanced,
startup completes the same reset cleanup before generic stale-snapshot validation
without incrementing generation again.

## Checkpoint reducer priority

Checkpoint captures pending-control target together with input boundary. Completed
atomic block context persists first; controls reduce through captured target, then
ordinary input may apply.

```text
reset > pause > continue > ordinary input
```

Audit records keep monotonic sequence even when a later continue neutralizes a
not-yet-applied pause.

## Deterministic tests / DoD

Covered: concurrent control allocation/idempotency, record-first crash repair,
blocked LLM/multi-tool stop, rapid pause/continue, paused FIFO/no wake,
input-before/after-continue deterministic barriers, duplicate target preservation,
WAITING with/without reply, reset generation/fencing, stale writer/wake, terminal
race interaction and Telegram composition.

Done when durable watermarks reflect command acceptance/application, stop is
cooperative/protocol-safe, continue is same-cycle with immutable frozen target and
reset generation is exactly-once/fenced.

## Historical out-of-scope

No conversation rewind, `/cancel` reinterpretation, startup scanner or full
randomized corruption matrix in IR-5. IR-6—IR-9 were implemented later; IR-10
remains planned.

---

# IR-6 — AgentEmission и intermediate messages

## Status / evidence

Implemented on historical code/test HEAD
`4447d1bfe487bfd764829e701f274655aa8c3c50`.

- `Validate Input Runtime` #297 — success, compile success, `350 passed`, `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #609 — success.

## Goal and ownership

Create durable semantic intermediate communication independent from transient
progress, question/WAITING and final OutputBatch. Application emission service is
transport/storage-neutral; filesystem command adapter owns policy/claim/receipt
transitions and transport workers use authenticated outbox APIs.

## Manager tool and runtime identity

`send_user_message` schema contains only semantic fields:

```text
message
kind = intermediate
importance = normal | high
```

Runtime injects exact `session_id`, `cycle_id`, `generation`,
`context_revision_id`, native assistant `tool_call_id` and original input batch.
LLM cannot choose route, client instance, reply target, emission ID or idempotency
key.

Stable logical identity:

```text
cycle + generation + assistant tool_call_id
→ one semantic AgentEmission
```

Same replay returns existing emission; changed semantics under same identity is a
managed conflict.

## Policy and trusted route

Max chars/count/min interval are applied atomically with READY persistence under
short exact-session coordination. Delivery failure does not free semantic spam
budget.

Trusted route is derived from authoritative original committed input, response
anchor and capability snapshot; arbitrary route metadata/secrets are not persisted
and LLM cannot redirect delivery.

## Persistence and delivery lifecycle

READY must be durable before manager-tool success:

```text
READY
→ DELIVERING
→ DELIVERED | FAILED | UNKNOWN | CANCELLED
```

Claim token fences one active attempt. Same-token retry after lost claim response
returns exact same DELIVERING attempt; competing token is rejected. Reliable
durable receipt is authority for DELIVERED and optional reply binding; duplicate
same receipt is idempotent.

`FAILED` means deterministic known-not-delivered. Timeout/connection ambiguity,
missing receipt, expired active claim or reset during started delivery become
`UNKNOWN`. UNKNOWN never automatically returns to READY and is never blindly
resent.

## Reply binding / reset / terminal

External reply binds to delivered emission only by exact session/client
instance/conversation/thread/external message ID. Numeric external ID in another
scope cannot spoof relation.

Reset fences old generation: READY → CANCELLED, DELIVERING → UNKNOWN. Sequential
terminal state prevents new old-cycle emission delivery; IR-7 later linearizes
concurrent emission READY claim ↔ terminal commit.

`send_user_message` remains a native tool call followed by one matching
`role=tool` result; runtime never duplicates it as second assistant message.

## Deterministic tests / DoD

Covered duplicate/concurrent tool calls, fake-clock policy, record/index crash,
cancellation after READY, lost claim/receipt responses, claim expiry, reset during
delivery, terminal-before-claim, route fencing, fake Telegram FAILED/UNKNOWN and
cross-session reply binding.

Done when READY-before-success, runtime-owned provenance, stable idempotency,
claim/receipt fencing, UNKNOWN no-blind-retry, safe reply relation and reset/
terminal fences are durable and transport-neutral.

## Historical out-of-scope

IR-6 did not own complete `/status`/timeline/Web/CLI projection; that was deferred
historically and is now implemented by IR-9. IR-10 full randomized/live roast is
still planned.

---

# IR-7 — Finalization barrier

## Status / evidence

Implemented after corrective passes on historical code/test HEAD
`6bd0dce0018b20520ed28236211fccdf0a8075fb`.

- `Validate Input Runtime` #417 — success, production compile success, `387 passed`, `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #669 — success.

## Goal

Prevent stale final/waiting response from ignoring newly durable input/control,
preserve handoff-before-terminal authority and linearize admission/output/emission
races around terminal commit.

## Terminal eligibility

Successful finalization requires authoritative agreement:

```text
accepted_through_cycle_sequence == applied_through_cycle_sequence
pending_control_sequence == applied_control_sequence
session generation == cycle generation
active cycle == finalizing cycle
exact context/finalization identity unchanged
exact RuntimeHandoff relation unchanged
```

## Durable finalization protocol

```text
candidate DONE
→ CP-BEFORE-FINAL-PROCESSING
→ capture exact candidate + admitted handoff authority
→ immutable final processing outside lock
→ PREPARED
→ short authoritative recheck
→ FINALIZING
→ RESULT_PERSISTED
→ final OutputBatch persisted / OUTPUT_READY
→ second authoritative terminal recheck
IF input/control/generation authority changed:
    ABORTED_NEW_INPUT | ABORTED_CONTROL
    RuntimeHandoff remains HANDED_OFF
    stale output remains fenced
ELSE:
    RuntimeHandoff COMPLETED
    → terminal ActiveCycleSnapshot
    → terminal SessionInputRuntimeState
    → TERMINAL_COMMITTED written last
    → final OutputBatch becomes claimable
```

Final processing does not reserve terminal right. No LLM/tool side effect occurs
after successful durable handoff completion.

## RuntimeHandoff ordering and output fence

Final terminal recheck must precede handoff completion; handoff completion must
precede terminal snapshot/session/marker. If handoff completion fails, no new
terminal state/marker/output delivery authority may appear.

`RESULT_PERSISTED` and `OUTPUT_READY` are not terminal authority. Ready outbox and
direct claim remain fenced until `TERMINAL_COMMITTED`; normal admitted-run output
also requires matching `RuntimeHandoff=COMPLETED`. Stale/aborted output is never
deliverable.

## Admission ↔ terminal race

Optimistic application read is not tie-break. Durable exact-session repository
coordination decides:

```text
admission first
→ same-cycle admission persists
→ accepted watermark advances
→ second terminal recheck aborts stale finalization
→ same cycle continues

terminal first
→ handoff COMPLETED + terminal state + TERMINAL_COMMITTED
→ stale non-start admission candidate rejected before writes
→ same admission call re-reads latest state
→ committed batch becomes START_CYCLE(new cycle), sequence 0
```

No old-cycle admission/index/inbox/watermark mutation may occur before stale
reclassification. Arbitrary corruption/consistency conflict is not silently
retried.

## WAITING barrier

```text
candidate question
→ CP-BEFORE-WAITING
→ exact input/control recheck
→ one durable waiting question authority
→ WAITING_USER
```

Input/control durable before commit suppresses stale question. Input after waiting
commit uses same-cycle `RESUME_WAITING`; no second question lifecycle is created.

## AgentEmission claim ↔ terminal ordering

IR-6 READY claim and IR-7 terminal command share compatible exact-session durable
ordering. Claim-first gives one legitimate DELIVERING attempt; terminal-first
prevents new old-cycle READY claim. Network send remains outside lock.

## Crash/direct retry

IR-7 direct retry supports PREPARED/result/output/partial-terminal windows with
stable finalization/result/output identities. Critical window:

```text
RuntimeHandoff COMPLETED
+ terminal snapshot/session/marker incomplete
→ retry exact known finalization
→ preserve handoff token/completed_at + finalization/result/output IDs
→ perform only local terminal convergence
→ no AgentCycle/LLM/tool replay
```

Completed handoff is never downgraded to AMBIGUOUS. Lost terminal response returns
the same terminal authority.

## Deterministic tests / DoD

Covered finalization phases, late input/control/reset, waiting barrier, output
claim fence, handoff completion faults/order, completed-handoff partial terminal
retry, admission-first/terminal-first race, emission claim ordering and
cancellation around completion.

Done when stale final/question/output is fenced, handoff completion cannot be
masked, terminal marker is last authority, same finalization retry is idempotent
and no stale final output becomes deliverable.

## Historical out-of-scope

IR-7 did not implement startup scanner/reconstruction or complete diagnostics.
IR-8 and IR-9 implemented those later. Randomized/live release acceptance remains
IR-10.

---

# IR-8 — Startup recovery и lifecycle

## Status / evidence

Implemented on historical final code/test HEAD
`5c88c52faa837b8b58c33c4893292a0708f6776a`.

- `Validate Input Runtime` #601 — success, production compile success;
- focused IR-8 — `49 passed`, `0 failed`;
- full input-runtime/config regression at that stage — `436 passed`, `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #761 — success.

## Goal / readiness

Recover durable runtime before accepting ordinary work and reconstruct only states
proved safe by durable evidence.

Process-local readiness:

```text
RECOVERING
READY
FAILED
STOPPING
STOPPED
```

Mandatory startup ordering:

```text
RECOVERING
→ startup-only durable discovery/reconciliation
→ committed-but-unadmitted admission repair
→ admission/session/inbox/control/reset/snapshot reconciliation
→ handoff/finalization/emission reconciliation
→ durable dependency validation
→ MCP connect
→ ActiveAgentCycle rehydration
→ exact recovered runner ownership installation
→ READY
```

No LLM/tool/client/network send occurs during mandatory recovery.

## Committed-but-unadmitted and claim/apply recovery

Startup discovers all durable committed batches without admission, not only the
current `commit_ready_drafts()` result, and creates/repairs exactly one admission
relation in deterministic order.

Expired claim behavior follows snapshot authority:

```text
CLAIMED expired + no apply authority → QUEUED
APPLYING expired + snapshot below range → QUEUED
APPLYING expired + snapshot covers exact range → marker reconciliation only
```

If snapshot/context revision already proves semantic apply, recovery never creates
a second input update/revision.

## PAUSED / WAITING / RUNNING / ambiguity

`PAUSED_BY_USER` remains same cycle/context with no automatic runner. Explicit
continue uses existing frozen-target semantics.

`WAITING_USER` preserves same question/context/cycle. Fresh reply is admitted as
same-cycle `RESUME_WAITING`.

Safe pre-handoff RUNNING may be reconstructed after MCP connect. `HANDED_OFF` or
`AMBIGUOUS` without stronger completion evidence is conservative non-auto-replay
interruption. Restart itself is never proof that an external side effect failed.

## Terminal preflight and partial convergence

Every existing `TERMINAL_COMMITTED` is validated before projection repair:

```text
matching finalization
+ matching RuntimeHandoff == COMPLETED
+ valid matching final OutputBatch
```

Contradiction is controlled structural recovery failure; recovery does not
retroactively complete an inconsistent handoff.

Safe partial terminal:

```text
OUTPUT_READY
+ RuntimeHandoff COMPLETED
+ TERMINAL_COMMITTED missing
→ same IR-7 local terminal convergence
→ same finalization/result/output/handoff IDs
→ no AgentCycle/LLM/tool replay
```

If handoff remains HANDED_OFF, late committed input can still win admission and
abort stale finalization. If handoff is already COMPLETED, old terminal convergence
precedes admitting late input as a new cycle.

## Reset and emission recovery

If reset already advanced generation, startup completes that existing cleanup
before generic stale old-generation snapshot validation. No second generation
increment. Already APPLIED immutable history remains evidence; pending old work is
fenced/cancelled.

Emission recovery:

```text
READY → retain READY, no startup send
expired DELIVERING → UNKNOWN
valid in-flight DELIVERING → do not steal/retry
UNKNOWN → remain UNKNOWN
terminal old-cycle READY → cancelled/non-claimable
```

## Exact recovered reservation

Recovered runner reservation is process-local defensive ownership over exact:

```text
session_id
cycle_id
input_batch_id
generation
```

It is installed after MCP connect. Foreign same-cycle addition cannot consume the
reservation or start runner #2; it remains FIFO input. Generation change/reset/
shutdown clears defensive ownership.

## Corruption policy

Repair is allowed only when immutable durable authorities agree and a derived
index/marker lags. Contradictions such as conflicting session/cycle ownership,
duplicate/gapped immutable sequences, missing referenced committed/context state,
incompatible handoffs or invalid terminal marker/handoff/output relation set gate
FAILED. No newest/mtime/majority heuristic chooses a semantic winner.

## Shutdown

```text
gate → STOPPING
→ reject new runner starts
→ cancel/await tracked recovered/admitted tasks
→ cancellation-safe durable cleanup
→ retain durable queues/controls/emissions/finalizations
→ MCP cleanup in owning lifespan task
→ STOPPED
```

PAUSED/WAITING without active runner remain durable states; COMPLETED handoff is
never downgraded.

## Deterministic tests / DoD

Focused IR-8 (`49 passed`) covers readiness sequencing, committed-unadmitted
repair, missing inbox/frontier, CLAIMED/APPLYING snapshot-first recovery,
PAUSED/WAITING same-cycle continuation, safe RUNNING vs ambiguous handoff,
partial reset, READY/UNKNOWN emission recovery, finalization phases/terminal
preflight, late-input handoff ordering, controlled corruption, exact recovered
reservation and shutdown.

Done when ordinary runtime stays closed until deterministic durable reconciliation
is complete, safe state is rehydrated without semantic guessing, ambiguity never
blind-replays and shutdown owns runner/MCP cleanup ordering.

## Historical out-of-scope

Complete `/status`/timeline/client diagnostics were historically deferred from
IR-8 and are now implemented by IR-9. Randomized restart/corruption roast and live
acceptance remain IR-10.

---

# IR-9 — Client projections, diagnostics и configuration examples

## Status / code evidence

Implemented and validated on exact code/test boundary:

`068f8f6682e7b7b805b60dbb640b53b671cc8565`

Exact code CI evidence:

- `Validate Input Runtime` #685 — completed / success;
- production compile — success;
- focused IR-8 — `49 passed`, `0 failed`;
- focused IR-9 — `101 passed`, `0 failed`;
- full input-runtime/config audit — `537 passed`, `0 failed`;
- `Validate v0.4 file artifacts PR` #803 — completed / success;
- token permissions — `Contents: read`, `Metadata: read`.

No skipped count is claimed because relevant workflow stdout does not print one.

## Goal / ownership

Expose coherent safe client status without changing durable IR-1—IR-8 authority:

```text
durable IR-1—IR-8 authority
→ coherent exact-session diagnostics read
→ InputRuntimeDiagnosticsService
→ RuntimeStatusSnapshot / RuntimeTimeline
→ Telegram / Web / CLI renderer
```

Diagnostics are `READ / DERIVE only`, never admission/control/finalization/recovery
authority.

## Coherent reader and privacy

Filesystem diagnostics uses existing short exact-session coordination for bounded
structured current-session/current-generation metadata. Localization, rendering,
Telegram network, HTTP serialization and CLI output happen outside lock. No
LLM/tool/network await or unbounded content scan occurs under runtime coordination.

Generic diagnostics excludes raw user/LLM/system text, prompts, tool args/results,
file contents, tokens/API keys/callback auth, arbitrary response-route metadata,
filesystem paths and raw traceback.

## Runtime status / timeline

`RuntimeStatusSnapshot` separates process readiness from durable session cycle
status and derives safe generation/cycle/context/watermark, queue/apply, control,
handoff, emission, finalization and recovery fields. Old-generation records are
excluded from current counts.

`RuntimeTimeline`:

- default `limit = 20`;
- maximum `limit = 100`;
- deterministic equal-timestamp display order;
- per-stream authoritative sequence/identity retained;
- cross-stream merge is display-only;
- no global semantic sequence;
- no new durable event store/WebSocket/event bus.

## Input/addendum projections

Implemented lifecycle:

```text
input_addendum_admitted
input_addendum_applying
input_addendum_applied
input_addendum_cancelled
input_addendum_failed
```

`QUEUED_RUNNING`, `QUEUED_PAUSED`, `RESUME_WAITING` have distinct semantics.
Queued addition is never described as already applied. Durable applied checkpoint
may produce transient presentation update for exact input batch without creating
AgentEmission, changing admission authority or writing client status into LLM
history.

## Control / recovery / terminal projections

Client distinction is explicit:

```text
pause accepted != PAUSED_BY_USER
continue accepted != resumed != still_waiting_for_input
INTERRUPTED != AMBIGUOUS
UNKNOWN delivery != FAILED
```

Ambiguous external work is not called definitely failed and diagnostics does not
replay it.

Emission diagnostics preserves READY/DELIVERING/DELIVERED/FAILED/UNKNOWN/CANCELLED
without raw text/route secrets. Finalization projection reflects existing IR-7
states; session DONE alone is not terminal authority without matching IR-7/IR-8
handoff/finalization/output evidence.

## Telegram / Web / CLI

Production `/status` is high-priority read-only consumer of shared diagnostics DTO:
no committed input, control sequence, wake, watermark/generation mutation or
AgentEmission.

Presentation edit policy:

- deterministic not-editable → bounded one-send fallback;
- ambiguous edit/send → no blind duplicate;
- session generation + presentation revision fencing suppress stale update;
- existing progress terminal barrier prevents stale progress overwriting terminal
  presentation.

Structured API endpoints:

```text
GET /runtime/status
GET /runtime/timeline
```

Standalone production runtime CLI framework is not added; shared DTO renderer is
the CLI-compatible consumer path.

RU/EN projection keys are synchronized. IR-9 adds no new config/environment
fields; existing configuration-example audit remains in the green `537 passed`
regression.

## IR-9 Definition of Done

Implemented/validated boundary includes transport-neutral coherent diagnostics,
privacy/current-generation filtering, bounded deterministic timeline, addendum /
control/recovery/emission/finalization projections, read-only Telegram `/status`,
safe edit fallback/fencing, structured Web API, shared CLI renderer, localization
parity, config-example audit and green deterministic CI.

---

# IR-10 — planned

IR-10 owns release-final randomized race/restart/corruption repetition, synthetic
whole-system marathon, maintainer live Telegram acceptance and final real-service
acceptance/report where planned.

This restoration pass does not implement or execute IR-10.

## Explicitly deferred outside current update

- Telegram edited-message history rewind;
- PostgreSQL / SQLAlchemy / Alembic;
- Redis/distributed workers/leases;
- distributed runtime;
- scheduler / `AgentRun` / `TaskRun`;
- parallel branches/fork-join;
- durable global event bus.

## Commit discipline

Stage history is advanced only by real forward commits. No reset/rebase/squash/
force-push/history rewrite is part of this sequence. Evidence records only GitHub
SHAs/workflow runs that actually exist.
