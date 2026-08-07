---
id: design.v0.4.input-runtime.sequence
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-07
---

# Последовательность реализации v0.4-input-runtime

## Статус реализации

- `IR-1 — Domain models, config и repository ports`: implemented;
- `IR-2 — Filesystem repositories и coordination service`: implemented;
- `IR-3 — Admission service и initial-cycle integration`: implemented and hardened;
- `IR-4 — Active snapshot, checkpoints и CycleInputApplier`: implemented;
- `IR-5 — Durable control plane /stop, /continue, /reset`: implemented and validated;
- `IR-6`—`IR-10`: planned.

Общий `v0.4-input-runtime` остаётся `partial`.

Финальный подтверждённый IR-5 code/test boundary:

- итоговый code/test HEAD:
  `85c52d4b60a60786bdb10732eb0a52893a422eee`;
- `Validate Input Runtime` #173 — success, compile success, `278 passed`,
  `0 failed`;
- `Validate v0.4 file artifacts PR` #547 — success.

IR-5 production boundary теперь гарантирует:

```text
transport command
→ transport-neutral InputRuntimeControlService
→ stable durable SessionControlCommand + monotonic sequence
→ pending_control_sequence
→ safe checkpoint control reducer
→ pause/continue/reset semantic effect
→ contiguous applied_control_sequence
```

`/stop` cooperative: bounded LLM attempt/complete assistant tool block
завершается protocol-valid, затем snapshot фиксируется `paused_by_user` и
следующий semantic block не начинается. `pause_requested`/`paused_by_user` input
admitted как `QUEUE_PAUSED` без wake/auto-resume.

`/continue` возобновляет тот же `cycle_id`; pre-continue paused additions
фиксируются target watermark и drain-ятся bounded chunks через `CP-RESUME` до
первого post-resume LLM. Continue без additions не создаёт fake input/revision;
`WAITING_USER` без ответа возвращает `still_waiting_for_input`.

`/reset` использует durable `SessionInputRuntimeState.generation` как authority,
повышает её ровно один раз на logical command, cancels/fences old-generation
records и синхронизирует defensive in-process coordinator только после durable
transition. Mutable session memory очищается после safe execution lease boundary.

Checkpoint-level pause/reset suppression перед terminal transition реализовано,
но late terminal race после последнего checkpoint recheck остаётся IR-7.
Startup-wide runner reconstruction/reconciliation остаётся IR-8. Полная corruption
matrix `recover_cycle_authority()` остаётся IR-8/IR-10.

Финальный подтверждённый IR-4 code/test boundary:

- итоговый code/test HEAD:
  `1d31b6fbd1d5e88966d3964dc35cf4680f32f522`;
- `Validate Input Runtime` #115 — success, compile success, `241 passed`,
  `0 failed`;
- `Validate v0.4 file artifacts PR` #518 — success, validation suites и status
  enforcement success;
- regression-fix проход после `224911a…` затронул только tests; production code
  не менялся.

IR-4 production boundary гарантирует:

```text
admitted active cycle
→ CP-RESUME
→ initial R1 + durable ActiveCycleSnapshot
→ protocol-safe checkpoint entry watermark
→ bounded contiguous FIFO apply
→ one input_batch_update per applied range
→ next linear CycleContextRevision
→ snapshot-first applied watermark
→ inbox/admission marking reconciliation
```

WAITING reply проходит тот же common FIFO `CP-RESUME`, не обходит более ранние
queued additions и не владеет legacy semantic continuation path. Claim acquisition
и apply cancellation-safe. Runtime handoff completion предшествует terminal
snapshot synchronization.

Финальный подтверждённый IR-3 code boundary сохраняется как предыдущий stage
evidence:

- основной admission implementation:
  `4929b703d7f6e200392661b2b66205b8fa4ca034`;
- crash-safe capacity и runner handoff hardening:
  `d11db7f2a2f8caae900f3bc94ed91de020059231`;
- cancellation-safe/storage-neutral handoff implementation:
  `e8192380cc3104668ea9b0f3f017d3c962fd65e4`;
- итоговый IR-3 code HEAD после узкого test-fixture fix:
  `c36e4cc38095e15f54f63ae81c29b4829defec1f`;
