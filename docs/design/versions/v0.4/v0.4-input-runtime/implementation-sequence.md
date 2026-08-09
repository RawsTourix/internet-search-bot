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

Последовательность выполняется по durable ownership boundaries, а не по transport UI.

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

Общий `v0.4-input-runtime` остаётся `partial` до IR-10.

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

Client projection выполняется после того, как durable semantic authority уже определена. Поэтому IR-9 читает IR-1—IR-8 records, но не добавляет новую authority layer.

## Historical stage boundaries

### IR-1

Созданы domain models/configuration и storage-neutral repository ports. Stable IDs, generation, watermarks и state validation принадлежат domain layer.

### IR-2

Реализованы filesystem adapters, atomic writes, exact-session coordination, claim/identity/index recovery и repository recreation contracts. Future transactional backend может реализовать те же ports без application dependency на filesystem layout.

### IR-3

`CommittedInputBatch` проходит один admission service. Initial input запускает один cycle; active-cycle additions durable enqueue'ятся в FIFO и не создают parallel runner. Runtime handoff фиксирует external execution boundary и no-blind-replay ambiguity.

### IR-4

Initial `R1`, active snapshot, accepted-at-entry watermarks, bounded contiguous `CycleInputApplier`, linear context revisions и snapshot-first apply protocol закрывают same-cycle input application. WAITING reply использует common FIFO checkpoint path.

### IR-5

Durable `SessionControlCommand` реализует pause/continue/reset. Stop cooperative и применяется только на safe checkpoint. Continue возобновляет тот же cycle с frozen target, выделенным внутри shared durable coordination. Reset generation является durable authority.

### IR-6

Semantic intermediate `AgentEmission` отделён от transient progress, question и final output. Runtime-owned route/provenance/idempotency и durable READY/DELIVERING/DELIVERED|FAILED|UNKNOWN|CANCELLED lifecycle не зависят от Telegram UI.

### IR-7

`CycleFinalizationRecord`, repeated authoritative rechecks и exact `RuntimeHandoff` relation закрывают terminal races. `OutputBatch READY` не является terminal authority; delivery eligibility возникает после matching terminal commit. Admission allocation и terminal commit используют общий exact-session durable ordering.

### IR-8

Startup gate/recovery выполняет deterministic durable reconciliation до ordinary work. PAUSED/WAITING сохраняют same-cycle context, safe RUNNING может rehydrate'иться, ambiguous handoff не replay'ится. Invalid terminal authority оставляет process recovery FAILED, а не repair по догадке.

## IR-9 — Client projections, diagnostics и configuration examples

### Status

Implemented and validated на code/test boundary:

`068f8f6682e7b7b805b60dbb640b53b671cc8565`

Exact code CI evidence:

- `Validate Input Runtime` #685 — completed / success;
- production compile — success;
- focused IR-8 — `49 passed`, `0 failed`;
- focused IR-9 — `101 passed`, `0 failed`;
- full input-runtime/config audit — `537 passed`, `0 failed`;
- `Validate v0.4 file artifacts PR` #803 — completed / success;
- token permissions — `Contents: read`, `Metadata: read`.

Workflow stdout не используется для выдуманного skipped count: если summary его не печатает, он не записывается как factual evidence.

### Goal

IR-9 завершает client-facing read model после IR-8:

```text
durable IR-1—IR-8 authority
→ coherent exact-session diagnostics read
→ RuntimeStatusSnapshot / RuntimeTimeline
→ Telegram / Web / CLI renderer
```

Application diagnostics остаётся `READ / DERIVE only` и никогда не становится admission/control/finalization/recovery authority.

### Step 1 — transport-neutral DTO/query

Реализован structured status/timeline contract, который не импортирует Telegram presentation или FastAPI response classes и не раскрывает filesystem directory layout.

Status включает bounded current-session/current-generation semantic metadata:

- process readiness;
- session/cycle status;
- active cycle/context revision;
- accepted/applied input sequences;
- queue/apply counts и oldest queued age;
- control watermarks/effective state;
- handoff/emission/finalization states;
- safe current/last issue code;
- initial/addendum/recovery projection.

### Step 2 — coherent filesystem reader

Filesystem diagnostics использует существующую short exact-session coordination.

Под coordination разрешены только bounded durable reads. За lock остаются:

- localization;
- rendering;
- Telegram send/edit;
- HTTP serialization;
- CLI output;
- all network/LLM/tool awaits.

Race tests допускают coherent before/after snapshot, но не torn watermark/count combination.

### Step 3 — privacy-safe projection

Generic diagnostics не возвращает raw:

- user/LLM/system content;
- prompts;
- tool arguments/results;
- file contents;
- tokens/API keys/callback auth;
- arbitrary response-route metadata;
- filesystem paths;
- traceback.

Используются safe IDs/enums/sequences/counts/timestamps/ages/reason codes.

### Step 4 — bounded timeline

`RuntimeTimeline` является projection, а не event sourcing authority.

- default `limit = 20`;
- maximum `limit = 100`;
- deterministic same-timestamp ordering;
- per-stream sequence/identity retained;
- cross-stream order — display-only;
- no global semantic sequence;
- no new durable event bus/WebSocket stream.

### Step 5 — input/addendum projections

IR-9 реализует:

```text
input_addendum_admitted
input_addendum_applying
input_addendum_applied
input_addendum_cancelled
input_addendum_failed
```

`QUEUED_RUNNING`, `QUEUED_PAUSED`, `RESUME_WAITING` получают разные semantic acknowledgements. Queued addition не объявляется applied раньше snapshot/inbox authority.

После durable APPLIED публикуется только transient presentation projection для exact `input_batch_id`; он не создаёт `AgentEmission`, не мутирует LLM history и не меняет durable admission state.

### Step 6 — control/recovery projections

Client различает:

```text
pause accepted != PAUSED_BY_USER
continue accepted != resumed != still_waiting_for_input
INTERRUPTED != AMBIGUOUS
UNKNOWN delivery != FAILED
```

Ambiguous external work не объявляется definitely failed и не запускается повторно projection layer.

### Step 7 — finalization/emission projection

Emission diagnostics видит READY/DELIVERING/DELIVERED/FAILED/UNKNOWN/CANCELLED без raw text/route secret.

Finalization diagnostics видит existing IR-7 states. Session DONE не обходится как standalone terminal authority: valid terminal client state требует matching finalization/handoff/output evidence.

### Step 8 — Telegram

Production `/status` подключён как read-only high-priority consumer общей DTO и сохраняет collection FIFO bypass.

Он не:

- создаёт `CommittedInputBatch`;
- выделяет control sequence;
- будит runner;
- меняет watermarks/generation;
- создаёт `AgentEmission`.

Addendum presentation использует presentation-level generation/revision fencing. Deterministic edit impossibility допускает bounded fallback send; ambiguous edit/send не приводит к blind duplicate. Existing generic progress fencing не позволяет stale progress перезаписать terminal presentation.

### Step 9 — Web/API и CLI

Structured API endpoints:

- `GET /runtime/status`;
- `GET /runtime/timeline`.

Web/API сериализует DTO, а не парсит Telegram localization strings. Session scope следует existing trusted API auth/session convention.

Standalone production runtime CLI framework не добавлен: shared DTO renderer представляет CLI-compatible consumer path без отдельной business logic ветки.

### Step 10 — localization/config examples

RU/EN projection keys синхронизированы existing localization layer и проверяются deterministic parity tests.

IR-9 не добавляет новых config/environment fields. `.env.example` и `src/api/mcp.config.example` остаются без искусственных IR-9 settings; existing configuration-example audit входит в green `537 passed` regression.

## IR-9 Done boundary

IR-9 считается implemented, потому что подтверждены:

- transport-neutral diagnostics/query contract;
- durable authority only, READ/DERIVE projections;
- coherent exact-session/current-generation status;
- bounded deterministic timeline без global semantic sequence;
- privacy/no-content-leak boundary;
- initial/addendum/control/recovery/emission/finalization projections;
- read-only Telegram `/status`;
- deterministic presentation fallback + stale edit fencing;
- structured Web/API consumer;
- shared DTO CLI renderer без standalone CLI framework;
- RU/EN localization parity;
- no new config fields + green configuration-example audit;
- deterministic IR-9 and full input-runtime regression;
- production compile;
- code workflows green.

## IR-10 — planned

IR-10 начинается только после IR-9 documentation/evidence closure и владеет:

- randomized race repetitions;
- randomized corruption/restart permutations;
- synthetic whole-system marathon;
- live Telegram maintainer acceptance;
- release-final real-service acceptance/report.

Corrective IR-9 documentation pass не выполняет эти действия.

## Explicitly deferred outside current stage

Не добавляются:

- Telegram edited-message history rewind;
- PostgreSQL / SQLAlchemy / Alembic;
- Redis/distributed workers/leases;
- scheduler / `AgentRun` / `TaskRun`;
- parallel branches/fork-join;
- durable global event bus.

## Commit discipline

Каждый stage закрывается forward real commits. История не должна переписываться reset/rebase/squash/force-push. Documentation evidence записывает только существующие GitHub SHAs/workflow runs и не создаёт empty/trigger commits для «красивых» чисел.
