---
id: design.v0.4.input-runtime.sequence
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-06
---

# Последовательность реализации v0.4-input-runtime

## Статус реализации

- `IR-1 — Domain models, config и repository ports`: implemented;
- `IR-2 — Filesystem repositories и coordination service`: implemented;
- `IR-3 — Admission service и initial-cycle integration`: implemented and hardened;
- `IR-4`—`IR-10`: planned.

Финальный подтверждённый IR-3 code boundary:

- основной admission implementation:
  `4929b703d7f6e200392661b2b66205b8fa4ca034`;
- crash-safe capacity и runner handoff hardening:
  `d11db7f2a2f8caae900f3bc94ed91de020059231`;
- cancellation-safe/storage-neutral handoff implementation:
  `e8192380cc3104668ea9b0f3f017d3c962fd65e4`;
- итоговый code HEAD после узкого test-fixture fix:
  `c36e4cc38095e15f54f63ae81c29b4829defec1f`;
- `Validate Input Runtime` #84 — success, `198 passed`;
- `Validate v0.4 file artifacts PR` #503 — success.

IR-3 production boundary теперь гарантирует:

```text
CommittedInputBatch
→ authoritative InputAdmission
→ один exact active cycle либо FIFO CycleInbox
→ pre-run setup
→ durable RuntimeHandoffRecord
→ process_query invocation
```

Running additions durable сохраняются в том же cycle без второго
`process_query()`. Count/byte capacity определяется authoritative admissions, а
не только наличием inbox file. Exact-cycle wake не позволяет позднему signal
старого cycle будить новый.

Runtime handoff storage-neutral: application service зависит от
`RuntimeHandoffRepository`, а concrete filesystem adapter создаётся только
`create_filesystem_input_runtime_repositories(...)`.

Cancellation contract:

- initial cancellation до marker оставляет admission retryable;
- initial cancellation после marker переводит marker в `AMBIGUOUS`, cycle — в
  `interrupted`, duplicate не запускает runtime повторно;
- WAITING cancellation после claim, но до marker, requeue-ит claim;
- WAITING cancellation после marker не requeue-ит claim, оставляет его evidence,
  переводит marker в `AMBIGUOUS`, cycle — в `interrupted`;
- cleanup запускается отдельной task, ожидается через `asyncio.shield`, завершается
  даже при повторной cancellation, затем исходный `CancelledError` re-raise-ится.

Общий update остаётся partial. IR-3 не применяет queued additions к LLM context и
не реализует startup recovery ambiguous handoff. Safe checkpoints, общий
`CycleInputApplier`, context revisions и active snapshot начинаются с IR-4.

Для следующих этапов зафиксированы обязательные contracts:

- IR-4: `WAITING_USER` reply при наличии более ранних queued additions применяется
  вместе с ними через общий FIFO `CycleInputApplier`;
- IR-7: pending accepted input подавляет stale `DONE`, question и output до
  terminal commit;
- IR-8: ambiguous marker и post-handoff claim evidence reconciles explicit
  recovery policy без blind replay внешних действий.

## Назначение документа

Документ является канонической последовательностью implementation patches. Каждый
этап фиксирует:

- цель и ownership boundary;
- изменяемые components;
- обязательные invariants и transitions;
- deterministic tests и acceptance;
- условие завершения;
- запрещённое расширение scope.

Реализация выполняется небольшими fast-forward patches. Большой rewrite
`src/mcp/mcp_client.py`, преждевременная modularization, PostgreSQL, Redis и
scheduler в этот update не входят.

## Предварительная инвентаризация

Перед каждым следующим этапом implementation agent повторно проверяет branch,
HEAD, status и diff относительно target branch, затем читает current owners:

```text
src/core/message_processor.py
src/api/api.py
src/api/session_reset.py
src/runtime/cycle.py
src/runtime/session_execution.py
src/mcp/mcp_client.py
src/mcp/waiting_user_batch_continuation.py
src/agent/protocol.py
src/interaction/*
src/servers/telegram/*
src/ingress/*
src/input_runtime/*
```

Обязательно определить:

