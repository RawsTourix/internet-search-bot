---
id: design.v0.4.input-runtime.emissions
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-08-09
---

# Agent emissions и client projections

## Current implementation status

Этот документ описывает совместный observable boundary IR-6—IR-9.

- IR-6 — durable semantic `AgentEmission`: implemented;
- IR-7 — shared emission/finalization terminal ordering: implemented;
- IR-8 — startup recovery/reconciliation и ambiguity preservation: implemented;
- IR-9 — complete client diagnostics/projections: implemented;
- IR-10 — randomized/restart/synthetic/live acceptance: planned.

`v0.4-input-runtime` остаётся `partial` только потому, что IR-10 ещё не выполнен.

## Evidence

### IR-6 historical boundary

IR-6 code/test boundary:

`4447d1bfe487bfd764829e701f274655aa8c3c50`

На этой historical stage boundary durable intermediate-message lifecycle был реализован отдельно от transient progress, waiting question и final `OutputBatch`.

### IR-9 current projection boundary

IR-9 code/test boundary:

`068f8f6682e7b7b805b60dbb640b53b671cc8565`

Фактический CI на exact SHA:

- `Validate Input Runtime` #685 — completed / success;
- production compile — success;
- focused IR-8 — `49 passed`, `0 failed`;
- focused IR-9 — `101 passed`, `0 failed`;
- full input-runtime/config regression — `537 passed`, `0 failed`;
- `Validate v0.4 file artifacts PR` #803 — completed / success;
- token permissions — `Contents: read`, `Metadata: read`.

## Four distinct outgoing lifecycles

IR-9 не смешивает существующие четыре типа исходящего взаимодействия.

### `ProgressEvent`

Transient/coalescible presentation progress.

Свойства:

- может edit/coalesce/throttle;
- не является durable dialog event;
- не изменяет context revision;
- не становится `AgentEmission` автоматически;
- restart не обязан воспроизводить его как semantic history.

Current `AgentAction.agent_request` остаётся transient `ProgressEvent(type="agent_message")`.

### Intermediate `AgentEmission`

Durable semantic intermediate message.

Lifecycle:

```text
READY
→ DELIVERING
→ DELIVERED | FAILED | UNKNOWN | CANCELLED
```

`AgentEmission` имеет stable internal identity и exact cycle/generation/context provenance. Он не terminalizes cycle, не переводит cycle в `WAITING_USER` и не создаёт новую input context revision.

`UNKNOWN` означает, что transport side effect мог произойти. Поэтому UNKNOWN не отображается как FAILED и не re-arm-ится для blind replay.

### Question / `WAITING_USER`

Question остаётся отдельным waiting lifecycle. Intermediate emission не используется как ask-user authority.

IR-7 waiting barrier сохраняет ordering:

```text
candidate question
→ CP-BEFORE-WAITING
→ exact input/control recheck
→ one durable question authority
→ WAITING_USER
```

Fresh user reply продолжает existing same-cycle `RESUME_WAITING` admission path.

### Final response / `OutputBatch`

Final response остаётся `OutputBatch` + IR-7 finalization protocol.

`OutputBatch READY` сам по себе не является client terminal authority. Claim/delivery eligibility требует matching terminal authority; `SessionInputRuntimeState == DONE` без valid IR-7/IR-8 evidence недостаточен.

## IR-6 manager tool contract

Builtin `send_user_message` принимает только semantic arguments:

```text
message
kind = intermediate
importance = normal | high
```

LLM не получает authority задавать:

- session/cycle/generation/context revision;
- response route/client instance;
- reply target;
- emission ID;
- idempotency key.

Runtime-owned `ManagerToolExecutionContext` связывает exact assistant tool call с current session/cycle/generation/context revision/original input batch.

Logical idempotency основана на runtime-owned tool-call identity. Replay того же logical call возвращает тот же emission; changed semantic arguments под той же identity дают managed conflict.

## Trusted delivery route

Route выводится runtime из authoritative committed input/response anchor/capability state. LLM не выбирает client destination.

Stored/projection-safe route data не раскрывает callback auth, bot/API tokens или arbitrary transport metadata.

Telegram semantic intermediate доставляется отдельным new message. Он не редактирует progress/addendum status и не превращается в final response.

## Delivery ambiguity

`FAILED` допускается только для deterministic known-not-delivered outcome.