- `Validate Input Runtime` #84 — success, `198 passed`;
- `Validate v0.4 file artifacts PR` #503 — success.

IR-3 production boundary гарантирует:

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

Cancellation contract IR-3:

- initial cancellation до marker оставляет admission retryable;
- initial cancellation после marker переводит marker в `AMBIGUOUS`, cycle — в
  `interrupted`, duplicate не запускает runtime повторно;
- WAITING cancellation после claim, но до marker, requeue-ит claim;
- WAITING cancellation после marker не requeue-ит claim, оставляет его evidence,
  переводит marker в `AMBIGUOUS`, cycle — в `interrupted`;
- cleanup запускается отдельной task, ожидается через `asyncio.shield`, завершается
  даже при повторной cancellation, затем исходный `CancelledError` re-raise-ится.

После IR-5 следующие mandatory contracts остаются:

- IR-6: durable semantic `AgentEmission`;
- IR-7: durable finalization barrier, закрывающий late terminal race;
- IR-8: ambiguous/startup recovery policy без blind replay;
- IR-9: client projections/diagnostics/config examples;
- IR-10: full race/restart/synthetic/live acceptance, включая corruption matrix.

Scheduler/parallel branches и Telegram history rewind в текущий implemented
baseline не входят.

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

IR-4 реализовал snapshot-watermark reconciliation для applied active-cycle
ranges. Startup-wide ambiguous/corruption reconciliation остаётся IR-8/IR-10.

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

Implemented, hardened и подтверждён CI на итоговом IR-3 code HEAD
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
- IR-3 сохранял временный same-cycle WAITING compatibility adapter до IR-4;
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

Call flow IR-3:

```text
submit/commit
→ admit
→ start_cycle: reserve exact runner
→ queued_running: durable enqueue + wake intent
→ resume_waiting: compatibility claim + same-cycle runner
→ duplicate: return existing relation
→ capacity_blocked: retryable response, committed input retained
```

На IR-4 WAITING semantic continuation переведён на common FIFO checkpoint path;
историческое `resume_waiting` описание выше фиксирует именно IR-3 boundary.

## Initial cycle

1. allocate service-owned cycle ID and sequence `0`;
2. persist admission/session state;
3. acquire exact admitted execution lease;
4. resolve authoritative batch and capabilities;
5. persist runtime handoff marker;
6. invoke current agent runtime with exact batch/cycle identity;
7. persist applied/status/output compatibility steps;
8. complete handoff marker.

Initial context revision и active snapshot ownership были intentionally deferred
из IR-3 в IR-4; теперь IR-4 реализует initial `R1` + durable snapshot до first main
LLM/result.

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

IR-4 сохраняет этот ownership: successful handoff completion выполняется раньше
terminal snapshot synchronization.

## Cancellation contract

Оба IR-3 paths имеют отдельный:

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

WAITING compatibility на IR-3:

- cancellation после claim, до marker: requeue claim;
- cancellation после marker: не requeue claim, сохранить claim evidence,
  `AMBIGUOUS` + `interrupted`, duplicate no-rerun.

IR-4 дополнительно делает cancellation-safe common claim acquisition/apply на
checkpoint path.

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

Mandatory scenarios IR-3:

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

## Не делать на этапе IR-3

- не применять additions к LLM context;
- не добавлять safe checkpoints/snapshots;
- не реализовывать startup recovery policy;
- не удалять WAITING compatibility before common applier;
- не начинать IR-4 внутри IR-3 patch.

Эти пункты фиксируют историческую stage boundary; IR-4 теперь реализован отдельно.

---

# IR-4 — Active snapshot, checkpoints и CycleInputApplier

## Статус

Implemented и закрыт по code gate на
`1d31b6fbd1d5e88966d3964dc35cf4680f32f522`:

- `Validate Input Runtime` #115 — success, compile success, `241 passed`,
  `0 failed`;
- `Validate v0.4 file artifacts PR` #518 — success;
- regression fixes после `224911a…` — test-only, production code unchanged.

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