- где committed batch проходит admission;
- где создаётся/resumes active cycle;
- точный LLM/tool loop и terminal return paths;
- где фиксируются `WAITING_USER`, `DONE`, interrupted/error;
- где выполняются final audit и output assembly;
- где хранятся memory/pending cycle;
- как `/reset` и shutdown взаимодействуют с execution lease;
- какие tests фиксируют FIFO, continuation, progress и delivery.

Результат инвентаризации — code comments/tests или PR evidence, а не новый
competing canonical design document.

---

# IR-1 — Domain models, config и repository ports

## Статус

Implemented и подтверждён CI. Production runtime integration не входит в IR-1.

## Цель

Создать независимый package `src/input_runtime/` с pure models, errors, config и
command-oriented repository Protocols.

## Основные файлы

```text
src/input_runtime/__init__.py
src/input_runtime/models.py
src/input_runtime/handoff.py
src/input_runtime/errors.py
src/input_runtime/config.py
src/input_runtime/interfaces.py
src/input_runtime/factory.py
```

## Models

Минимальный набор:

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

Stable ID factories и enums определяются один раз. Timestamp fields timezone-aware;
terminal runtime handoff timestamp не может предшествовать `handed_off_at`.

## Ports

```python
class SessionInputRuntimeRepository(Protocol): ...
class InputAdmissionRepository(Protocol): ...
class CycleInboxRepository(Protocol): ...
class RuntimeHandoffRepository(Protocol): ...
class SessionControlRepository(Protocol): ...
class ActiveCycleSnapshotRepository(Protocol): ...
class ContextRevisionRepository(Protocol): ...
class AgentEmissionRepository(Protocol): ...
class FinalizationRepository(Protocol): ...
```

`RuntimeHandoffRepository` имеет только command-oriented surface:

```python
async def get(...)
async def begin(...)
async def complete(...)
async def mark_ambiguous(...)
```

Generic `save(dict)`, filesystem paths, locks и serialization helpers в
application-facing ports запрещены.

## Config

```yaml
input_runtime:
  enabled: true
  max_queued_batches_per_session: ...
  max_queued_bytes_per_session: ...
  max_batches_per_checkpoint: ...
  max_batch_bytes_per_checkpoint: ...
  claim_lease_seconds: ...
  max_intermediate_messages_per_cycle: ...
  min_intermediate_message_interval_seconds: ...
  max_intermediate_message_chars: ...
```

Defaults conservative и valid для current single-process mode.

## Tests

- model/state/ID validation;
- invalid sequence/watermark rejected;
- config examples cover all settings;
- serialization round-trip;
- Protocols command-oriented;
- no Telegram/FastAPI/MCP concrete imports in domain/interfaces.

## Done

- package импортируется без side effects;
- ports пригодны для filesystem и PostgreSQL adapters;
- current production behavior не изменён;
- full existing suite green.

## Не делать

- не подключать stores к API;
- не добавлять checkpoints;
- не менять transport command handlers;
- не переносить agent loop.

---

# IR-2 — Filesystem repositories и coordination service

## Статус

Implemented и подтверждён CI. Реализованы durable filesystem adapters,
atomic-write/restart contracts, sequence repair, bounded coordination, global
identity fencing и crash-recoverable indexes.

## Цель

Реализовать local durable backend, atomic replacement, per-session short
coordination и startup-readable indexes поверх `StorageConfigType.root_dir`.

## Infrastructure files

```text
src/input_runtime/filesystem.py
src/input_runtime/_filesystem_*.py
src/input_runtime/coordination.py
src/input_runtime/serialization.py
src/input_runtime/factory.py
```

Filesystem implementation `RuntimeHandoffRepository` также принадлежит
infrastructure-модулю и подключается только composition factory.

## Storage/coordination invariants

- user-controlled IDs не используются raw path segments;
- one short in-process lock per normalized session;
- bounded/ref-counted lock registry cancellation-safe;
- fixed lock order `root identity → session`;
- no long LLM/tool/delivery await под coordination lock;
- deterministic session/cycle sequence allocation;
- compare-and-swap revisions;
- durable record write предшествует indexes;
- missing/dangling index запускает exact-identity recovery до competing create;
- один authoritative record rebuilds relations;
- отсутствие durable record очищает dangling reservation;
- ambiguous durable identity возвращает managed consistency error.