`UNKNOWN` используется, когда side effect мог дойти до клиента, например при transport timeout/connection ambiguity после начала send. Это сохраняет no-blind-retry contract IR-6/IR-8.

Reset fences old generation:

```text
old READY      → CANCELLED
old DELIVERING → UNKNOWN
```

Terminal-first ordering не позволяет начать новый old-cycle READY delivery attempt; claim-first already-started attempt остаётся отдельным durable delivery lifecycle.

## IR-9 diagnostics architecture

Production projection flow:

```text
durable IR-1—IR-8 state
→ coherent exact-session read
→ RuntimeStatusSnapshot / RuntimeTimeline
→ client-specific renderer
```

Diagnostics — только `READ / DERIVE`.

Ни Telegram status text, ни Web JSON, ни CLI rendering, ни merged timeline не становятся semantic authority для admission/control/finalization/recovery.

### Coherent read boundary

Filesystem implementation использует short existing exact-session coordination. Под этой boundary читается только bounded structured metadata current session/current generation.

После release lock выполняются:

- localization;
- Telegram edit/send;
- Web serialization;
- CLI rendering.

Network/LLM/tool await и large content scans под session coordination не выполняются.

## Privacy-safe generic projection

Generic status/timeline не включает raw:

- user text;
- LLM/system messages;
- prompts;
- tool arguments/results;
- file/artifact contents;
- tokens/API keys;
- callback auth;
- arbitrary response route metadata;
- filesystem paths;
- exception traceback.

Допустимая основа projection:

- stable IDs;
- enum states;
- sequences/watermarks;
- counts;
- bounded timestamps/ages;
- safe categorical reason codes.

## Runtime status

`RuntimeStatusSnapshot` отделяет `process_readiness` от durable `session_status`.

Projection производно отражает:

- generation;
- active cycle/context revision;
- accepted/applied input sequences;
- queued/claimed/applying/applied counts;
- oldest queued age;
- pending/applied control sequence;
- effective control state;
- handoff state;
- emissions by lifecycle state;
- finalization state;
- safe current/last issue code;
- initial request/addendum state;
- safe recovery notice when current-process evidence exists.

Old-generation records не входят в current counts.

## Bounded timeline

`RuntimeTimeline` — merged display projection existing durable history.

- default `limit = 20`;
- maximum `limit = 100`;
- deterministic ordering for equal durable timestamps;
- each source stream keeps its authoritative sequence/identity;
- cross-stream timestamp + stable tie-break is display-only ordering;
- no global semantic transaction sequence is invented;
- no new durable event store/WebSocket/event bus is introduced.

Timeline ordering никогда не используется для runtime correctness.

## Addendum projections

IR-9 завершает lifecycle, который исторически был deferred from IR-6/IR-7:

```text
input_addendum_admitted
input_addendum_applying
input_addendum_applied
input_addendum_cancelled
input_addendum_failed
```

Источник projection — existing durable admission/inbox/session/snapshot/generation authority. Отдельная durable addendum state machine не создаётся.

Identity использует durable IDs (`input_batch_id`, cycle/admission identity, cycle sequence, generation), а не Telegram message ID.

### Running/paused/waiting acknowledgement

Client projection различает:

- `QUEUED_RUNNING` — accepted/queued for current running cycle;
- `QUEUED_PAUSED` — accepted/queued while cycle remains paused;
- `RESUME_WAITING` — real user reply admitted into same waiting cycle.

Queued acknowledgement не сообщает, что addition уже applied.

### Applied completion

После durable checkpoint `INPUT_APPLIED` runtime публикует только transient structured presentation event для exact `input_batch_id`/cycle sequence.

Это событие:

- не создаёт semantic `AgentEmission`;
- не добавляет assistant/client message в LLM history;
- не меняет admission/inbox watermarks;
- не становится второй durable event authority.

## Control projections

### Stop

Client-visible моменты различаются:

```text
pause command accepted
→ pause requested
→ durable PAUSED_BY_USER
```

Command acceptance не представляется как уже завершённая pause.

### Continue

Projection различает:

- continue accepted;
- same-cycle resumed;
- already-running/no-op where applicable;
- `still_waiting_for_input` для `WAITING_USER` без real reply;
- stale/reset-fenced outcome.