Фактическая implementation может быть разделена дополнительными IR-4 modules,
но ownership остаётся `src/input_runtime` + thin agent-loop hooks; filesystem logic
в `mcp_client.py` не переносится.

## Active snapshot

```text
generation
applied_input_batch_ids
applied_through_cycle_sequence
active_context_revision_id
safe_checkpoint
pause/interruption metadata
```

Initial `CP-RESUME` создаёт `R1` и durable `ActiveCycleSnapshot` до первого main
LLM/result. Snapshot сохраняет semantic context authority, applied IDs/watermark,
runtime refs и safe checkpoint.

## Hooks

```text
after create/resume
before main LLM request
after complete tool block
before WAITING_USER
before final processing
before terminal return
after controlled interruption
```

No filesystem logic inside `mcp_client.py`.

## Protocol-safe checkpoint matrix

IR-4 использует общую checkpoint service boundary:

```text
CP-RESUME
CP-BEFORE-LLM
CP-AFTER-TOOL-BLOCK
CP-BEFORE-WAITING
CP-BEFORE-FINAL-PROCESSING
CP-BEFORE-TERMINAL-COMMIT
CP-AFTER-INTERRUPTION
```

Checkpoint не вставляет user update между `assistant.tool_calls` и matching
`role=tool` results и не меняет context revision уже начатого atomic block.

## Accepted-at-entry watermark

На входе checkpoint фиксируется `active_cycle_accepted_through_sequence`.
Checkpoint может выполнить несколько bounded contiguous apply ranges, чтобы
догнать именно этот target, но input, admitted позже, не расширяет текущий drain.

```text
entry accepted = N
apply from current watermark through N
late admission N+1
→ N+1 waits for next safe checkpoint
```

Это делает next LLM/tool block привязанным к deterministic context revision.

## Apply protocol

Accepted IR-4 protocol сохраняется:

- claim contiguous FIFO range;
- load exact committed batches;
- validate generation/order;
- build one `input_batch_update`;
- activate artifact refs;
- append protocol-valid user message;
- persist context revision + active snapshot + watermark;
- mark inbox/admissions applied;
- emit lifecycle/projection events.

Реализованная semantic ownership уточняет:

- каждый bounded applied range создаёт один `input_batch_update` и одну следующую
  linear context revision;
- несколько contiguous batches внутри range сохраняют batch boundaries и ordered
  `cycle_sequence`;
- durable `AgentEmission` не считается реализованным этим пунктом и остаётся IR-6;
- checkpoint outcomes/runtime traces не подменяют IR-6 semantic emissions.

## Linear context revisions

```text
initial batch → R1
range A → R2(parent=R1)
range B → R3(parent=R2)
```

No-op checkpoint не создаёт новую revision. Multiple-parent identity остаётся
future-compatible, но scheduler/parallel branches/merge semantics не реализованы.

## Snapshot-first crash reconciliation

Порядок authority:

```text
append next context revision
→ persist ActiveCycleSnapshot with new watermark
→ mark inbox/admission applied
```

Если crash/failure происходит после snapshot persistence и до marking, snapshot
watermark является authority. Следующий checkpoint/reconciliation домаркировывает
inbox/admission без duplicate `input_batch_update`, без второй revision и без
повторного semantic apply. Retry после repair — no-op для уже applied range.

## Cancellation-safe claim/apply

Claim acquisition и apply обрабатывают cancellation так, чтобы durable state
оставался recoverable:

- до persisted snapshot claim может быть безопасно requeued/reconciled;
- после persisted snapshot watermark marking завершается из snapshot authority;
- cancellation не создаёт duplicate update/revision;
- repeated cleanup/cancellation не ослабляет existing IR-3 no-blind-replay
  contract.

## Mandatory WAITING contract

Если перед `WAITING_USER` reply существуют более ранние queued additions, reply
не может обойти их через compatibility path. Общий `CycleInputApplier` применяет
contiguous range строго в cycle-sequence order.

На реализованном IR-4 WAITING reply проходит common FIFO `CP-RESUME`. Legacy
adapter больше не владеет semantic continuation и не подменяет
`original_input_batch_id`; initial batch identity сохраняется.

## Terminal snapshot ordering

Successful runtime handoff завершается до terminal snapshot synchronization:

```text
RuntimeHandoffRecord.complete
→ terminal ActiveCycleSnapshot sync
```

IR-4 не ослабляет handoff authority ради terminal persistence.

## Checkpoint-level stale candidate suppression

`CP-BEFORE-WAITING`, `CP-BEFORE-FINAL-PROCESSING` и
`CP-BEFORE-TERMINAL-COMMIT` видят accepted-at-entry watermark. Если accepted input
опережает applied context, stale candidate подавляется, input применяется и cycle
продолжается.

Это **не** durable IR-7 finalization barrier. Late input после последнего
checkpoint observation и до terminal commit остаётся IR-7 race.

## Tests

Реализованный IR-4 deterministic coverage включает:

- addition during LLM;
- addition during tool block;
- addition immediately before WAITING_USER;
- two/multiple additions between checkpoints;
- initial `R1` + durable snapshot lifecycle;
- accepted-at-entry watermark;
- bounded contiguous FIFO application;
- exactly one update/revision per applied range;
- snapshot persisted / mark applied fails;
- snapshot watermark marking repair + retry no-op;
- expired/aborted claim handling на безопасной IR-4 boundary;
- cancellation during claim acquisition/apply;
- WAITING reply common FIFO `CP-RESUME`;
- compaction before/after update;
- plan/artifact refs preserved;
- protocol-valid tool sequence;
- handoff completion before terminal snapshot sync.

Cross-stage scenarios, которые accepted specification сохраняет, но IR-4 не
объявляет закрытыми:

- ambiguous IR-3 handoff/startup claim reconciliation — IR-8;
- late terminal race/durable output barrier — IR-7;
- `recover_cycle_authority()` corruption/restart matrix — IR-8/IR-10.

## Done

- same active cycle consumes new input;
- protocol sequence valid;
- initial `R1` и durable active snapshot authoritative;
- stale WAITING/final candidate suppressed на checkpoint-level;
- additions exactly-once/FIFO в пределах snapshot-first apply contract;
- accepted-at-entry watermark deterministic;
- compatibility adapter loses semantic ownership;
- handoff completion precedes terminal snapshot;
- cancellation-safe claim/apply подтверждён tests.

## Не делать

- не classify additions semantically;
- не create parallel task/branch;
- не reset plan automatically;
- не реализовывать IR-5 controls в IR-4;
- не считать checkpoint suppression IR-7 finalization barrier;
- не притягивать IR-8 startup/ambiguous recovery в IR-4.

---

# IR-5 — Durable control plane `/stop`, `/continue`, `/reset`

## Статус

Implemented and validated на code/test HEAD
`85c52d4b60a60786bdb10732eb0a52893a422eee`:

- `Validate Input Runtime` #173 — success, compile success, `278 passed`,
  `0 failed`;
- `Validate v0.4 file artifacts PR` #547 — success.

## Цель

Добавить pause/resume без state loss и перевести reset на общий durable
generation/control contract без превращения in-process coordinator в authority.

## Реализованный ownership

Основные production boundaries:

```text
src/input_runtime/ir5_controls.py
src/input_runtime/ir5_hardening.py
src/input_runtime/ir5_checkpoints.py
src/input_runtime/ir5_filesystem_controls.py
src/input_runtime/_filesystem_identity_recovery_session.py
src/runtime/session_execution.py
src/mcp/input_runtime_controls.py
src/api/input_runtime_controls.py
src/api/session_reset.py
src/core/message_processor.py
src/servers/telegram/runtime_control_handlers.py
```

Application-layer service не импортирует `Path`, filesystem layout,
`SessionLockRegistry`, Telegram/aiogram/python-telegram-bot types. Filesystem
coordination остаётся infrastructure adapter.

## Durable acceptance

- one monotonic `sequence_number` allocated under short session coordination;
- stable idempotency binds one source delivery to one logical command;
- publication order record-first: command record → identity/index → session
  `pending_control_sequence`;
- retry same key repairs missing pending watermark without new ID/sequence;
- rejected/cancelled/applied head records allow contiguous
  `applied_control_sequence` advancement;
- no LLM/tool/network/Telegram await under repository coordination lock.