## Claim leases

```text
claim contiguous head range
validate generation/token
mark applying
mark applied
requeue retryable/expired claim
cancel generation
reconcile by authoritative watermark in later stages
```

## Tests

- concurrent sequence allocation;
- duplicate admission creation;
- claim conflict/expiry;
- stale CAS revision;
- partial atomic-write/index failures;
- dangling pointer без durable record;
- lost identity/cycle-authority indexes;
- ambiguous duplicate records;
- path traversal rejection;
- recreation two repository bundles over same root;
- filesystem handoff marker survives recreation;
- stale handoff token cannot complete another attempt.

## Done

- repositories survive process recreation;
- ordered list operations deterministic;
- no duplicate identities/sequences under race;
- partial metadata writes recover safely;
- application service does not import filesystem adapter details.

## Не делать

- не emulate distributed transaction длинным global lock;
- не добавлять Redis/PostgreSQL;
- не выполнять whole-repository scan на каждом hot path.

---

# IR-3 — Admission service и initial-cycle integration

## Статус

Implemented, hardened и подтверждён CI на итоговом code HEAD
`c36e4cc38095e15f54f63ae81c29b4829defec1f`:

- `Validate Input Runtime` #84 — success, `198 passed`;
- `Validate v0.4 file artifacts PR` #503 — success;
- production composition создаёт filesystem repository bundle и injects готовый
  `RuntimeHandoffRepository` в `InputAdmissionService`;
- каждый `CommittedInputBatch` проходит `admit → start/resume/acknowledge`;
- second batch during running получает durable admission/FIFO inbox того же cycle
  без второго `process_query()`;
- duplicate, count/byte capacity, crash windows, runner handoff, cancellation и
  exact-cycle wake покрыты deterministic tests;
- `WAITING_USER` остаётся временным same-cycle compatibility adapter;
- `interrupted` и `AMBIGUOUS` не запускают automatic replay.

## Цель

Every committed batch проходит transport-neutral `InputAdmissionService`. New
input во время active cycle не создаёт second run operation.

## Основные файлы

```text
src/input_runtime/admission.py
src/input_runtime/service.py
src/input_runtime/hardened_service.py
src/input_runtime/handoff.py
src/input_runtime/interfaces.py
src/input_runtime/factory.py
src/input_runtime/_filesystem_handoff.py
src/api/api.py
src/core/message_processor.py
src/runtime/session_execution.py
```

## Composition boundary

`Api.__init__` создаёт filesystem adapters через factory и injects:

- committed batch reader;
- `InputRuntimeRepositories`, включая `handoffs` port;
- execution coordinator/wakeup port;
- config и deterministic identity/time policies.

`InputAdmissionService` не импортирует `Path`, `SessionLockRegistry`, concrete
filesystem handoff store или serialization helpers.

## Admission API

```python
async def admit_committed_batch(
    input_batch_id: str,
    *,
    session_id: str,
) -> InputAdmissionOutcome
```

Call flow:

```text
submit/commit
→ admit
→ start_cycle: reserve exact runner
→ queued_running: durable enqueue + wake intent
→ resume_waiting: compatibility claim + same-cycle runner
→ duplicate: return existing relation
→ capacity_blocked: retryable response, committed input retained
```

## Initial cycle

1. allocate service-owned cycle ID and sequence `0`;
2. persist admission/session state;
3. acquire exact admitted execution lease;
4. resolve authoritative batch and capabilities;
5. persist runtime handoff marker;
6. invoke current agent runtime with exact batch/cycle identity;
7. persist applied/status/output compatibility steps;
8. complete handoff marker.

Initial context revision и active snapshot ownership intentionally deferred to IR-4.

## Running addition и capacity authority

- addition получает `cycle_sequence > 0`;
- persist admission, accepted watermark и inbox relation;
- signal exact active runner;
- return acknowledgement without awaiting final result.

