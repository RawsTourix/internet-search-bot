---
id: design.v0.4.input-runtime
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-09
---

# v0.4-input-runtime

## Current status

Текущий canonical stage status:

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

`v0.4-input-runtime = partial`: IR-9 реализован и validated, но release-final
acceptance IR-10 ещё не выполнялась.

## Canonical documents и ownership

Этот README является index/current overview, а не полной дублирующей
specification. Подробный canonical ownership распределён так:

- [domain models and state machines](domain-models-and-state-machines.md) —
  durable models, states и transition invariants;
- [admission and cycle inbox](admission-and-cycle-inbox.md) — admission,
  same-cycle FIFO и `RuntimeHandoff` foundation;
- [checkpoints and context revisions](checkpoints-and-context-revisions.md) —
  protocol-safe checkpoints, `CycleInputApplier`, accepted/applied watermarks и
  context revisions;
- [control plane pause/resume](control-plane-pause-resume.md) — durable
  `/stop`, `/continue`, `/reset`;
- [agent emissions and client projections](agent-emissions-and-client-projections.md) —
  durable `AgentEmission`, claim/receipt delivery semantics и IR-9 client
  projections;
- [finalization and recovery](finalization-and-recovery.md) — finalization,
  terminal authority и recovery policy;
- [implementation sequence](implementation-sequence.md) — подробная
  stage-by-stage implementation history, ownership, crash/race boundaries,
  deterministic tests, Definition of Done и explicit deferred scope для IR-1—IR-9;
- [contracts and acceptance](contracts-and-acceptance.md) — общие observable
  functional, protocol-integrity, persistence, idempotency, ordering,
  crash/cancellation и acceptance contracts.

Accepted detail из реализованных IR-1—IR-8 остаётся частью canonical design и не
отменяется появлением IR-9. IR-9 добавляет read/projection layer поверх этих
authorities, а не заменяет их.

## Durable authority

