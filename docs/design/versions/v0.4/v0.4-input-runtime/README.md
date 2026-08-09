---
id: design.v0.4.input-runtime
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-09
---

# v0.4-input-runtime

## Статус реализации

Текущий stage status:

- `IR-1 — Domain models, configuration и repository ports`: implemented;
- `IR-2 — Filesystem repositories и coordination`: implemented;
- `IR-3 — Admission service и initial-cycle integration`: implemented;
- `IR-4 — Active snapshot, checkpoints и context revisions`: implemented;
- `IR-5 — Durable /stop, /continue, /reset control plane`: implemented;
- `IR-6 — AgentEmission и intermediate messages`: implemented;
- `IR-7 — Finalization barrier и terminal authority`: implemented;
- `IR-8 — Startup recovery, reconstruction и readiness`: implemented;
- `IR-9 — Client projections, diagnostics и configuration examples`: implemented;
- `IR-10 — Full race/restart/synthetic/live acceptance`: planned.

Общий update остаётся `partial`: IR-9 завершён, но release-final acceptance IR-10 ещё не выполнен.

## Canonical documents

- [domain models and state machines](domain-models-and-state-machines.md)
- [admission and cycle inbox](admission-and-cycle-inbox.md)
- [checkpoints and context revisions](checkpoints-and-context-revisions.md)
- [control plane pause/resume](control-plane-pause-resume.md)
- [agent emissions and client projections](agent-emissions-and-client-projections.md)
- [finalization and recovery](finalization-and-recovery.md)
- [implementation sequence](implementation-sequence.md)
- [contracts and acceptance](contracts-and-acceptance.md)

## Semantic authority

IR-9 не создаёт новую runtime state machine. Semantic authority остаётся только в durable IR-1—IR-8 records:

- `SessionInputRuntimeState`;
- `InputAdmissionRecord`;
- `CycleInboxItem`;
- `SessionControlCommand`;
- `ActiveCycleSnapshot`;
- `CycleContextRevision`;
- `RuntimeHandoff`;
- `AgentEmission`;
- `CycleFinalizationRecord`;
- `OutputBatch`;
- durable recovery evidence.

Client projections читают и производно представляют эту authority, но не участвуют в admission/control/finalization/recovery decisions.

## Реализованные stage boundaries

### IR-1 — Domain foundation

Определены durable models, stable IDs, generation/watermark invariants, configuration и storage-neutral repository ports. Application contract не зависит от filesystem layout и остаётся пригодным для future transactional backend.

### IR-2 — Filesystem repositories

Реализованы atomic durable records, exact-session coordination, identity/index recovery, bounded claims и recreation-safe filesystem adapters. Coordination применяется только к коротким state transitions и не удерживается во время LLM/tool/network work.

### IR-3 — Admission

Каждый `CommittedInputBatch` проходит один transport-neutral admission service. Initial input создаёт один cycle, additions active cycle получают durable FIFO relation и не запускают второй runner. Runtime handoff отделяет безопасный pre-handoff retry от ambiguous post-handoff execution.

### IR-4 — Active snapshot и checkpoints

Initial `R1`, `ActiveCycleSnapshot`, accepted/applied watermarks, bounded `CycleInputApplier`, linear context revisions и snapshot-first apply protocol реализованы. Input применяется только на protocol-safe checkpoints; WAITING reply использует тот же FIFO path.

### IR-5 — Control plane

`SessionControlCommand` является durable authority для pause/continue/reset. `/stop` cooperative и становится фактически paused только после safe checkpoint. `/continue` возобновляет тот же cycle с atomically frozen resume target. `/reset` продвигает durable generation ровно один раз и fences old work.

### IR-6 — Durable AgentEmission

Semantic intermediate message отделён от transient `ProgressEvent`, Question/`WAITING_USER` и final `OutputBatch`.

Lifecycle:

```text
READY
→ DELIVERING
→ DELIVERED | FAILED | UNKNOWN | CANCELLED
```

`UNKNOWN` означает delivery ambiguity и не re-arm-ится как READY для blind replay.

### IR-7 — Finalization

`CycleFinalizationRecord` и exact runtime handoff дают terminal barrier. Persisted result и `OutputBatch READY` не являются terminal authority сами по себе. Valid final authority требует matching finalization/handoff/output evidence и `TERMINAL_COMMITTED`.