Capacity reservation определяется authoritative admissions текущего generation и
cycle, для которых:

```text
cycle_sequence > 0
cycle_sequence > active_cycle_applied_through_sequence
state == admitted/pending
payload_size_bytes contributes to byte limit
```

Initial sequence `0` и terminal applied/cancelled/failed records capacity не
занимают. Missing inbox после crash не освобождает reservation. Retry exact batch
восстанавливает ровно один inbox relation с исходной sequence.

## Runtime handoff contract

```text
pre-run resolution
→ RuntimeHandoffRepository.begin(HANDED_OFF)
→ process_query()
→ complete(COMPLETED) or mark_ambiguous(AMBIGUOUS)
```

- failure до marker retryable;
- после marker duplicate не вызывает runtime повторно;
- exception/crash/cancellation после marker становится ambiguous/interrupted;
- successful runtime result + subsequent persistence failure также не rerun-ится;
- stale token не завершает marker другой attempt.

## Cancellation contract

Оба paths имеют отдельный:

```python
except asyncio.CancelledError:
    ...
    raise
```

Durable cleanup:

```text
create cleanup task
→ await through asyncio.shield
→ if repeated cancellation: continue waiting cleanup task
→ inspect cleanup result/log failure
→ re-raise original CancelledError
```

Initial:

- cancellation до marker: marker отсутствует, admission retryable;
- cancellation после marker, включая окно до фактического invocation: marker
  `AMBIGUOUS`, cycle `interrupted`, duplicate no-rerun.

WAITING compatibility:

- cancellation после claim, до marker: requeue claim;
- cancellation после marker: не requeue claim, сохранить claim evidence,
  `AMBIGUOUS` + `interrupted`, duplicate no-rerun.

## SessionExecutionCoordinator

Coordinator остаётся in-process execution lease/wakeup foundation, но не durable
queue. `wake(session_id, cycle_id=...)` выставляет event только при exact match
reserved/active cycle; mismatch возвращает `False` и event не меняет.

## Tests

```text
tests/test_input_runtime_admission.py
tests/test_input_runtime_api_admission.py
tests/test_input_runtime_no_parallel_cycle.py
tests/test_input_runtime_ir3_contract_gaps.py
tests/test_input_runtime_ir3_cancellation_and_portability.py
```

Mandatory scenarios:

- idle batch starts one cycle;
- running additions never create parallel runner;
- missing inbox admission reserves count and byte capacity;
- retry missing inbox creates exactly one relation;
- pre-handoff resolution failure/cancellation retryable;
- cancellation during `process_query()` ambiguous/interrupted;
- cancellation after marker before invocation ambiguous/interrupted;
- repeated cancellation cannot interrupt durable cleanup;
- WAITING pre-marker cancellation requeues claim;
- WAITING post-marker cancellation preserves applying claim evidence;
- every post-handoff duplicate no-rerun;
- durable session status after post-handoff cancellation is interrupted;
- in-memory fake handoff port works without root/locks;
- filesystem bundle provides `handoffs` and reads marker after recreation;
- stale token rejected;
- terminal timestamp ordering validated;
- late wake old cycle does not wake new cycle.

## Done

- every production committed batch проходит admission;
- ordinary initial request remains compatible;
- one active cycle enforced durably and in-process;
- additions no longer wait as separate conflicting cycles;
- handoff cancellation-safe and storage-neutral;
- no blind replay after ambiguous runtime boundary.

## Не делать

- не применять additions к LLM context;
- не добавлять safe checkpoints/snapshots;
- не реализовывать startup recovery policy;
- не удалять WAITING compatibility before common applier;
- не начинать IR-4.

---

# IR-4 — Active snapshot, checkpoints и CycleInputApplier

## Статус

Planned. Не реализован в IR-3 hardening.

## Цель

Apply admitted additions к тому же active cycle только в protocol-safe
checkpoints.

## Основные файлы

```text
src/input_runtime/checkpoints.py
src/input_runtime/applier.py
src/input_runtime/context_revisions.py
src/runtime/cycle.py
src/mcp/mcp_client.py
src/mcp/waiting_user_batch_continuation.py
```

