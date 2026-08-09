---
id: design.current
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-09
---

# Текущее состояние проекта

На текущем baseline `v0.4` остаётся частично реализованной версией. Завершены
storage/result/cycle compaction, DAG planning, file artifacts,
file-artifacts-advanced и batch-workflows. `v0.4-input-runtime` остаётся
`partial`: IR-1—IR-8 реализованы и подтверждены CI, IR-9—IR-10 planned.
Следующие update-level этапы `v0.4-runtime-modularization` и
`v0.4-mcp-registry-foundation` также planned.

## Current baseline

Текущий production baseline включает:

- filesystem `ContentStore`/`ArtifactStore`, immutable refs и atomic writes;
- externalized MCP/tool results и bounded compaction;
- cycle working memory и semantic compaction;
- optional DAG planning без scheduler;
- artifact manager tools, versions и current/active scopes;
- capability snapshots, response anchors, `OutputBatch`, durable delivery receipts
  и Telegram file delivery;
- AUTO/EXPLICIT input batch workflows, collection lifecycle, relocation и
  terminal collection snapshot;
- durable `CommittedInputBatch` admission, one active cycle, FIFO `CycleInbox`,
  initial `R1`, active snapshot и protocol-safe input checkpoints;
- admission-vs-terminal linearization на durable repository coordination:
  admission allocation, выигравший первым, продвигает accepted watermark и
  abort-ит stale finalization; terminal authority, выигравшая первой, заставляет
  discard stale optimistic active-cycle classification и reclassify тот же
  committed batch как `START_CYCLE` без transport retry;
- durable pause/continue/reset controls с monotonic watermarks, cooperative
  `/stop`, same-cycle `/continue`, atomic frozen resume target и generation fence;
- explicit durable semantic intermediate `AgentEmission` через manager tool
  `send_user_message`, runtime-owned exact provenance/idempotency/route,
  linearizable policy и independent delivery lifecycle;
- route-scoped emission outbox, fenced delivery claims, durable generic receipts,
  conservative FAILED/UNKNOWN semantics, reset/terminal fencing;
- durable `CycleFinalizationRecord` barrier с stable logical identity,
  `PREPARED → RESULT_PERSISTED → OUTPUT_READY → TERMINAL_COMMITTED`, двумя
  authoritative watermark/control rechecks и crash-safe replay;
- exact admitted invocation `RuntimeHandoff` binding: successful terminal path
  durable фиксирует `RuntimeHandoff=COMPLETED` после final recheck, но до terminal
  snapshot/session convergence и до `TERMINAL_COMMITTED`;
- final `OutputBatch` delivery gate: persistence/READY не даёт claim authority до
  `TERMINAL_COMMITTED`, а stale/aborted output освобождает cycle-final identity;
- shared exact-session ordering `AgentEmission READY claim ↔ terminal commit`,
  при котором network send остаётся вне coordination lock;
- durable WAITING question commit с late-input/control suppression до
  `WAITING_USER` authority;
- Telegram semantic intermediate delivery отдельным plain-text message и optional
  server-resolved reply-to-emission projection;
- process-local IR-8 readiness gate: `RECOVERING → READY`, либо controlled
  `FAILED`, с `STOPPING → STOPPED` shutdown lifecycle;
- startup-wide durable reconciliation всех committed/admission/inbox/control/
  snapshot/handoff/finalization/emission authorities до MCP connect;
- fresh-process rehydration PAUSED/WAITING/INTERRUPTED context и safe runner
  ownership installation после MCP connect, но до READY;
- exact recovered runner reservation owner
  `(cycle_id + input_batch_id + generation)`, который нельзя забрать foreign
  same-cycle addition;
- strict terminal startup authority:
  `TERMINAL_COMMITTED + matching RuntimeHandoff COMPLETED + valid final OutputBatch`;
  terminal session/snapshot projection без этой authority считается corruption;
- conservative startup no-blind-replay для `HANDED_OFF`/`AMBIGUOUS`, READY emission
  retention и `DELIVERING` expiry → `UNKNOWN` без re-arm.

## v0.4-input-runtime: current status

### IR-1 — implemented

Domain models/config/repository ports созданы в `src/input_runtime`. Stable IDs,
watermarks, generation/state validation и PostgreSQL-compatible command-oriented
ports зафиксированы.

