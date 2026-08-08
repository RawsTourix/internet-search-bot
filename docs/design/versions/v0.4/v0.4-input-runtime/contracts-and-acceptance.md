---
id: design.v0.4.input-runtime.contracts
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-08
---

# Contracts и acceptance

## Current implementation evidence

IR-1—IR-7 реализованы; IR-8—IR-10 остаются planned. Общий update сохраняет
`implementation_status: partial` до startup recovery, complete projections и full
acceptance.

IR-7 code/test boundary:

- `c58ab05c8354d7e76d4176e39ebf481edc4c613b`;
- `Validate Input Runtime` #355 — success, production compile success,
  `376 passed`, `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #638 — success;
- workflow сохраняет `permissions: contents: read`;
- deterministic IR-7 suites используют fake clock, explicit `asyncio.Event`,
  injected persistence faults, repository recreation, real OutputBatch
  claim/outbox paths и production MCP MRO;
- real Agent/LLM/MCP/Telegram/Web/internet calls для IR-7 tests не выполняются.

IR-6 code/test boundary:

- `4447d1bfe487bfd764829e701f274655aa8c3c50`;
- `Validate Input Runtime` #297 — success, compile success, `350 passed`,
  `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #609 — success.

IR-5 final code/test boundary:

- `0fabe15c6730a4e8db6be8b54ecec2c13ea773c7`;
- `Validate Input Runtime` #219 — success, compile success, `291 passed`,
  `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #570 — success.

IR-4 code/test boundary:

- `1d31b6fbd1d5e88966d3964dc35cf4680f32f522`;
- `Validate Input Runtime` #115 — success, compile success, `241 passed`,
  `0 failed`;
- `Validate v0.4 file artifacts PR` #518 — success.

IR-3 final code boundary:

- `c36e4cc38095e15f54f63ae81c29b4829defec1f`;
- `Validate Input Runtime` #84 — success, `198 passed`;
- `Validate v0.4 file artifacts PR` #503 — success.

IR-1/IR-2 historical boundaries остаются в git history и canonical stage docs.

## Назначение

Документ фиксирует public/runtime contracts, configuration, observability и
release acceptance для `v0.4-input-runtime`.

## Public semantic boundary

Ingress заканчивается только immutable committed input:

```text
transport fragments / drafts
→ ingress commit
→ CommittedInputBatch
→ InputAdmissionService
```

Runtime не читает незавершённые drafts и не принимает transport-local update как
semantic input.

## One active cycle contract

Для одной session/generation:

```text
0 or 1 active cycle
```

Ordinary committed input:

- idle/terminal session → новый cycle по admission semantics;
- running/finalizing до terminal authority → addition same cycle;
- waiting_user → same-cycle `RESUME_WAITING` semantics;
- pause_requested/paused_by_user → queued same-cycle input без auto-resume;
- stale generation → fenced.

Новый committed batch во время active cycle не запускает второй
`MCPClient.process_query()`.

## Input ordering contract

Input additions получают monotonic cycle sequence. Apply всегда FIFO и только
protocol-safe checkpoint:

```text
accepted_through_cycle_sequence
applied_through_cycle_sequence
```

`applied` не может опережать `accepted`. Gap/duplicate identity — consistency
defect, а не повод silently skip/reorder.

## Control ordering contract

IR-5 authoritative controls:

```text
pending_control_sequence
applied_control_sequence
```

Stable source idempotency создаёт один logical `SessionControlCommand`. Sequence
allocation и state publication происходят под short exact-session coordination.
Effective priority:

```text
reset > pause > continue > ordinary input
```

`/continue` freezes accepted-input target атомарно внутри той же coordination
boundary, которая упорядочивает input admission. Wall clock, transport arrival и
`asyncio` task scheduling не являются authority tie-break.

## Context revision contract

Initial active cycle создаёт `R1` до first main LLM/result. Каждый applied
contiguous range создаёт exactly one next linear `CycleContextRevision` и exactly
one runtime-owned `input_batch_update`.

Physical prompt compaction не является semantic apply evidence. Durable snapshot,
admission IDs, context revision и watermarks сохраняют replay identity.

## Manager-tool AgentEmission contract

IR-6 builtin manager tool:

```text
send_user_message(message, kind=intermediate, importance=normal|high)
```

LLM не получает authority задавать:

```text
session/cycle/generation/context_revision
route/client instance/conversation/thread
reply target
emission ID
idempotency key
```

Runtime связывает native assistant `tool_call_id` с exact
`ManagerToolExecutionContext` и stable logical idempotency. READY persistence
завершается до tool success; delivery wake/network receipt не входят в semantic
tool success.

## AgentEmission delivery contract

Lifecycle:

```text
READY → DELIVERING → DELIVERED | FAILED | UNKNOWN
```

- same-token claim retry idempotent;
- competing token conflicts;
- reliable durable receipt обязателен для DELIVERED;
- deterministic known rejection может стать FAILED;
- timeout/connection ambiguity/expired claim/reset during attempt → UNKNOWN;
- UNKNOWN не возвращается автоматически в READY;
- delivery failure не меняет AgentCycle state/input revision;
- reset fences old generation: READY→CANCELLED, DELIVERING→UNKNOWN.

IR-7 добавляет shared exact-session linearization относительно terminal commit:

```text
claim first    → READY→DELIVERING, attempt legitimate, terminal may commit later
terminal first → old-cycle READY claim cannot start
```

Network send выполняется после release coordination lock.

## Finalization contract — implemented IR-7

Final candidate не является terminal authority. Canonical terminal eligibility:

```text
accepted_through_cycle_sequence == applied_through_cycle_sequence
pending_control_sequence == applied_control_sequence
session generation == cycle generation
active cycle == finalizing cycle
exact finalization/context authority unchanged
```

Implemented pipeline:

```text
candidate DONE
→ CP-BEFORE-FINAL-PROCESSING
→ exact candidate authority
→ immutable final processing outside lock
→ CycleFinalizationRecord PREPARED
→ short authoritative recheck / FINALIZING
→ persist final AgentResult evidence / RESULT_PERSISTED
→ assemble + persist final OutputBatch / OUTPUT_READY
→ second short authoritative recheck
→ terminal snapshot/session convergence
→ CycleFinalizationRecord TERMINAL_COMMITTED written last
→ final OutputBatch becomes claimable
```

Любой input/control, который стал durable accepted/pending до terminal marker,
имеет право suppress stale finalization. Mismatch даёт controlled
`ABORTED_NEW_INPUT` или `ABORTED_CONTROL`. Persisted result и `OUTPUT_READY` не
дают client delivery authority.

Stable logical finalization ID выводится из exact session/cycle/generation/context
revision и expected input/control watermarks. Direct retry/repository recreation
той же logical finalization не создаёт independent result/output/terminal commit.

Filesystem multi-file terminal convergence пишет `TERMINAL_COMMITTED` marker
последним. Output ready outbox/direct claim проверяют именно durable finalization
eligibility. Stale/aborted unclaimed output не доставляется и освобождает
cycle-final commit-once binding для следующего корректного same-cycle финала.

## WAITING_USER barrier — implemented IR-7

Question ordering:

```text
candidate question
→ CP-BEFORE-WAITING
→ exact input/control recheck
→ one durable waiting snapshot/question authority
→ WAITING_USER
```

Input/control durable accepted до waiting commit suppress stale question. Input
после successful waiting commit использует existing same-cycle
`RESUME_WAITING`. `send_user_message(kind=intermediate)` не является ask-user и
не создаёт второй question lifecycle.

## Backpressure contract

Limits относятся к authoritative queued/admitted work, а не только к наличию
inbox file:

```text
max_queued_batches_per_session
max_queued_bytes_per_session
max_batches_per_checkpoint
max_batch_bytes_per_checkpoint
claim_lease_seconds
```

Capacity block retryable и не удаляет committed user input.

## Configuration

Canonical config fields:

```yaml
input_runtime:
  enabled: true
  max_queued_batches_per_session: 64
  max_queued_bytes_per_session: 67108864
  max_batches_per_checkpoint: 8
  max_batch_bytes_per_checkpoint: 8388608
  claim_lease_seconds: 300
  max_intermediate_messages_per_cycle: 16
  min_intermediate_message_interval_seconds: 1.0
  max_intermediate_message_chars: 4000