## Active snapshot

```text
generation
applied_input_batch_ids
applied_through_cycle_sequence
active_context_revision_id
safe_checkpoint
pause/interruption metadata
```

## Hooks

```text
after create/resume
before main LLM request
after complete tool block
before WAITING_USER
before final processing
before terminal return
```

No filesystem logic inside `mcp_client.py`.

## Apply protocol

- claim contiguous FIFO range;
- load exact committed batches;
- validate generation/order;
- build one `input_batch_update`;
- activate artifact refs;
- append protocol-valid user message;
- persist context revision + active snapshot + watermark;
- mark inbox/admissions applied;
- emit lifecycle/projection events.

Crash between snapshot persistence and item marking reconciles by snapshot
watermark without duplicate append.

## Mandatory WAITING contract

Если перед `WAITING_USER` reply существуют более ранние queued additions, reply
не может обойти их через compatibility path. Общий `CycleInputApplier` применяет
contiguous range строго в cycle-sequence order.

## Tests

- addition during LLM;
- addition during tool block;
- addition immediately before WAITING_USER;
- two additions between checkpoints;
- snapshot persisted / mark applied fails;
- expired applying claim reconciliation;
- ambiguous IR-3 claim reconciliation without blind runtime replay;
- compaction before/after update;
- plan/artifact refs preserved.

## Done

- same active cycle consumes new input;
- protocol sequence valid;
- stale WAITING suppressed;
- additions exactly-once/FIFO;
- compatibility mixin loses semantic ownership.

## Не делать

- не classify additions semantically;
- не create parallel task/branch;
- не reset plan automatically.

---

# IR-5 — Durable control plane `/stop`, `/continue`, `/reset`

## Цель

Добавить pause/resume без state loss и перевести reset на общий durable
generation contract.

## Поведение

- `/stop` persists pause command и `pause_requested`;
- checkpoint завершает текущий atomic block и сохраняет `paused_by_user`;
- ordinary input during pause queues without auto-resume;
- `/continue` resumes same cycle и drains additions;
- `/reset` advances generation и invalidates stale work/finalization/delivery.

## Tests

- stop during LLM/tool block;
- additions during pause;
- continue after several batches;
- duplicate controls;
- reset racing finalization;
- existing collection commands unchanged.

## Не делать

- не implement conversation rewind;
- не reinterpret `/cancel` as runtime stop;
- не promise force cancellation confirmed external side effects.

---

# IR-6 — AgentEmission и intermediate messages

## Цель

Durable semantic intermediate messages independent from transient progress и
terminal output.

## Контракт

`send_user_message(...)` validates kind/length/rate, persists emission before tool
success and uses trusted runtime-owned route. Delivery state independent from
cycle execution result.

## Tests

- persistence before tool success;
- duplicate idempotency;
- delivery failure independent from cycle;
- no emission after terminal commit;
- progress remains transient;
- safe optional reply relation.

## Не делать

- не convert all progress to emissions;
- не build distributed event bus;
- не expose arbitrary route to LLM.

---

# IR-7 — Finalization barrier

## Цель

Не допустить stale final/waiting response, игнорирующий accepted input/control.

## Mandatory contract

Pending accepted input подавляет stale `DONE`, question и output до terminal
commit, даже если inbox/application ещё не завершились.

## Protocol

```text
candidate remains non-terminal
→ pre-final checkpoint
→ prepare finalization
→ short watermark recheck
→ persist result/output
→ second terminal recheck
→ terminal commit enables delivery
```

## Tests

Inject input/control:

- before/during final processing;
- after result persistence;
- after output ready;
- immediately before terminal commit.

## Done

- all pre-terminal events abort stale finalization;
- post-terminal input creates new cycle;
- output not claimable before terminal commit;
- restart does not duplicate cycle/output.

---

# IR-8 — Startup recovery и lifecycle

## Цель

Recover durable runtime before accepting new work and shutdown without orphan
claims/runners.

## Startup