## `/stop`

```text
running
→ durable pause accepted
→ pause_requested
→ current bounded atomic block completes
→ protocol-safe control checkpoint
→ paused_by_user snapshot
```

- no history/plan/artifact/result/context reset;
- stop during blocked LLM waits that bounded attempt then prevents next semantic
  tool/LLM block;
- stop inside assistant multi-tool block waits all matching `role=tool` results
  and pauses at `CP-AFTER-TOOL-BLOCK`;
- waiting/interrupted resumable snapshot can be paused without losing question or
  interruption metadata;
- terminal/idle returns `no_active_cycle`;
- compatibility AgentResult mapping cannot overwrite durable pause.

## Paused admission

`pause_requested` and `paused_by_user` ordinary committed batches use existing
IR-3 admission/FIFO stores and return `QUEUE_PAUSED`. Accepted watermark advances,
items remain FIFO queued, `should_start_runner=false`, `should_wake_runner=false`.
Input does not behave as `/continue`.

## `/continue`

- resumes the same durable `cycle_id` only;
- true pause can reacquire defensive in-process execution lease for that same
  cycle after previous runner unwinds;
- `CP-RESUME` drains every addition accepted before continue acceptance target in
  bounded chunks, with no LLM between chunks;
- input admitted after continue boundary remains future running checkpoint input;
- no-addition continue preserves original batch/context revision;
- `WAITING_USER` without answer returns `still_waiting_for_input`;
- rapid `pause → continue` before pause application can reduce to running without
  phantom paused state or second runner.

Process-restart runner reconstruction остаётся IR-8.

## `/reset`

- reset has highest effective priority;
- durable generation is authority and advances exactly once per logical reset;
- same-key duplicate does not advance generation twice;
- partial old-generation cleanup failure is retried against same reset record;
- old admissions/inbox/pending controls/snapshot/dormant finalization/emission
  records are cancelled/fenced;
- stale old-generation checkpoint writer cannot regain current session authority;
- stale generation/cycle wake cannot wake new work;
- coordinator synchronizes to already-durable generation;
- open ingress drafts/collections are cancelled;
- mutable MCP/session memory clears only after old execution lease boundary.

## Checkpoint reducer

Checkpoint entry captures both accepted-input and pending-control watermarks.
Completed atomic block context is persisted first, controls are reduced through
captured control target, and only then ordinary input may apply. Control arriving
after entry cannot mutate an already-started atomic block.

Effective priority:

```text
reset > pause > continue > ordinary input
```

Audit order/records remain durable.

## Deterministic tests

IR-5 suites:

```text
tests/test_input_runtime_ir5_controls.py
tests/test_input_runtime_ir5_races.py
tests/test_input_runtime_ir5_telegram.py
tests/test_input_runtime_ir5_state_matrix.py
```

Covered barriers/contracts:

- concurrent control allocation and duplicate delivery;
- command persisted / pending watermark write fault and retry repair;
- session/snapshot control effect persisted / acknowledgement or apply marking
  fault and retry;
- blocked fake LLM stop;
- barrier inside complete multi-tool assistant block;
- rapid stop/continue while LLM blocked;
- paused input/no wake/FIFO;
- continue same cycle with no additions and several bounded additions;
- late post-continue input remains queued;
- waiting/interrupted state matrix;
- reset running/paused/waiting/interrupted;
- generation exactly once and partial cleanup repair;
- stale writer/wake fencing;
- pause/reset vs terminal checkpoint;
- compatibility result mapping;
- Telegram high-priority routing and stable source identity.

## Done

- durable transport-neutral control service;
- race-safe sequence/idempotency and real control watermarks;
- cooperative safe-checkpoint stop with complete tool protocol;
- durable `paused_by_user` snapshot;
- paused FIFO input without auto-resume;
- same-cycle continue and pre-continue drain target;
- durable reset generation authority and old-generation fencing;
- legacy reset observable memory/draft behavior preserved;
- Telegram `/stop`/`/continue` use common service; `/cancel` remains ingress-only;
- focused full input-runtime regression and compile gate green.

## Не делать