### IR-2 — implemented

Filesystem repositories, atomic record writes, short per-session coordination,
crash-recoverable identity/index publication, claims и repository recreation
реализованы.

### IR-3 — implemented and hardened

Каждый immutable `CommittedInputBatch` проходит общий admission service. Initial
batch запускает ровно один cycle; additions active cycle durable admitted в FIFO
и не создают второй runner. Capacity reservation не теряется при missing inbox,
a runtime handoff marker отделяет retryable pre-handoff failure от ambiguous
post-handoff execution. Cancellation cleanup shielded и storage-neutral.

IR-3 code boundary:

- `c36e4cc38095e15f54f63ae81c29b4829defec1f`;
- `Validate Input Runtime` #84 — success, `198 passed`;
- `Validate v0.4 file artifacts PR` #503 — success.

### IR-4 — implemented

Active-cycle context ownership реализован через initial `R1`, durable
`ActiveCycleSnapshot`, accepted-at-entry checkpoints, bounded FIFO
`CycleInputApplier`, linear context revisions и snapshot-first apply protocol.
WAITING reply использует общий FIFO `CP-RESUME`; input не вставляется внутрь
открытого assistant tool-call block. Handoff completion предшествует terminal
snapshot synchronization.

IR-4 final code boundary:

- `1d31b6fbd1d5e88966d3964dc35cf4680f32f522`;
- `Validate Input Runtime` #115 — success, compile success, `241 passed`,
  `0 failed`;
- `Validate v0.4 file artifacts PR` #518 — success.

### IR-5 — implemented and validated

Transport-neutral control service и durable `SessionControlCommand` дают
monotonic sequence/idempotency, real pending/applied control watermarks,
cooperative safe-checkpoint pause, paused FIFO admission без auto-resume,
same-cycle continue и durable generation-authoritative reset.

`/continue` target не определяется pre-lock state. Command-oriented
`accept_continue(...)` внутри shared `root identity → session` coordination
атомарно читает authoritative state, repairs control frontier, freezes current
`active_cycle_accepted_through_sequence`, publishes command/indexes и advances
pending watermark. Input coordinated до continue входит в initial resume drain;
input coordinated после continue не расширяет frozen target и ждёт следующий
ordinary running checkpoint. Duplicate/crash replay сохраняет original
ID/sequence/target.

IR-5 final code boundary:

- `0fabe15c6730a4e8db6be8b54ecec2c13ea773c7`;
- `Validate Input Runtime` #219 — success, compile success, `291 passed`,
  `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #570 — success.

Final IR-5 documentation HEAD был
`c06819c0f85b5113024fff2302726c7fb6a7aa85`; на нём `Validate Input Runtime`
#231 и `Validate v0.4 file artifacts PR` #576 были green.

### IR-6 — implemented and validated

IR-6 активировал dormant `AgentEmission` foundation как production semantic
intermediate-message lifecycle, отдельно от transient `ProgressEvent`,
Question/`WAITING_USER` и final `OutputBatch`.

Builtin manager tool `send_user_message` принимает только:

```text
message
kind = intermediate
importance = normal | high
```

LLM не получает authority задавать session/cycle/generation/context revision,
client route/instance, reply target, emission ID или idempotency key.
Runtime-owned `ManagerToolExecutionContext` связывает exact native assistant
`tool_call_id` с `session_id + cycle_id + generation + context_revision_id +
original_input_batch_id`. Scoped active-cycle ContextVar token/reset-ится вокруг
exact cycle, а deterministic concurrent-session test подтверждает отсутствие
context bleed.

Stable logical idempotency строится из manager tool namespace + cycle + generation
+ native assistant tool-call ID. Same replay возвращает тот же `emission_id`,
changed semantic arguments дают managed conflict, concurrent same key создаёт
один record. Record-first/index-publication crash и manager cancellation после
READY persistence восстанавливаются на тот же emission после repository
recreation.

Политика реально использует:

```text
max_intermediate_messages_per_cycle
min_intermediate_message_interval_seconds
max_intermediate_message_chars
```

Count/rate/persistence acceptance выполняются одним command-oriented repository
operation под короткой exact-session coordination. Delivery failure не
освобождает semantic spam budget; tests используют fake clock без policy sleeps.

Trusted response route выводится только из authoritative original
`CommittedInputBatch`, response anchor и capability snapshot. Stored snapshot
содержит client type/instance, conversation/thread, safe reply/reference relation
и capability snapshot ID, но не transport route metadata, callback auth, bot/API
tokens или иные secrets. LLM не может переопределить delivery route; missing
trusted route даёт controlled `route_unavailable`.

Persistence contract:

```text
validate semantic arguments
→ resolve exact runtime authority
→ linearizable policy + durable AgentEmission READY
→ best-effort delivery wake
→ compact role=tool result
→ AgentCycle continues
```

Tool success не возвращается до READY persistence, но agent loop не ждёт client
network receipt. Emission persistence сама не создаёт context revision, не меняет
input/control watermarks и не переводит cycle в WAITING.

Delivery lifecycle:

```text
READY
→ exact route-scoped claim
→ DELIVERING
→ client send
→ durable receipt/outcome
→ DELIVERED | FAILED | UNKNOWN
```

Same-token claim retry после lost HTTP response возвращает тот же active attempt;
different token конфликтует. Reliable external receipt обязателен для
`DELIVERED`; duplicate same receipt идемпотентен, changed receipt конфликтует.
Deterministic preflight/client rejection может стать `FAILED`; timeout,
connection ambiguity, missing receipt, expired in-flight claim или reset во время
active attempt становятся `UNKNOWN`. UNKNOWN не возвращается автоматически в
READY и не blind-retry-ится.

Worker authority повторно fenced server-side. Outcome validation включает exact
session, cycle, generation, client type, client instance, conversation и thread.
Network await не выполняется под session/filesystem coordination lock.

Telegram worker отправляет semantic intermediate как новый `send_message` с
`parse_mode=None`, а не редактирует transient progress message. Successful
Telegram `message_id` сохраняется в durable generic receipt. Claim/receipt HTTP
responses можно безопасно повторить с той же durable identity, но Telegram send
после ambiguous transport outcome не повторяется blindly.

Telegram ingress уже получает `reply_to_message.message_id` server-side. После
successful emission delivery external ID связывается с internal emission только
при exact session/client type/client instance/conversation/thread scope.
Optional input projection может содержать
`reply_to: {emission_id, kind=intermediate}` без branch semantics, изменения FIFO
или admission policy. Совпадение external numeric ID в другой session/chat/thread
не создаёт relation; произвольного user-supplied `reply_to_emission_id` authority
нет.

Existing `AgentAction.agent_request` остаётся transient progress
`agent_message`; IR-6 не auto-promote-ит progress в durable dialog message.
Native LLM history остаётся
`assistant tool_call(send_user_message) → matching role=tool result`; runtime не
добавляет второй assistant message с тем же text.

Pause не отменяет уже durable READY semantic intent. Reset fences old generation:
`READY → CANCELLED`, `DELIVERING → UNKNOWN`; stale claim writer после reset не
может завершить record. Sequential terminal fencing отвергает новый emission,
если cycle уже terminal, и не начинает новую READY delivery после already-visible
terminal state. Concurrent `READY claim ↔ terminal commit` ordering теперь
закрыт IR-7 общей exact-session finalization authority.

IR-6 code/test boundary:

- `4447d1bfe487bfd764829e701f274655aa8c3c50`;
- `Validate Input Runtime` #297 — success, compile success, `350 passed`,
  `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #609 — success.

Focused deterministic IR-6 tests используют fake clock, explicit asyncio
barriers, controlled persistence/cancellation faults, repository recreation и
fake Telegram/http transport. Real LLM/MCP/Telegram/Web/internet calls не нужны.

Startup reconstruction/reconciliation READY/UNKNOWN и interrupted/paused/waiting
runtime реализованы IR-8. Полные client timelines, `/status`, Web/CLI/addendum
projections остаются IR-9; randomized/full-system/live roast — IR-10.

### IR-7 — implemented and validated

IR-7 активировал existing `CycleFinalizationRecord`/`FinalizationRepository` в
production path без второй параллельной finalization state machine.

Canonical terminal eligibility проверяется по authoritative session state:

```text
active_cycle_accepted_through_sequence == active_cycle_applied_through_sequence
pending_control_sequence == applied_control_sequence
session.generation == finalizing generation
session.active_cycle_id == finalizing cycle
```

DONE candidate сначала проходит `CP-BEFORE-FINAL-PROCESSING`. После clean
checkpoint runtime фиксирует exact candidate authority: session/cycle/generation,
context revision и expected input/control watermarks. Exact admitted invocation
также передаёт runtime-owned `admission_id + handoff_token`; эти values не
приходят от LLM/client и durable связывают finalization с текущим
`RuntimeHandoff`. Final audit/grounding не резервирует terminal authority. После
final processing durable stable `finalization_id` переводится в `PREPARED`, а
короткий exact-session recheck либо закрепляет `FINALIZING`, либо даёт controlled
`ABORTED_NEW_INPUT` / `ABORTED_CONTROL`.

После успешного prepare выполняются:

```text
persist final AgentResult evidence
→ RESULT_PERSISTED
→ assemble/persist normal final OutputBatch
→ OUTPUT_READY
→ second authoritative terminal recheck
→ RuntimeHandoff COMPLETED
→ terminal snapshot/session convergence
→ TERMINAL_COMMITTED
→ final OutputBatch claim eligibility
```

Late input/control mismatch обрабатывается **до** handoff completion. Поэтому
abort оставляет `RuntimeHandoff=HANDED_OFF`, stale output закрыт, а тот же cycle
может продолжить LLM/tool работу. После durable `RuntimeHandoff=COMPLETED` никакой
новый LLM/tool side effect этого invocation не запускается.

Admission и terminal commit также упорядочены той же durable `root identity →
session` coordination boundary. Application state read остаётся optimistic и не
является tie-break. Если admission allocation к cycle A получает durable boundary
первым, accepted watermark продвигается и second terminal recheck abort-ит stale
finalization как `ABORTED_NEW_INPUT`. Если terminal authority получает boundary
первой, stale non-start candidate отвергается **до** admission/index/inbox/state
write специальным managed stale-decision conflict; тот же in-process admission
call один раз перечитывает authoritative state, заново вычисляет kind/action/
capacity/target и создаёт ровно один `START_CYCLE` admission для нового cycle B.
Transport retry и IR-8 committed-but-unadmitted recovery для этой normal live race
не требуются. Arbitrary corruption/consistency conflicts этим retry не маскируются.

`RESULT_PERSISTED` и `OUTPUT_READY` не являются delivery authority. Final
`OutputBatch` скрыт из ready outbox и отвергается claim service до durable
`TERMINAL_COMMITTED`. Для normal admitted-run path delivery gate дополнительно
требует matching `RuntimeHandoff=COMPLETED`. Если handoff completion write падает,
не появляется ни новый terminal snapshot, ни session DONE, ни
`TERMINAL_COMMITTED`, ни delivery eligibility.

Filesystem terminal commit использует последовательные durable writes под одной
короткой exact-session coordination boundary. Lock-aware infrastructure primitive
завершает exact RuntimeHandoff без повторного захвата non-reentrant session lock.
Finalization `TERMINAL_COMMITTED` marker записывается последним и является
server-owned output-delivery fence.

Crash после durable `RuntimeHandoff=COMPLETED`, но до terminal snapshot/session/
marker, допускает только direct IR-7 retry известного `finalization_id`: повтор
сохраняет тот же handoff token/completed_at, finalization ID, result_ref и
OutputBatch ID и завершает terminal convergence без LLM/tool replay. Startup-wide
discovery/reconstruction такого состояния реализованы IR-8.

WAITING candidate проходит `CP-BEFORE-WAITING`, затем отдельный короткий exact
input/control recheck и одну durable waiting-question authority. Input/control,
durable accepted до waiting commit, подавляет stale question; input после
successful `WAITING_USER` commit остаётся существующим same-cycle
`RESUME_WAITING` flow. `send_user_message(kind=intermediate)` не используется как
ask-user и duplicate question lifecycle не создаётся.