`WAITING_USER + /continue` без input не отображается как новый LLM execution.

### Reset

Reset projection показывает safe generation/session reset outcome без internal paths/deleted-record dump. Durable IR-5 generation semantics не меняются.

## Recovery notices

`INTERRUPTED` и `AMBIGUOUS` — разные semantic outcomes.

- interrupted work может быть представлено как прерванное;
- ambiguous external work представляется консервативно: оно могло частично выполниться, automatic replay disabled.

Projection не утверждает `definitely failed`, если durable recovery contract говорит UNKNOWN/AMBIGUOUS.

## Emission diagnostics

Status/timeline может показывать:

- emission ID/cycle/generation;
- READY/DELIVERING/DELIVERED/FAILED/UNKNOWN/CANCELLED;
- importance;
- safe timestamps;
- attempt count;
- safe reason code.

Raw emission text и trusted route secrets в generic diagnostics не возвращаются.

## Finalization diagnostics

Projection отражает реально существующие IR-7 states, включая:

- `PREPARED`;
- `ABORTED_NEW_INPUT`;
- `ABORTED_CONTROL`;
- `RESULT_PERSISTED`;
- `OUTPUT_READY`;
- `TERMINAL_COMMITTED`;
- recoverable/terminal failure states.

Terminal client state требует valid matching finalization/handoff/output authority. Session DONE не обходится как самостоятельный source of truth.

## Telegram `/status`

Production `/status` — read-only consumer общей structured diagnostics DTO.

Он:

- resolve'ит trusted session server-side;
- не создаёт `CommittedInputBatch`;
- не создаёт control command/sequence;
- не будит runner;
- не меняет watermarks/generation;
- не создаёт `AgentEmission`;
- сохраняет architectural assumption, что `/status` не участвует в collection semantic FIFO barrier.

Renderer локализует compact output через existing RU/EN catalogs.

## Telegram edit/fallback fencing

Presentation lifecycle остаётся client-specific и не входит в application DTO authority.

IR-9 production policy:

- deterministic edit impossibility → bounded one-send fallback;
- fallback handle может быть локально rebound в existing presentation lifecycle;
- ambiguous edit/send outcome → no blind duplicate;
- session generation + presentation revision fence suppresses stale queued/old-generation update;
- existing generic progress version queue + terminal edit barrier prevents stale progress from winning after terminal presentation.

Semantic intermediate `AgentEmission` остаётся отдельным message; status editing не подменяет IR-6 delivery.

## Web/API

Structured diagnostics endpoints:

- `GET /runtime/status`;
- `GET /runtime/timeline`.

Web/API получает semantic DTO/JSON, а не Telegram-localized strings. Session scope resolve'ится из trusted existing auth/session context; ordinary Web caller не получает authority читать произвольную Telegram/internal session.

## CLI

Отдельного standalone production runtime CLI framework сейчас нет. IR-9 предоставляет общий DTO-to-CLI renderer/consumer path, но не создаёт второй repository-scanning business logic framework.

## Localization

IR-9 user-visible runtime projection strings используют existing localization subsystem. RU/EN catalogs синхронизированы; deterministic tests проверяют required keys и formatter placeholders.

## Configuration

IR-9 не добавляет config/environment fields. `.env.example` и `src/api/mcp.config.example` не требуют новых IR-9 knobs. Existing deterministic config-example audit остаётся green в full `537 passed` regression.

## Acceptance state

IR-9 client-projection acceptance подтверждает:

- coherent exact-session/current-generation reads;
- no raw-content/secret leakage;
- deterministic bounded timeline;
- addendum admitted/applying/applied/cancelled/failed projections;
- stop accepted != paused;
- continue accepted != resumed/still waiting;
- INTERRUPTED != AMBIGUOUS;
- UNKNOWN != FAILED;
- terminal projection requires matching IR-7/IR-8 authority;
- `/status` does not mutate durable runtime;
- Web and CLI consume the shared semantic DTO;
- Telegram deterministic edit fallback and ambiguity policy;
- stale presentation writes are fenced;
- RU/EN localization parity;
- configuration examples require no new fields and audit remains green.

IR-10 remains planned for randomized/restart/synthetic/live release-final acceptance. Historical statements that IR-9 was deferred from IR-6/IR-7 describe those earlier stage boundaries only; complete projections are now implemented by IR-9.