- не implement conversation rewind;
- не reinterpret `/cancel` as runtime stop;
- не promise force cancellation confirmed external side effects;
- не implement IR-6 durable emission lifecycle;
- не close IR-7 late terminal window;
- не implement IR-8 startup reconstruction/reconciliation;
- не claim IR-9/IR-10 completion.

---

# IR-6 — AgentEmission и intermediate messages

## Статус

Planned. Durable semantic emissions не реализованы IR-5.

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

## Статус

Planned. IR-4/IR-5 реализуют только checkpoint-level stale input/control candidate
suppression. Durable finalization barrier и late terminal race closure отсутствуют.

## Цель

Не допустить stale final/waiting response, игнорирующий accepted input/control.

## Mandatory contract

Pending accepted input/control подавляет stale `DONE`, question и output до
terminal commit, даже если inbox/application ещё не завершились.

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

IR-4/IR-5 checkpoint recheck покрывает только pre-final/checkpoint observation.
Short durable watermark recheck, output fencing и terminal commit ownership будут
реализованы здесь.

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

## Статус

Planned. Snapshot/control retry repair IR-4/IR-5 не является startup recovery.

## Цель

Recover durable runtime before accepting new work and shutdown without orphan
claims/runners.

## Startup

- reconcile sessions/admissions/inbox/controls;
- repair committed-but-unadmitted inputs;
- inspect runtime handoff markers;
- keep ambiguous external operation non-replayable;
- reconcile post-handoff claim evidence by explicit policy;
- reconstruct resumable/paused/waiting runner only when safe;
- enable new admission after mandatory recovery.

IR-3 marker, IR-4 snapshot-first marking repair и IR-5 same-delivery control repair
не являются готовой startup recovery implementation.

`recover_cycle_authority()` corruption matrix также не считается закрытой IR-5:
её authoritative corruption/restart policy и negative cases относятся к IR-8, а
полная randomized/restart acceptance — к IR-10.

## Shutdown

- stop new runner starts;
- persist resumable/interrupted state where possible;
- cancel in-process wakeups;
- retain durable admissions/inbox/claims/controls;
- leave ambiguous external operations unknown;
- close runtime after agent/tool lifecycle.

## Tests

Two service instances over same temporary root:

- pause/wait/inbox survive recreation;
- committed unadmitted repaired;
- ambiguous marker does not rerun runtime;
- post-handoff claim evidence preserved/reconciled;
- `recover_cycle_authority()` corrupted/missing/divergent authority cases;
- no new work before recovery ready;
- shutdown deterministic.

---

# IR-9 — Client projections, diagnostics и configuration examples

## Статус

Planned. IR-5 добавил минимальные control localizations/status watermarks, но не
объявляет полный IR-9 completed.

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

## Статус

Planned. IR-5 focused CI не заменяет full IR-10 acceptance.

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
- cancellation before/after handoff and during cleanup;
- `recover_cycle_authority()` corruption/restart matrix.

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

Telegram history rewind по edited message остаётся deferred client-specific
follow-up и не входит в этот release gate.

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

IR-6 может интегрироваться после stable cycle/context/control identity и до
завершения finalization barrier.

Scheduler/parallel branches не реализуются этим update stage sequence; future
scheduler остаётся отдельной orchestration layer.

---

# Рекомендуемые commit boundaries

```text
feat(input-runtime): add domain contracts and config
feat(input-runtime): add filesystem repositories and claims
feat(input-runtime): route committed batches through admission
fix(input-runtime): close IR-3 capacity and runner handoff gaps
fix(input-runtime): make IR-3 handoff cancellation-safe and portable
feat(input-runtime): apply additions at safe checkpoints
feat(input-runtime): implement IR-5 durable controls
feat(input-runtime): add intermediate agent emissions
feat(input-runtime): guard waiting and finalization races
feat(input-runtime): recover durable active runtime state
test(input-runtime): add race restart and transport coverage
docs(input-runtime): finalize acceptance and current baseline
```

IR-5 documentation evidence фиксируется отдельным documentation-only commit после
зелёных code gates; этот commit не начинает IR-6.

Один commit не должен одновременно вводить новый state contract, переписывать
agent loop и менять transport presentation без characterization tests.