IR-6 emission claim и IR-7 terminal commit используют один exact-session
coordination ordering. Если READY→DELIVERING claim linearized первым, этот уже
начатый transport attempt легитимно завершает IR-6 lifecycle. Если terminal
commit linearized первым, новый old-cycle READY claim не стартует. Network send
остаётся за пределами lock; ambiguous `DELIVERING` semantics IR-6 не ослаблены.

Corrected IR-7 code/test boundary:

- `6bd0dce0018b20520ed28236211fccdf0a8075fb`;
- `Validate Input Runtime` #417 — success, production compile success,
  `387 passed`, `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #669 — success;
- workflow сохраняет `permissions: contents: read`;
- previous eight handoff-ordering tests остаются green;
- три новые deterministic admission/terminal tests покрывают terminal-first
  transparent `START_CYCLE` reclassification, admission-first
  `ABORTED_NEW_INPUT` и pre-write managed stale-decision/no-raw-ValidationError
  contract.

### IR-8 — implemented and validated

IR-8 добавил process-restart reconstruction без второй runtime state machine.
Production composition использует полный chain:

```text
base recovery
→ conservative recovery_hardening
→ strict recovery_terminal validation
→ Api.start()/stop() lifecycle
```

Startup gate и фактический порядок:

```text
RECOVERING
→ startup-only durable discovery/reconciliation
→ committed-but-unadmitted admission repair
→ admission/session/inbox/control/reset/snapshot reconciliation
→ handoff/finalization/emission recovery
→ validate durable auxiliary refs
→ MCP connect
→ rehydrate/install recovered ActiveAgentCycle
→ install exact recovered runner reservation/task ownership
→ READY
```

Mandatory recovery не запускает LLM, MCP tool, Telegram/Web send или иной external
side effect. Safe runner может быть запланирован только после MCP connect и ждёт
READY; его process-local reservation принадлежит exact
`cycle_id + input_batch_id + generation`, поэтому foreign same-cycle addition не
может украсть recovered runner lease.

Все durable committed batches без `InputAdmissionRecord` обнаруживаются startup
query независимо от того, были ли они только что возвращены `commit_ready_drafts()`.
Existing admission с missing inbox repair-ится без второго admission. Snapshot-first
apply authority завершает только lagging inbox/admission/session markers: уже
persisted context revision/input update не повторяется.

Fresh-process state reconstruction сохраняет exact cycle/context:

- `PAUSED_BY_USER` остаётся paused, input остаётся FIFO queued, explicit
  `/continue` возобновляет тот же cycle/context и frozen target;
- `WAITING_USER` сохраняет waiting question/context, новый reply получает
  `RESUME_WAITING` в тот же cycle;
- safe pre-handoff RUNNING классифицируется как restartable same-cycle work;
- `HANDED_OFF`/`AMBIGUOUS` без stronger durable evidence не replay-ится автоматически.

Terminal projection не равна terminal authority. Для уже существующего
`TERMINAL_COMMITTED` strict preflight **до любых repair mutations** требует:

```text
TERMINAL_COMMITTED
+ matching RuntimeHandoff COMPLETED
+ existing matching final OutputBatch
```

`session/snapshot DONE` без marker, terminal marker с non-COMPLETED handoff или
missing/mismatched final output дают controlled fatal recovery и READY не
открывается. Отдельный recoverable case
`OUTPUT_READY + RuntimeHandoff COMPLETED + TERMINAL_COMMITTED missing` сходится
локально через IR-7 direct command с теми же finalization/result/output IDs и без
whole-cycle/LLM/tool replay. Это также сохраняет critical ordering late committed
input: до handoff completion batch может invalidate old finalization; после
COMPLETED сначала завершается old terminal authority, затем late batch начинает
новый cycle.

Incomplete reset, где durable generation уже advanced, сходится **до** generic
validation stale old-generation snapshots. Generation второй раз не повышается;
already APPLIED immutable admission history сохраняется, а pending old-generation
work/snapshot/emissions/finalization fenced/cancelled existing IR-5 semantics.
Coordinator generation затем синхронизируется только из durable state.

Emission startup semantics:

```text
READY → retained, no startup send
expired DELIVERING → UNKNOWN
UNKNOWN → retained UNKNOWN, never READY
terminal old-cycle READY → CANCELLED / non-claimable
```

Consistent derived lag/index loss допускает deterministic repair. Contradictory
immutable ownership, duplicate/gapped admission authority, missing committed
batch/context revision, incompatible handoff/finalization authority дают typed
recovery failure; corruption не выбирается по mtime/newest/majority.

Shutdown сначала переводит readiness в STOPPING, запрещает новые runner starts,
отменяет tracked recovered/admitted tasks через cancellation-safe runtime cleanup,
сохраняет durable recovery evidence, затем закрывает MCP lifecycle и переводит
gate в STOPPED. PAUSED/WAITING без active runner не получают fake interruption.

IR-8 final code/test boundary:

- `5c88c52faa837b8b58c33c4893292a0708f6776a`;
- `Validate Input Runtime` #601 — success;
- production compile — success;
- focused IR-8 restart contracts — `49 passed`, `0 failed`;
- full input-runtime/config regression — `436 passed`, `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #761 — success, all validation groups green;
- workflow сохраняет `permissions: contents: read`;
- real LLM/MCP network/Telegram/Web/internet calls в deterministic recovery tests
  не используются.