Semantic authority остаётся в durable IR-1—IR-8 records и relations:

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
durable recovery evidence
```

Ключевая ownership chain:

```text
CommittedInputBatch
→ one authoritative admission
→ one active cycle or durable FIFO inbox
→ protocol-safe checkpoint apply
→ durable context/control/emission/finalization state
→ recovery from durable evidence
```

Process-local queues, presentation handles, client status messages и IR-9
diagnostics не являются semantic authority.

## Cross-stage invariant digest

Подробная формулировка находится в thematic docs, implementation sequence и
contracts/acceptance. На уровне overview обязательны следующие invariants:

1. Один committed `input_batch_id` получает ровно одну durable admission relation.
2. Active-cycle addition не запускает второй `process_query()`/parallel AgentCycle:
   она сохраняется в FIFO и применяется тем же cycle.
3. `RuntimeHandoff` разделяет безопасный pre-handoff retry и post-handoff
   ambiguity; неизвестный external side effect не replay-ится blind.
4. Checkpoint фиксирует accepted-at-entry watermark и применяет bounded contiguous
   FIFO range; поздний admission ждёт следующей safe boundary.
5. Один applied range создаёт один semantic `input_batch_update` и следующую
   `CycleContextRevision`; snapshot-first reconciliation не повторяет semantic
   apply после crash.
6. Open assistant tool block остаётся protocol-atomic: user/runtime update не
   вставляется между assistant tool calls и matching `role=tool` results.
7. `/stop` cooperative; command acceptance не равно `PAUSED_BY_USER`.
8. `/continue` возобновляет тот же cycle и использует durable frozen resume
   target; input-before/after continue упорядочивается durable coordination, а не
   wall clock.
9. `/reset` продвигает durable generation ровно один раз и fences old work.
10. `AgentEmission` имеет отдельный durable delivery lifecycle
    `READY → DELIVERING → DELIVERED | FAILED | UNKNOWN | CANCELLED`;
    `UNKNOWN` не означает `FAILED` и не становится автоматически `READY`.
11. Final result/`OutputBatch READY` не являются terminal authority.
    Successful terminal visibility требует exact handoff/finalization/output
    evidence и `TERMINAL_COMMITTED`.
12. Startup recovery открывает ordinary runtime только после deterministic durable
    reconciliation, MCP connect и установки safe recovered ownership.
13. Contradictory immutable durable evidence приводит к controlled recovery
    failure, а не к semantic guessing.
14. IR-9 status/timeline только читают и производно представляют эти authorities.

## IR-9 implementation evidence

IR-9 code/test boundary:

`068f8f6682e7b7b805b60dbb640b53b671cc8565`

Exact GitHub Actions evidence для этой code boundary:

- `Validate Input Runtime` #685 — completed / success;
- production compile — success;
- focused IR-8 — `49 passed`, `0 failed`;
- focused IR-9 — `101 passed`, `0 failed`;
- full `tests/test_input_runtime_*.py + tests/test_artifact_configuration_examples.py`
  — `537 passed`, `0 failed`;
- `Validate v0.4 file artifacts PR` #803 — completed / success;
- workflow token permissions — `Contents: read`, `Metadata: read`.

Relevant workflow stdout не печатает skipped summary, поэтому skipped count не
заявляется.

## IR-9 diagnostics boundary

IR-9 реализует transport-neutral diagnostics/query layer:

```text
durable IR-1—IR-8 authority
→ coherent exact-session read
→ InputRuntimeDiagnosticsService
→ RuntimeStatusSnapshot / RuntimeTimeline
→ Telegram / Web / CLI renderer
```

Diagnostics являются только `READ / DERIVE`. Они не принимают admission,
control, finalization, output-delivery или recovery decisions.

Filesystem reader использует existing short exact-session coordination только
для bounded structured current-session/current-generation snapshot. После release
lock выполняются localization, Telegram presentation/network, HTTP serialization
и CLI rendering. LLM/tool/network awaits и unbounded content scan под runtime lock
не выполняются.

## Privacy и current-generation filtering

Generic status/timeline не возвращают raw:

- user text;
- LLM/system messages и prompts;
- tool arguments/results;
- file/artifact contents;
- tokens/API keys/callback auth;
- arbitrary response-route metadata;
- internal filesystem paths;
- raw traceback/exception dump.

Current queue/emission/finalization counts относятся только к authoritative
current generation. Old-generation records могут оставаться durable audit
evidence, но не смешиваются с current projection.

## RuntimeTimeline

Timeline является bounded deterministic projection существующих durable streams:

- default `limit = 20`;
- maximum `limit = 100`;
- per-stream authoritative sequence/identity сохраняется;
- equal timestamps используют stable deterministic display ordering;
- cross-stream order не становится runtime correctness authority;
- global semantic sequence не изобретается;
- новый durable event store/WebSocket/global event bus не добавляется.

## Addendum, controls, recovery и terminal projection

IR-9 client projection покрывает:

```text
input_addendum_admitted
input_addendum_applying
input_addendum_applied
input_addendum_cancelled
input_addendum_failed
```

`QUEUED_RUNNING`, `QUEUED_PAUSED` и `RESUME_WAITING` различаются. Queued input
никогда не представляется как already applied.

Control/recovery projection сохраняет различия:

```text
pause accepted != PAUSED_BY_USER
continue accepted != resumed != still_waiting_for_input
INTERRUPTED != AMBIGUOUS
UNKNOWN != FAILED
```

`SessionInputRuntimeState == DONE` сам по себе не является client terminal
authority: terminal projection требует matching IR-7/IR-8
finalization/handoff/output evidence.

## Telegram / Web / CLI

Production Telegram `/status` — high-priority read-only consumer общей
diagnostics DTO. Он не создаёт `CommittedInputBatch`, control sequence,
`AgentEmission`, не будит runner и не меняет generation/watermarks.

Telegram presentation lifecycle имеет отдельное fencing:

- deterministic not-editable outcome → bounded one-send fallback;
- ambiguous edit/send → no blind duplicate;
- session generation + presentation revision suppress stale queued/old-generation
  updates;
- existing progress version/terminal barrier не позволяет stale progress стать
  последним после terminal presentation.

Structured Web/API diagnostics:

- `GET /runtime/status`;
- `GET /runtime/timeline`.

Standalone production runtime CLI framework в проекте отсутствует. IR-9
предоставляет shared DTO-to-CLI renderer/consumer path без отдельной
repository-scanning business logic.

RU/EN projection keys синхронизированы existing localization layer. IR-9 не
добавляет новых configuration/environment fields; `.env.example` и
`src/api/mcp.config.example` не требуют искусственных IR-9 knobs, а existing
configuration-example audit входит в green `537 passed` regression.

## IR-10 — planned

IR-10 остаётся единственным незавершённым stage `v0.4-input-runtime`. Он владеет
release-final randomized/restart/synthetic/live acceptance.

Этот documentation restoration pass IR-10 не начинает и не является его
acceptance evidence.

## Deferred outside this update

Не реализованы этим update:

- Telegram edited-message history rewind;
- PostgreSQL / SQLAlchemy / Alembic;
- Redis/distributed workers/leases;
- distributed runtime;
- scheduler / `AgentRun` / `TaskRun`;
- parallel branches / fork-join;
- новый durable global event bus.
