---
id: design.current
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-08
---

# Текущее состояние проекта

На текущем baseline `v0.4` остаётся частично реализованной версией. Завершены
storage/result/cycle compaction, DAG planning, file artifacts,
file-artifacts-advanced и batch-workflows. `v0.4-input-runtime` остаётся
`partial`: IR-1—IR-6 реализованы и подтверждены CI, IR-7—IR-10 planned.
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
- durable pause/continue/reset controls с monotonic watermarks, cooperative
  `/stop`, same-cycle `/continue`, atomic frozen resume target и generation fence;
- explicit durable semantic intermediate `AgentEmission` через manager tool
  `send_user_message`, runtime-owned exact provenance/idempotency/route,
  linearizable policy и independent delivery lifecycle;
- route-scoped emission outbox, fenced delivery claims, durable generic receipts,
  conservative FAILED/UNKNOWN semantics, reset/terminal sequential fencing;
- Telegram semantic intermediate delivery отдельным plain-text message и optional
  server-resolved reply-to-emission projection.

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
terminal state.

IR-6 code/test boundary:

- `4447d1bfe487bfd764829e701f274655aa8c3c50`;
- `Validate Input Runtime` #297 — success, compile success, `350 passed`,
  `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #609 — success.

Focused deterministic IR-6 tests используют fake clock, explicit asyncio
barriers, controlled persistence/cancellation faults, repository recreation и
fake Telegram/http transport. Real LLM/MCP/Telegram/Web/internet calls не нужны.

IR-6 sequential terminal fence **не** закрывает atomic concurrent race
`emission claim ↔ terminal/finalization commit`: shared durable finalization
barrier остаётся IR-7. Startup reconstruction/reconciliation READY/UNKNOWN и
interrupted/paused/waiting runtime остаётся IR-8. Полные client timelines,
`/status`, Web/CLI/addendum projections остаются IR-9; randomized/full-system/live
roast — IR-10.

## Что ещё не реализовано

### IR-7 — planned

Durable `CycleFinalizationRecord` barrier, prepared/result/output/terminal phase
machine, повторный watermark/control recheck и atomic late
input/control/emission-claim vs terminal closure.

IR-4/IR-5 checkpoint-level stale candidate suppression и IR-6 sequential terminal
emission fence не являются заменой этому barrier.

### IR-8 — planned

Startup recovery/reconstruction: ambiguous `RuntimeHandoff`, retained
READY/UNKNOWN emission claims, paused/waiting/interrupted runner reconstruction,
finalization startup repair и authoritative corruption matrix.

### IR-9 — planned

Complete structured projections, diagnostics, `/status`, addendum lifecycle,
Web/CLI UX и config/documentation polish beyond minimal IR-6 transport-neutral
reply/emission projection foundation.

### IR-10 — planned

Full randomized race repetitions, restart matrix, synthetic whole-system roast и
maintainer live Telegram acceptance. Focused deterministic IR-6 CI не заменяет
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
10. Filesystem adapters скрыты за command-oriented ports; application services не
    импортируют Path/layout/locks и сохраняют PostgreSQL v0.5 portability.
11. Scheduler/branches не реализуются до отдельной orchestration layer.

## Next implementation stage

Следующий stage — **только IR-7 finalization barrier**. IR-8/IR-9/IR-10 и прочие
future items не должны подтягиваться в IR-7 без необходимости контракта.

До завершения IR-7—IR-10 общий `v0.4-input-runtime` и весь `v0.4` baseline
остаются `partial`.
