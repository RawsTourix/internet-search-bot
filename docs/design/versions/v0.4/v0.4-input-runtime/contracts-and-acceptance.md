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

`v0.4-input-runtime = partial`: IR-9 закрывает deterministic client-projection/diagnostics boundary, но release-final IR-10 acceptance ещё не выполнялась.

## IR-9 implementation evidence

Code/test boundary:

`068f8f6682e7b7b805b60dbb640b53b671cc8565`

Exact GitHub Actions evidence для этого SHA:

- `Validate Input Runtime` #685 — completed / success;
- production compile — success;
- focused IR-8 restart contracts — `49 passed`, `0 failed`;
- focused IR-9 projection contracts — `101 passed`, `0 failed`;
- full `tests/test_input_runtime_*.py + tests/test_artifact_configuration_examples.py` — `537 passed`, `0 failed`;
- `Validate v0.4 file artifacts PR` #803 — completed / success;
- workflow token permissions — `Contents: read`, `Metadata: read`.

Relevant workflow stdout не печатает skipped summary, поэтому skipped count здесь не выдумывается.

## Authority contract

IR-9 не меняет semantic authority IR-1—IR-8.

Authoritative durable state остаётся в:

```text
SessionInputRuntimeState
InputAdmissionRecord
CycleInboxItem
SessionControlCommand
ActiveCycleSnapshot
CycleContextRevision
RuntimeHandoff
AgentEmission
CycleFinalizationRecord
OutputBatch
recovery evidence
```

Client diagnostics contract:

```text
durable authority
→ coherent exact-session read
→ RuntimeStatusSnapshot / RuntimeTimeline
→ client renderer
```

Status/timeline/presentation являются только `READ / DERIVE`. Они не используются как input admission, control, finalization, output-delivery или recovery authority.

## Coherent status contract

Filesystem diagnostics использует существующую short exact-session coordination boundary.

Под coordination допускается только bounded structured read current session/current generation/current cycle metadata. Под lock не выполняются:

- formatting/localization;
- Telegram send/edit;
- HTTP/Web serialization;
- CLI output;
- network/LLM/tool awaits;
- unbounded historical content scan.

### Concurrency acceptance

`status ↔ admission` допускает два coherent linearization outcomes:

```text
status first
→ accepted/queue before new input

admission first
→ accepted/queue including new input
```

Недопустимо сочетать новый accepted watermark с queue/count snapshot до этого admission.

`status ↔ control acceptance` аналогично допускает before/after snapshot, но pending/applied control fields должны принадлежать одной coherent durable view.

`status ↔ terminal commit` допускает pre-terminal или post-terminal view. Normal client projection не должна возвращать `session DONE` без matching finalization/handoff/output terminal authority.

## Runtime status contract

`RuntimeStatusSnapshot` отделяет process readiness от durable session status.

Bounded safe fields включают:

- process readiness;
- session/cycle status;
- generation;
- active cycle/context revision;
- accepted/applied input sequences;
- queued/claimed/applying/applied counts;
- oldest queued age;
- pending/applied control sequence;
- effective control state;
- runtime handoff state;
- emissions by state;
- finalization state;
- safe current/last issue code;
- initial request/addendum projection;
- safe recovery notice when current-process evidence exists.

Current counts относятся только к authoritative current generation. Old-generation audit evidence не смешивается с current queue/emission/finalization state.

## Privacy acceptance

Generic diagnostics/timeline **не содержат raw**:

- user text;
- LLM/system messages;
- prompts;
- tool arguments/results;
- file/artifact contents;
- tokens/API keys;
- callback auth;
- arbitrary response-route metadata;
- internal filesystem paths;
- raw traceback/exception dump.

Допустимы stable IDs, enums, sequences, counts, bounded timestamps/ages и safe reason codes.

Cross-session isolation является обязательной: query session A не может включать IDs/counts/issues/emissions session B, даже если external transport message IDs численно совпадают.

## Timeline contract

`RuntimeTimeline` — bounded deterministic projection existing durable history.

- default limit = `20`;
- maximum limit = `100`;
- per-stream authoritative sequence/identity сохраняется;
- equal durable timestamps используют stable deterministic display tie-break;
- cross-stream ordering не является semantic correctness authority;
- global runtime transaction sequence не изобретается;
- новый durable event store/global event bus/WebSocket log не добавляется.