- reconcile sessions/admissions/inbox;
- repair committed-but-unadmitted inputs;
- inspect runtime handoff markers;
- keep ambiguous external operation non-replayable;
- reconcile post-handoff claim evidence by explicit policy;
- restore resumable/paused/waiting runners only when safe;
- enable new admission after mandatory recovery.

IR-3 marker не является готовой startup recovery implementation.

## Shutdown

- stop new runner starts;
- persist resumable/interrupted state where possible;
- cancel in-process wakeups;
- retain durable admissions/inbox/claims;
- leave ambiguous external operations unknown;
- close runtime after agent/tool lifecycle.

## Tests

Two service instances over same temporary root:

- pause/wait/inbox survive recreation;
- committed unadmitted repaired;
- ambiguous marker does not rerun runtime;
- post-handoff claim evidence preserved/reconciled;
- no new work before recovery ready;
- shutdown deterministic.

---

# IR-9 — Client projections, diagnostics и configuration examples

## Цель

Expose coherent safe status without raw-content leakage and preserve client
independence.

## Diagnostics

```text
runtime status/generation
active cycle
accepted/applied sequences
queued/claimed/applying additions
runtime handoff state
oldest queued age
pending/applied controls
emission/finalization states
last error code
```

## Projections

- initial request status;
- running/paused addition acknowledgement;
- applied addition completion;
- stop/continue outcome;
- interrupted/ambiguous recovery notice.

## Done

- RU/EN keys complete;
- Telegram editing fallback safe;
- Web/CLI consume structured outcomes;
- config examples/documentation synchronized.

---

# IR-10 — Full acceptance, roast и live validation

## Цель

Prove contracts под unit, race, restart, synthetic и real transport behavior.

## Automated suites

```text
input-runtime focused suite
artifact/storage/plans/planning/API regressions
Telegram transport/audit suite
full repository baseline
compile/config example audit
```

## Race/randomized matrix

- concurrent commit/admission;
- addition at every checkpoint/finalization boundary;
- stop/continue/reset ordering;
- claim expiry/restart;
- lost wakeup;
- output ready vs new input;
- duplicate requests;
- additions with files;
- shutdown while queued/claimed/applying;
- cancellation before/after handoff and during cleanup.

Every randomized run records seed and selector.

## Synthetic no-network roast

Must not call real LLM/MCP/network/Telegram. Use deterministic fake runtime and
transport sinks.

## Maintainer live Telegram acceptance

1. long run + text addition;
2. several additions/files during tools;
3. `/stop` during visible work;
4. additions while paused;
5. `/continue` and same-cycle apply;
6. intermediate message while work continues;
7. addition immediately before final;
8. `/reset` while active;
9. restart while paused/waiting/queued;
10. regression collection/artifact delivery commands.

## Completion gate

- zero deterministic failures;
- no unexplained flaky race;
- full baseline green;
- no production bypass of admission;
- no canonical documentation conflicts;
- current/roadmap/PR evidence synchronized;
- PR remains draft until all IR-1—IR-10 and live acceptance complete.

---

# Допустимая параллельность patches

После IR-1 независимые filesystem adapters, emission policy drafts,
localization и synthetic harness могут разрабатываться параллельно.

Admission, checkpoint integration, controls и finalization выполняются
последовательно, поскольку разделяют session state/watermarks.

IR-6 может интегрироваться после stable cycle/context identity и до завершения
finalization barrier.

---

# Рекомендуемые commit boundaries

```text
feat(input-runtime): add domain contracts and config
feat(input-runtime): add filesystem repositories and claims
feat(input-runtime): route committed batches through admission
fix(input-runtime): close IR-3 capacity and runner handoff gaps
fix(input-runtime): make IR-3 handoff cancellation-safe and portable
feat(input-runtime): apply additions at safe checkpoints
feat(input-runtime): add durable stop and continue controls
feat(input-runtime): add intermediate agent emissions
feat(input-runtime): guard waiting and finalization races
feat(input-runtime): recover durable active runtime state
test(input-runtime): add race restart and transport coverage
docs(input-runtime): finalize acceptance and current baseline
```

Один commit не должен одновременно вводить новый state contract, переписывать
agent loop и менять transport presentation без characterization tests.