Admission allocation и terminal commit упорядочены общей durable exact-session coordination, поэтому late input либо aborts stale finalization, либо после already-won terminal authority начинает новый cycle.

### IR-8 — Startup recovery

Startup выполняет deterministic durable reconciliation до ordinary runtime work:

```text
RECOVERING
→ durable reconciliation/reconstruction
→ MCP connect
→ recovered cycle/runner installation
→ READY
```

PAUSED/WAITING восстанавливаются как тот же cycle/context. Ambiguous handoff не replay-ится автоматически. Existing invalid terminal authority приводит к controlled recovery failure, а не к reconstruction по догадке.

## IR-9 — Client projections, diagnostics и configuration examples

IR-9 реализован на code/test boundary:

`068f8f6682e7b7b805b60dbb640b53b671cc8565`

Фактическое code CI evidence на exact SHA:

- `Validate Input Runtime` #685 — completed / success;
- production compile — success;
- focused IR-8 — `49 passed`, `0 failed`;
- focused IR-9 — `101 passed`, `0 failed`;
- full `tests/test_input_runtime_*.py + tests/test_artifact_configuration_examples.py` — `537 passed`, `0 failed`;
- `Validate v0.4 file artifacts PR` #803 — completed / success;
- workflow token permissions — `Contents: read`, `Metadata: read`.

Skipped count не записывается как evidence, потому что relevant workflow stdout его не печатает.

### Diagnostics ownership

Production query flow:

```text
durable IR-1—IR-8 authority
→ coherent exact-session read
→ RuntimeStatusSnapshot / RuntimeTimeline
→ Telegram / Web / CLI renderer
```

Diagnostics — только `READ / DERIVE`. Ни status snapshot, ни timeline, ни client presentation не становятся authority для admission, control, finalization или recovery.

Application DTO/query layer не зависит от Telegram UI, FastAPI response classes, filesystem directories или raw JSON record layout.

### Coherent filesystem read

Filesystem diagnostics использует существующую short exact-session coordination boundary. Под coordination собирается только bounded structured metadata current session/current generation.

Под runtime lock не выполняются:

- localization/formatting;
- Telegram send/edit;
- HTTP response serialization;
- CLI rendering;
- LLM/tool/network operations;
- длинный historical content scan.

Concurrent admission/control/finalization status query может linearize до или после transition, но не возвращает torn combination watermarks/counts из разных snapshots.

### Safe status

`RuntimeStatusSnapshot` разделяет process readiness и durable session cycle status и производно отражает:

- generation;
- active cycle/context revision;
- accepted/applied input sequence;
- queued/claimed/applying/applied counts;
- oldest queued age;
- pending/applied control sequence и effective control state;
- runtime handoff state;
- emission states/counts;
- finalization state;
- safe current/last issue code;
- initial request/addendum state;
- safe recovery notice, когда current-process evidence доступен.

Current counts фильтруются по authoritative current generation. Old-generation records могут оставаться audit evidence, но не смешиваются с current status.

### Privacy / no-content-leak contract

Generic status/timeline по умолчанию не содержат:

- raw user text;
- LLM/system messages;
- prompts;
- tool arguments/results;
- file/artifact contents;
- tokens/API keys;
- callback auth;
- arbitrary response-route metadata;
- internal filesystem paths;
- raw exception traceback.

Projection использует IDs, enums, sequences, counts, safe timestamps/ages и bounded reason codes.

### Bounded deterministic timeline

`RuntimeTimeline` — projection existing durable streams, а не новый durable event store.

- default `limit = 20`;
- maximum `limit = 100`;
- deterministic ordering при одинаковых durable timestamps;
- per-stream authoritative sequence/identity сохраняется;
- cross-stream timestamp/tie-break ordering — только display ordering;
- global semantic runtime sequence не изобретается;
- WebSocket/event bus/Kafka-like log не добавлялись.

### Initial request и addendum

Initial START_CYCLE projection отражает только состояния, основанные на durable authority: admitted/running, waiting, pause requested/paused, interrupted/finalizing/terminal.

Addendum projection реализует:

```text
input_addendum_admitted
input_addendum_applying
input_addendum_applied
input_addendum_cancelled
input_addendum_failed
```

Acknowledgement различает `QUEUED_RUNNING`, `QUEUED_PAUSED` и `RESUME_WAITING`. Queued addition не представляется как already applied.

После durable checkpoint authority `INPUT_APPLIED` испускается только transient structured projection event для exact `input_batch_id`; он не создаёт `AgentEmission`, не добавляет client status в LLM history и не меняет admission/checkpoint state.

### Controls

Client projection различает:

- pause command accepted;
- фактический `PAUSED_BY_USER`;
- continue accepted;
- same-cycle resumed;
- `still_waiting_for_input` для `WAITING_USER` без real reply;
- safe stale/reset-fenced outcomes.

Client текст не может объявить cycle paused/running раньше durable authority.

### Recovery

`INTERRUPTED` и `AMBIGUOUS` являются разными client outcomes. Если external work мог частично выполниться, projection не сообщает `definitely failed` и не инициирует automatic replay.

### Emissions

Diagnostics показывает lifecycle `READY`, `DELIVERING`, `DELIVERED`, `FAILED`, `UNKNOWN`, `CANCELLED` без raw emission text/route secrets. `UNKNOWN` остаётся ambiguity и не отображается как `FAILED`.

### Finalization и terminal projection

Finalization projection отражает durable states, включая PREPARED/ABORTED/RESULT_PERSISTED/OUTPUT_READY/TERMINAL_COMMITTED/failure states.

`SessionInputRuntimeState == DONE` сам по себе не является достаточной client terminal authority. Terminal projection требует matching IR-7/IR-8 terminal evidence.

### Telegram `/status`

Production `/status` — read-only high-priority consumer shared diagnostics DTO.

Он:

- resolve'ит trusted Telegram session server-side;
- не создаёт `CommittedInputBatch`;
- не создаёт control sequence;
- не будит runner;
- не меняет watermarks/generation;
- не создаёт `AgentEmission`;
- сохраняет existing rule, что `/status` не входит в collection semantic FIFO barrier.

Compact status локализуется через existing RU/EN catalogs.

### Telegram presentation fencing

Addendum status использует presentation handle как UI identity, а не semantic input identity.

- deterministic edit impossibility → bounded one-send fallback и optional presentation rebind;
- ambiguous edit/send outcome → no blind duplicate;
- generation + presentation revision fencing подавляет stale queued/old-generation write;
- generic progress path сохраняет existing version queue/terminal edit barrier, поэтому stale progress не может стать последним после terminal presentation.

Semantic `AgentEmission` остаётся отдельным message и не превращается в status edit.

### Web/API

Structured diagnostics доступны через existing API composition:

- `GET /runtime/status`;
- `GET /runtime/timeline`.

Web request scope resolve'ится из trusted auth/session context. Arbitrary Telegram/internal session injection для ordinary Web key не разрешён; explicit internal scope следует existing API convention.

### CLI

Standalone production runtime CLI framework в текущем проекте отсутствует. IR-9 не создаёт новый framework: существует общий DTO-to-CLI renderer/consumer path без отдельного repository-scanning business logic.

### Localization

RU/EN projection keys синхронизированы в existing catalogs. Focused tests проверяют required key parity и formatter placeholders.

### Configuration examples

IR-9 не добавляет новых configuration/environment fields. `.env.example` и `src/api/mcp.config.example` поэтому не получают искусственных IR-9 knobs.

Existing deterministic configuration-example audit остаётся частью green full regression `537 passed`.

## IR-10 — planned

IR-10 остаётся единственным незавершённым stage этого update и владеет release-final acceptance:

- randomized race repetitions;
- corruption/restart permutations;
- synthetic whole-system marathon;
- maintainer live Telegram acceptance;
- real-service acceptance/smoke в границах IR-10;
- release-final evidence.

IR-9 не начинает эту работу.

## Deferred вне IR-9/IR-10

Не реализуются этим corrective documentation pass:

- Telegram edited-message history rewind;
- PostgreSQL / SQLAlchemy / Alembic;
- Redis/distributed workers/leases;
- distributed runtime;
- scheduler;
- `AgentRun` / `TaskRun`;
- parallel branches / fork-join;
- новый durable global event bus.