Repeated build из одинаковой durable state должен давать одинаковый ordered structured result.

## Initial input contract

```text
idle session + committed batch
→ one admission sequence 0
→ one AgentCycle
→ initial request projection
→ running / waiting / paused / interrupted / finalizing / terminal according to durable authority
```

Client не создаёт fake progress state, не подтверждённый durable state. Transient `ProgressEvent` может давать cosmetic detail, но не заменяет status authority.

## Addendum contract

Running/paused/waiting additions остаются existing durable admission/inbox state. IR-9 только производит client lifecycle:

```text
input_addendum_admitted
input_addendum_applying
input_addendum_applied
input_addendum_cancelled
input_addendum_failed
```

Projection сохраняет durable semantic identity, включая applicable `input_batch_id`, cycle/admission identity, cycle sequence и generation. Transport message ID не является addendum identity.

### Admission acknowledgement

- `QUEUED_RUNNING` → accepted and queued for current running task;
- `QUEUED_PAUSED` → accepted and queued while current task remains paused;
- `RESUME_WAITING` → actual user reply admitted into the same waiting cycle.

Queued acknowledgement никогда не обещает `already applied`.

### Applied completion

`input_addendum_applied` появляется только после snapshot/inbox/checkpoint authority подтверждает APPLIED.

Transient applied-presentation event:

- не создаёт `AgentEmission`;
- не добавляет assistant text в LLM history;
- не меняет admission state;
- не создаёт вторую durable addendum state machine.

## Control projection contract

### Stop

Client обязан различать:

```text
command accepted / pause requested
!=
durable PAUSED_BY_USER
```

Accepted stop не называется «уже остановлено», пока safe checkpoint не зафиксировал pause authority.

### Continue

Client обязан различать:

- continue accepted;
- same-cycle resumed;
- already-running/no-op where existing contract applies;
- `still_waiting_for_input` для WAITING без real user reply;
- stale/reset-fenced outcome.

`WAITING_USER + /continue` без new input не представляется как новый LLM execution.

### Reset

Projection может сообщить safe reset/generation outcome, но не выводит deleted storage paths/records и не меняет existing IR-5 reset semantics.

## Recovery projection contract

`INTERRUPTED != AMBIGUOUS`.

Если durable recovery evidence говорит, что external operation могла частично выполниться, client projection сообщает ambiguity conservatively и не утверждает, что operation точно failed/not executed.

`AMBIGUOUS` projection сообщает, что automatic replay disabled; сам diagnostics layer replay не запускает.

Process readiness (`RECOVERING`, `READY`, `FAILED`, `STOPPING`, `STOPPED`) не смешивается с durable session cycle status.

## AgentEmission diagnostics contract

Lifecycle states остаются:

```text
READY
DELIVERING
DELIVERED
FAILED
UNKNOWN
CANCELLED
```

Diagnostics может включать safe identity/state/importance/timestamps/attempt count/reason code, но не raw emission text или trusted route secret.

`UNKNOWN` никогда не нормализуется в `FAILED`: ambiguity сохраняется и не создаёт blind replay authority.

## Finalization diagnostics contract

Projection использует реально существующие IR-7 states, включая:

- `PREPARED`;
- `ABORTED_NEW_INPUT`;
- `ABORTED_CONTROL`;
- `RESULT_PERSISTED`;
- `OUTPUT_READY`;
- `TERMINAL_COMMITTED`;
- recoverable/terminal failure states.

Persisted result или `OutputBatch READY` не равны delivered/terminal success.

Client terminal state требует matching IR-7/IR-8 authority. `SessionInputRuntimeState == DONE` сам по себе не обходится как terminal source of truth.

## Telegram `/status` acceptance

Production `/status` является read-only high-priority consumer shared diagnostics DTO.

Mandatory invariants:

- trusted Telegram session resolved server-side;
- no `CommittedInputBatch` created;
- no control sequence allocated;
- runner not woken;
- input/control watermarks unchanged;
- generation unchanged;
- no `AgentEmission` created;
- collection semantic FIFO barrier bypass preserved;
- legacy duplicate status handling suppressed after high-priority response.

Status output compact/localized и не превращается в raw repository dump.

## Telegram edit/fallback acceptance

Presentation failure не мутирует runtime semantic state.

Policy:

- deterministic not-editable outcome → one bounded fallback send;
- successful fallback may rebind existing presentation handle;
- ambiguous edit/send network outcome → no blind duplicate send;
- stale presentation revision cannot overwrite newer applied status;
- old process-local Telegram generation cannot overwrite post-reset presentation;
- generic progress version/terminal barrier prevents stale progress from winning after terminal output.

Presentation fencing остаётся presentation-only и не становится durable input authority.

Intermediate semantic `AgentEmission` доставляется отдельным new message и не смешивается с addendum/progress status edit.

## Web/API contract

Web/API получает structured DTO/JSON, не localized Telegram text.

Implemented query endpoints:

- `GET /runtime/status`;
- `GET /runtime/timeline`.

Ordinary Web auth/session context не даёт произвольную Telegram/internal `session_id` authority. Explicit internal scope использует existing API convention, а не новый IR-9 auth model.

Controlled diagnostics errors не должны раскрывать traceback/raw exception strings.

## CLI contract

Standalone production runtime CLI framework в текущем проекте отсутствует.

IR-9 предоставляет общий structured DTO → CLI renderer consumer path. CLI-compatible renderer не сканирует repositories самостоятельно и не парсит Telegram localization strings.

## Localization contract

Все новые user-visible Telegram IR-9 strings проходят existing localization layer.

Acceptance требует:

- required IR-9 keys exist in `ru.json`;
- same required keys exist in `en.json`;
- formatter placeholders compatible;
- translation params — safe scalars, не raw exception/user content.

Focused IR-9 tests закрывают parity.

## Configuration contract

IR-9 не добавил новых configuration knobs или production environment reads.

Следовательно:

- `.env.example` не требует IR-9 key additions;
- `src/api/mcp.config.example` не требует IR-9 fields;
- existing deterministic configuration-example audit остаётся source of truth.

Он входит в full green regression `537 passed`.

## Deterministic acceptance matrix

IR-9 focused suite подтверждает как минимум:

- coherent running/paused/waiting/interrupted/terminal status;
- no semantic mutation from status;
- exact cross-session isolation;
- status races with admission/control/terminal commit;
- fake-clock oldest queued age;
- addendum admitted/applying/applied/reset-cancelled/failure projections;
- stop accepted vs actually paused;
- continue same-cycle vs still waiting;
- recovery interruption/ambiguity projection;
- emission lifecycle including UNKNOWN;
- finalization lifecycle;
- bounded deterministic timeline;
- no raw-content/route-secret leakage;
- Web structured DTO;
- CLI shared DTO consumer;
- Telegram `/status` RU/EN;
- deterministic edit fallback;
- ambiguous edit policy;
- stale queued/applied generation/progress fencing;
- localization parity;
- configuration-example audit;
- complete semantic-cycle and fresh-process recovery consumer integration.

Focused IR-9 result on final code boundary: `101 passed`, `0 failed`.

Full input-runtime/config result on final code boundary: `537 passed`, `0 failed`.

## IR-10 boundary

IR-9 deterministic acceptance **не** является IR-10.

IR-10 remains planned and owns:

- thousands/repeated randomized races;
- corruption/restart permutation matrix;
- synthetic whole-system marathon;
- maintainer live Telegram acceptance;
- release-final real-service smoke/report where planned.

## Deferred outside IR-9

Не добавляются:

- Telegram edited-message history rewind;
- PostgreSQL / SQLAlchemy / Alembic;
- Redis/distributed workers/leases;
- scheduler;
- `AgentRun` / `TaskRun`;
- parallel branches/fork-join;
- new durable global event bus.

## Definition of current completion

IR-9 может считаться implemented/validated, когда одновременно истинны:

```text
code boundary == 068f8f6682e7b7b805b60dbb640b53b671cc8565
focused IR-9 == 101 passed / 0 failed
full input-runtime/config == 537 passed / 0 failed
production compile == success
code workflows #685 and #803 == completed/success
all canonical IR-9 docs agree IR-9 implemented / IR-10 planned
final documentation HEAD exists
both workflows on final documentation HEAD == completed/success
PR #6 evidence matches actual history and remains draft/open/unmerged
IR-10 not started
```

The exact final documentation SHA/workflow numbers belong to the closing documentation/PR evidence once GitHub has actually created that SHA and completed both workflows; they must never be predicted in advance.