## Что ещё не реализовано

### IR-9 — planned

Complete structured projections, diagnostics, `/status`, addendum lifecycle,
Web/CLI UX и config/documentation polish beyond minimal transport-neutral
reply/emission/finalization foundations.

### IR-10 — planned

Full randomized race repetitions, restart matrix, synthetic whole-system roast и
maintainer live Telegram acceptance. Focused deterministic IR-8 CI не заменяет
IR-10.

## Deferred / out of current stage

Текущий implemented baseline **не** включает:

- Telegram history rewind по edited message;
- PostgreSQL/SQLAlchemy/Alembic;
- Redis/arq/distributed workers/locks;
- scheduler, `AgentRun`/`TaskRun`, parallel branches, fork/join;
- automatic semantic rerouting additions into new tasks;
- full first-party Web conversation projection framework;
- force rollback already-confirmed external side effects.

## Architecture invariants in force

1. Ingress заканчивается immutable `CommittedInputBatch`.
2. Один active cycle на session; additions не запускают второй runner.
3. Cycle input применяются только protocol-safe checkpoints и FIFO.
4. Один LLM/tool atomic block использует immutable context revision.
5. Pause/resume/reset — durable control semantics, не transport-local state.
6. Semantic AgentEmission — durable отдельная dialog event, не transient progress,
   question или final OutputBatch.
7. Delivery lifecycle не является execution lifecycle; failure/UNKNOWN
   intermediate не убивает AgentCycle.
8. Runtime-owned route/provenance/idempotency не принимаются от LLM/client.
9. Ambiguous external side effect не replay-ится blindly.
10. Persisted final result/`OutputBatch READY` не являются terminal authority;
    successful admitted-run terminalization сначала durable завершает matching
    RuntimeHandoff, затем пишет terminal snapshot/session и только потом
    `CycleFinalizationRecord=TERMINAL_COMMITTED`.
11. Durable admission allocation и terminal commit linearized одним session
    ordering point: admission-first подавляет stale terminal candidate;
    terminal-first reclassifies тот же committed batch в новый cycle без ошибки и
    без transport retry.
12. До terminal/waiting commit любой durable accepted input/control имеет право
    подавить stale candidate; после terminal commit новый ordinary input создаёт
    новую cycle-level работу.
13. Filesystem adapters скрыты за command-oriented ports; application services не
    импортируют Path/layout/locks и сохраняют PostgreSQL v0.5 portability.
14. Startup ordinary runtime work запрещён до `READY`; recovery failure оставляет
    gate `FAILED` и не подключает MCP после structural contradiction.
15. Existing `TERMINAL_COMMITTED` проходит strict handoff/output preflight до
    projection repair; terminal authority не достраивается задним числом из
    contradictory immutable history.
16. Recovered runner reservation принадлежит exact input/cycle/generation owner и
    не является durable queue/authority.
17. Scheduler/branches не реализуются до отдельной orchestration layer.

## Next implementation stage

Следующий planned stage — **IR-9 complete client projections/diagnostics**. Он не
реализуется в рамках IR-8 и остаётся отдельным этапом; IR-10 также planned.

До завершения IR-9—IR-10 общий `v0.4-input-runtime` и весь `v0.4` baseline
остаются `partial`.