```

Exact defaults/validation находятся в `src/input_runtime/config.py`; examples
обязаны проходить `tests/test_artifact_configuration_examples.py`.

## Dependency contract

Application/domain input-runtime logic не зависит от:

- `Path`/filesystem layout;
- FastAPI;
- Telegram/python-telegram-bot;
- concrete HTTP transport;
- Redis/PostgreSQL adapters.

Filesystem specifics живут в infrastructure adapters. Command-oriented ports
должны маппиться на будущие PostgreSQL transactions/row locks без изменения
semantic state machine.

## Coordination contract

Locks:

- short;
- exact-session/identity scoped;
- deterministic order;
- no LLM/tool/compaction/network/client delivery await inside.

Execution lease отдельно защищает one active runner, но не является durable
semantic authority.

IR-7 PREPARED/terminal rechecks и IR-6 READY claim используют compatible exact
session coordination, поэтому их ordering linearizable без удержания Telegram/
network await.

## Crash/retry contract through IR-7

Already implemented direct/repository-local recovery includes:

- admission record-first/index publication repair;
- runtime handoff ambiguity evidence;
- snapshot-first input marking repair;
- control record-first/pending-watermark repair;
- continue frozen-target replay;
- emission record/index replay and same-token claim/receipt idempotency;
- finalization stable PREPARED identity;
- result evidence persisted / finalization state write retry;
- output persisted / OUTPUT_READY state write retry;
- partial terminal snapshot/session/finalization-marker convergence;
- lost terminal commit response returns same terminal authority;
- abort after persisted result/output never opens delivery.

IR-7 direct repository recreation tests **не** являются startup recovery. Global
startup discovery/reconstruction and policy remain IR-8.

## Observability contract

Raw user content/secrets не логируются как default diagnostics. Runtime events и
status должны уметь показать как минимум:

```text
session_id / cycle_id / generation
accepted/applied input watermarks
pending/applied control watermarks
cycle status
checkpoint
context revision ID
admission/inbox/control IDs
emission/finalization IDs and states
output batch ID / delivery eligibility
error/reason code
```

Complete `/status` timeline/Web/CLI diagnostics остаются IR-9.

## Security contract

- stable IDs/path inputs validated/escaped;
- LLM не контролирует response route/idempotency/provenance;
- stored emission route не содержит callback auth/bot/API secret metadata;
- claim/outcome проверяют exact client/session authority;
- external Telegram reply ID bind-ится только server-side exact scope;
- final OutputBatch terminal eligibility server-owned и transport-neutral;
- raw binary/user payload не встраивается в debug logs/history без policy;
- ambiguous external side effect не blind-retry-ится.

## Production composition contract

IR-7 не dormant helper. Characterization tests подтверждают production chain:

```text
Api
→ InputAdmissionService / FinalizationBarrierService
→ FinalizingArtifactDeliveryPlanningMCPClient
→ InputRuntimeControlMixin
→ InputRuntimeCheckpointHardeningMixin
→ InputRuntimeCheckpointMixin
→ real final processing
→ real OutputBatchAssembler
→ finalization terminal commit
→ normal OutputBatch claim/delivery path
```

Fakes используются только на external LLM/MCP/Telegram/Web boundaries.

## Deterministic race matrix through IR-7

### Input vs final

| Сценарий | Expected durable result |
|---|---|
| input before `CP-BEFORE-FINAL-PROCESSING` | checkpoint applies/suppresses stale candidate |
| input while final audit/grounding blocked | later PREPARED recheck rejects exact stale candidate |
| input after PREPARED | second recheck → `ABORTED_NEW_INPUT` |
| input after result persisted | retained result evidence, abort, no delivery |
| input after OutputBatch READY | stale batch fenced/cleaned, abort, no claim |
| input immediately before terminal marker | abort wins |
| input after `TERMINAL_COMMITTED` | ordinary admission creates new cycle-level work |

### Control vs final

| Сценарий | Expected durable result |
|---|---|
| `/stop` before terminal | stale finalization aborts; existing pause contract wins |
| `/reset` before terminal | old generation finalization/output fenced |
| control after final processing | pending/applied mismatch suppresses terminal commit |
| duplicate control with no watermark transition | no phantom abort |

### Waiting

| Сценарий | Expected durable result |
|---|---|
| input before waiting commit | stale question suppressed |
| pause/reset before waiting commit | no stale question; control semantics win |
| input after waiting commit | existing same-cycle `RESUME_WAITING` |
| repeated waiting commit | one durable question authority, no duplicate presentation |

### Output

| Сценарий | Expected durable result |
|---|---|
| OutputBatch persisted/READY before terminal | invisible/not claimable |
| terminal committed | normal claim becomes eligible |
| aborted/superseded output | never claimable/deliverable |
| lost terminal response/retry | same finalization/output/terminal authority |

### Emission

| Сценарий | Expected durable result |
|---|---|
| READY claim linearized first | one legitimate DELIVERING attempt |
| terminal commit linearized first | old READY claim cannot start |
| fake network blocked after claim | terminal commit proceeds; no session lock held by network await |

### Crash/replay

| Сценарий | Expected durable result |
|---|---|
| PREPARED recreation | same stable finalization ID |
| result durable, state write failed | same result hash/ref reused |
| OUTPUT_READY state write failure | same output identity reused |
| partial terminal marker window | output remains closed until marker; retry converges |
| partial terminal snapshot + newer input | controlled abort + snapshot repair to RUNNING |
| abort after result/output persistence | evidence retained/superseded, not delivered |
| duplicate logical retry | no second finalization/result/output/terminal authority |

No probabilistic sleeps используются как correctness mechanism.

## Regression command

Required gate after IR-7:

```text
python -m pytest -q tests/test_input_runtime_*.py tests/test_artifact_configuration_examples.py
```

Code/test boundary result:

```text
376 passed
0 failed
0 skipped
```

Production compile covers `src/input_runtime` plus affected API/runtime/MCP/
OutputBatch/emission/Telegram composition paths. Compile success confirmed by
`Validate Input Runtime` #355.

## External-call contract for tests

Focused IR-7 production/integration tests:

```text
real Agent/LLM calls: 0
real MCP network calls: 0
real Telegram network calls: 0
real Web/Internet calls: 0
```

Fakes/barriers are external-boundary substitutes only; durable repositories,
application services, OutputBatch claim/outbox and production MRO are real.

## CI contract

Workflow `Validate Input Runtime` remains read-only:

```yaml
permissions:
  contents: read
```

IR-7 extended only production compile/watch targets necessary for affected paths;
no separate workflow and no write permission introduced.

Code boundary required green workflows:

- `Validate Input Runtime` #355 — success;
- `Validate v0.4 file artifacts PR` #638 — success.

Documentation boundary must independently be green before PR evidence is updated.

## Remaining stage acceptance

### IR-8 planned

Startup recovery/reconstruction:

- startup ordering/readiness;
- committed-but-unadmitted repair;
- ambiguous runtime handoff reconciliation;
- paused/interrupted/waiting runner reconstruction;
- retained READY/UNKNOWN emission reconciliation without blind resend;
- incomplete finalization discovery/reconciliation;
- shutdown lifecycle recovery;
- full corruption authority policy.

### IR-9 planned

- complete runtime timelines;
- `/status` extension;
- Web/CLI projections;
- diagnostics UX/config polish.

### IR-10 planned

- randomized/restart/full-system roast;
- full repository acceptance baseline;
- synthetic transport roast;
- maintainer live Telegram acceptance.

## Deferred non-goals

- Telegram history rewind по edited messages;
- PostgreSQL/Redis/distributed runtime;
- scheduler;
- `AgentRun` / `TaskRun`;
- parallel branches/fork/join;
- new event bus;
- semantic intervention routing beyond current linear cycle;
- force rollback already-confirmed external side effects.

## Completion gate for whole `v0.4-input-runtime`

Whole update может стать `implemented` только когда:

- IR-1—IR-10 mandatory contracts реализованы;
- IR-8 startup recovery доказан;
- IR-9 diagnostics/projections complete;
- IR-10 full automated/restart/randomized/live acceptance green;
- no production bypass of admission/control/finalization authority;
- canonical docs and PR evidence synchronized;
- PR readiness/merge выполняются только отдельным explicit maintainer decision.

На текущем boundary IR-1—IR-7 implemented, IR-8—IR-10 planned, поэтому
`v0.4-input-runtime = partial`.
