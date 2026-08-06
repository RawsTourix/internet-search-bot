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
- `IR-3`—`IR-10`: planned.

IR-1 и IR-2 подтверждены code HEAD
`c7ed199deb0dfe042cff6055989a76e371537755`, workflow #67 и #494 и targeted
suite `164 passed`. Эти этапы создают domain/config/repository и durable
filesystem foundations, но не изменяют observable production behaviour:
production admission и agent-loop integration начинаются с IR-3.

Финальный IR-2 pass дополнительно подтвердил crash-recoverable global identity
protocol: durable record записывается до indexes, missing/dangling relations
восстанавливаются exact-identity scan под `root identity → session`, а competing
create после restart не создаёт скрытый duplicate.

## Назначение

Документ предназначен как каноническая основа для будущей пошаговой инструкции
ChatGPT/Codex. Каждый этап фиксирует:

- цель и границу ответственности;
- новые/изменяемые components;
- точки интеграции с текущим кодом;
- обязательные transitions/invariants;
- tests и acceptance;
- условие завершения этапа;
- запрещённое расширение scope.

Реализация выполняется небольшими совместимыми patches. Большой rewrite
`src/mcp/mcp_client.py`, преждевременная modularization и добавление PostgreSQL
запрещены.

## Предварительная инвентаризация

Перед IR-1 implementation agent обязан повторно прочитать актуальную branch и
зафиксировать current owners:

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
```

Обязательная characterization карта:

- где committed batch запускает `API.call_agent_batch()`;
- как создаётся/resumes `ActiveAgentCycle`;
- точный main LLM/tool loop и все return paths;
- где фиксируются `WAITING_USER`, `DONE`, interrupted/error;
- где выполняются final audit и output assembly;
- где хранятся session memory/pending cycle;
- какие progress callbacks и Telegram status targets используются;
- как `/reset` и shutdown ждут execution lease;
- какие tests уже фиксируют FIFO, WAITING_USER continuation, progress и delivery.

Результат инвентаризации — comments/tests либо отдельная implementation note в PR,
не новый competing canonical design document.

---

# IR-1 — Domain models, config и repository ports

## Статус

Implemented и подтверждён CI. Production runtime integration в этот этап не
входит.

## Цель

Создать независимый package `src/input_runtime/` с pure models, errors, config и
repository Protocols. На этом этапе observable behavior не меняется.

## Target files

```text
src/input_runtime/__init__.py
src/input_runtime/models.py
src/input_runtime/errors.py
src/input_runtime/config.py
src/input_runtime/interfaces.py
src/input_runtime/factory.py
```

Допустимые точечные изменения:

```text
src/runtime/cycle.py
src/api/config.py or current config loader/models
src/api/mcp.config.example
.env.example only if environment settings are introduced
```

## Models

Минимум:

```text
SessionInputRuntimeState
InputAdmissionRecord
InputAdmissionOutcome
CycleInboxItem
ClaimedInboxRange
SessionControlCommand
ControlOutcome
CycleContextRevision
AgentEmission
CycleFinalizationRecord
CheckpointOutcome
```

Stable ID factories и enums определяются один раз.

## Ports

```python
class SessionInputRuntimeRepository(Protocol): ...
class InputAdmissionRepository(Protocol): ...
class CycleInboxRepository(Protocol): ...
class SessionControlRepository(Protocol): ...
class ActiveCycleSnapshotRepository(Protocol): ...
class ContextRevisionRepository(Protocol): ...
class AgentEmissionRepository(Protocol): ...
class FinalizationRepository(Protocol): ...
```

Methods должны быть command-oriented, а не generic `save(dict)`:

```text
get/create session state
compare_and_swap state revision
find admission by batch
allocate/admit sequence
claim contiguous inbox range
mark applying/applied/requeue
append/claim control
persist active snapshot
append context revision
prepare/advance/abort finalization
```

## Config

Новая root section:

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

```text
tests/test_input_runtime_models.py
tests/test_input_runtime_config.py
tests/test_input_runtime_interfaces.py or type/contract tests
```

Проверить:

- validation всех states/IDs/sequences;
- invalid watermark/order rejected;
- config examples cover every supported setting;
- no import from Telegram/FastAPI/MCP concrete client in models/interfaces;
- serialization round-trip.

## Done

- package импортируется без side effects;
- текущий production behavior не изменён;
- ports пригодны для filesystem и PostgreSQL implementations;
- full existing suite green.

## Не делать

- не подключать stores к API;
- не добавлять checkpoints;
- не менять command handlers;
- не переносить agent loop.

---

# IR-2 — Filesystem repositories и coordination service

## Статус

Implemented и подтверждён CI. Реализованы durable filesystem adapters,
atomic-write/restart contracts, claims, sequence repair, bounded coordination,
global identity fencing и crash-recoverable index rebuilding. Production
admission/API integration в этот этап не входит.

## Цель

Реализовать durable local backend, atomic file replacement, per-session short
coordination и startup-readable indexes.

## Target files

```text
src/input_runtime/filesystem.py
src/input_runtime/coordination.py
src/input_runtime/serialization.py optional
src/input_runtime/factory.py
```

Использовать существующий `StorageConfigType.root_dir` и atomic-write policy.

## Storage layout

Конкретный layout может быть оптимизирован, но должен поддерживать:

```text
input-runtime/sessions/{session_id}/state.json
input-runtime/sessions/{session_id}/admissions/*.json
input-runtime/sessions/{session_id}/inbox/*.json
input-runtime/sessions/{session_id}/controls/*.json
input-runtime/cycles/{cycle_id}/snapshot.json
input-runtime/cycles/{cycle_id}/context-revisions/*.json
input-runtime/cycles/{cycle_id}/emissions/*.json
input-runtime/cycles/{cycle_id}/finalizations/*.json
```

User-controlled IDs не используются как raw path segments без safe encoding.

## Coordination

- one in-process lock per normalized session;
- lock registry bounded/cleanup-capable и cancellation-safe;
- global identities используют fixed order `root identity → session`;
- no await long LLM/tool/delivery operation inside lock;
- compare-and-swap repository revision;
- deterministic sequence allocation;
- duplicate creation returns existing record;
- create/append/prepare protocol: lookup/recovery, durable record write, затем
  index и cycle-authority writes;
- missing или dangling index запускает редкий exact-identity scan до competing
  create;
- один authoritative record rebuilds indexes, отсутствие record очищает dangling
  reservation, несколько records возвращают managed consistency error.

## Claim leases

Implement:

```text
claim head range
validate token
mark applying
mark applied
requeue expired claim
reconcile applying by snapshot watermark
```

## Tests

```text
tests/test_input_runtime_filesystem_repositories.py
tests/test_input_runtime_coordination.py
tests/test_input_runtime_claim_recovery.py
tests/test_input_runtime_ir2_index_recovery.py
```

Race/crash tests:

- concurrent sequence allocation;
- duplicate admission creation;
- claim conflict;
- stale CAS revision;
- atomic write interruption simulation;
- crash после durable record до первого index;
- crash после первого index до остальных indexes;
- dangling pointer без durable record;
- lost stable/relation index и competing create из другой session;
- lost cycle authority и competing snapshot/context/emission create;
- ambiguous durable identities возвращают consistency error;
- path traversal/invalid IDs;
- restart instantiate two repository bundles over same root.

## Done

- repositories survive process recreation;
- deterministic ordered list operations;
- no duplicate IDs/sequences under race;
- global indexes и cycle authority восстанавливаются после partial metadata write;
- dangling reservation не блокирует identity навсегда;
- no current API integration yet.

## Не делать

- не emulate SQL transaction through global lock across long operations;
- не add Redis;
- не scan whole repository on every hot-path operation if index can be bounded.

---

# IR-3 — Admission service и initial-cycle integration

## Цель

Every committed batch проходит `InputAdmissionService`. New input during active
cycle no longer starts second run operation.

## Target files

```text
src/input_runtime/admission.py
src/input_runtime/service.py
src/api/api.py
src/core/message_processor.py
src/api/artifact_transport.py or current run_committed facade
src/runtime/session_execution.py
```

## Composition

`Api.__init__` создаёт `InputRuntimeServices` через factory и injects:

- committed batch store;
- runtime repositories;
- execution coordinator/wakeup port;
- cycle runner callback/facade;
- capability/interaction projection dependencies later.

## API changes

Добавить service-neutral method:

```python
async def admit_committed_batch(
    input_batch_id: str,
    *,
    session_id: str,
) -> InputAdmissionOutcome
```

Existing `call_agent_batch()` становится internal start/resume runner path, а не
безусловным post-commit action.

Callers:

```text
submit/commit
→ admit
→ start_cycle: launch runner
→ queued/resume: return acknowledgement/wakeup
```

## Initial cycle

Admission `start_cycle`:

1. allocate cycle ID and sequence 0;
2. persist admission/session state;
3. create initial context revision;
4. acquire execution lease;
5. call current agent runtime with exact initial batch/cycle ID;
6. bind active snapshot in later IR stage or compatibility representation now.

No second runner can be created for same active session.

## Running addition

- persist admission + inbox item;
- advance accepted watermark;
- signal runner;
- return immediately without awaiting final AgentResult.

## Waiting/interrupted compatibility

At first keep existing continuation mixin behind admission outcome. Do not remove
until IR-4 parity.

## SessionExecutionCoordinator migration

Keep:

- run lease;
- worker/wakeup event;
- generation defensive cache;
- diagnostics.

Stop treating its in-memory queue as source of truth for committed additions.
Existing FIFO batch runner can remain compatibility for initial start only until
callers migrated.

## Tests

```text
tests/test_input_runtime_admission.py
tests/test_input_runtime_api_admission.py
tests/test_input_runtime_no_parallel_cycle.py
```

Scenarios:

- idle batch starts one cycle;
- second batch while running returns queued and does not invoke process_query;
- duplicate batch returns existing outcome;
- finalizing batch advances watermark;
- paused/waiting decisions correct;
- session mismatch rejected;
- commit/admission crash recovery hook registered.

## Done

- every production committed batch goes through admission;
- no behavior regression for ordinary initial request;
- one active cycle invariant enforced durably and in-process;
- additional batch no longer waits as a separate conflicting cycle.

## Не делать

- не inject addition into loop yet;
- не expose raw repository objects to transport;
- не delete WAITING_USER compatibility before common applier exists.

---

# IR-4 — Active snapshot, checkpoints и CycleInputApplier

## Цель

Apply admitted additions to same running cycle at protocol-safe boundaries.

## Target files

```text
src/input_runtime/checkpoints.py
src/input_runtime/applier.py
src/input_runtime/context_revisions.py
src/runtime/cycle.py
src/mcp/mcp_client.py
src/mcp/waiting_user_batch_continuation.py
artifact runtime integration modules as narrowly required
```

## Active cycle fields

Add bounded fields:

```text
generation
applied_input_batch_ids
applied_through_cycle_sequence
active_context_revision_id
safe_checkpoint
pause/interruption metadata
```

Persistence uses `ActiveCycleSnapshotRepository`; Python object may keep caches
not serialized directly.

## Hooks

Add delegated hooks:

```text
after create/resume
before main LLM request
after complete tool block
before WAITING_USER
before final processing
before terminal return
```

One service method:

```python
await checkpoint_service.run(
    active_cycle=...,
    checkpoint=...,
    runtime_context=...,
)
```

No filesystem logic in `mcp_client.py`.

## Apply

- claim contiguous range;
- load exact batches;
- build one `input_batch_update`;
- activate refs through artifact service;
- append user message;
- persist context revision/snapshot/watermark;
- mark applied;
- emit canonical lifecycle events.

## Migration WAITING_USER

Route waiting reply through common admission/inbox/applier. Remove
`WaitingUserBatchContinuationMixin` only after equivalent tests pass. No mutation
of `original_input_batch_id` for additions.

## Tests

```text
tests/test_input_runtime_checkpoints.py
tests/test_input_runtime_applier.py
tests/test_input_runtime_context_revisions.py
tests/test_input_runtime_tool_sequence.py
tests/test_input_runtime_waiting_resume.py
```

Mandatory races:

- addition during LLM;
- addition during tool block;
- addition immediately before WAITING_USER;
- two additions between checkpoints;
- snapshot persisted/mark applied fails;
- expired applying claim reconciliation;
- compaction before/after update;
- active plan/artifact refs preserved.

## Done

- active cycle consumes new input without second cycle;
- protocol sequence always valid;
- stale WAITING_USER suppressed;
- additions applied exactly once/order preserved;
- compatibility mixin removed or reduced to no semantic ownership.

## Не делать

- не classify additions semantically;
- не create parallel branch/task;
- не reset plan automatically.

---

# IR-5 — Durable control plane `/stop`, `/continue`, `/reset`

## Цель

Добавить pause/resume without state loss и перевести reset на общий durable
control/generation contract.

## Target files

```text
src/input_runtime/controls.py
src/input_runtime/checkpoints.py
src/api/api.py
src/api/session_reset.py
src/core/message_processor.py
src/servers/telegram/runtime_commands.py new
src/servers/telegram/telegram_server.py or handler registration
localization catalogs
```

`batch_commands.py` сохраняет только collection commands.

## Runtime behavior

- `/stop` persists pause command and sets `pause_requested`;
- checkpoint completes current atomic block and persists `paused_by_user`;
- ordinary input during pause queues without runner wakeup;
- `/continue` resumes same cycle and drains pre-continue additions;
- `/reset` advances generation and invalidates old work/finalization/delivery.

## Telegram

- high-priority exact handler;
- idempotency key from bot/chat/thread/update/message;
- initial acknowledgement and applied completion projection;
- `ApplicationHandlerStop` prevents lower command handling;
- `/help` and `/status` updated.

## Tests

```text
tests/test_input_runtime_controls.py
tests/test_input_runtime_pause_resume.py
tests/test_input_runtime_reset_generation.py
tests/test_artifact_telegram_runtime_commands.py
```

Live/synthetic:

- stop during LLM;
- stop during multi-tool block;
- additions during pause;
- continue after several batches;
- duplicate stop/continue;
- reset racing finalization;
- existing `/collect|/send|/cancel` unchanged.

## Done

- pause survives restart;
- no next LLM/tool block after applied stop;
- state/messages preserved;
- same cycle resumes;
- reset prevents stale output delivery.

## Не делать

- не implement destructive conversation rewind;
- не reinterpret `/cancel` as runtime stop;
- не promise force cancellation of external side effects.

---

# IR-6 — AgentEmission и intermediate messages

## Цель

Создать durable semantic intermediate messages independent from progress/final.

## Target files

```text
src/input_runtime/emissions.py
src/input_runtime/filesystem.py
src/mcp/mcp_client.py manager tool registration/handler adapter
src/agent/protocol.py only canonical event additions if required
src/interaction/* emission output/delivery integration
src/api/api.py composition
Telegram/Web delivery facades/routes as required
localization catalogs for runtime notices/addendum lifecycle
```

## Manager tool

`send_user_message(message, kind=intermediate, importance=normal)`.

Policy validates:

- length;
- allowed kind;
- per-cycle count/rate;
- user visibility;
- trusted response route from runtime.

Handler persists emission before returning success to model.

## Delivery

Prefer reuse of output/delivery lifecycle primitives without pretending emission
is terminal `OutputBatch`. If separate store/outbox is introduced, receipts and
unknown semantics mirror existing interaction contracts.

## Tests

```text
tests/test_input_runtime_emissions.py
tests/test_input_runtime_emission_delivery.py
tests/test_input_runtime_emission_policy.py
```

Check:

- persistence before tool success;
- duplicate idempotency;
- delivery failure independent from cycle;
- no late emission after terminal commit;
- progress event remains transient;
- reply relation safe/optional.

## Done

- agent can send meaningful message and continue;
- emission visible in diagnostics/history;
- client-specific renderer not in agent loop.

## Не делать

- не convert all progress to emissions;
- не build distributed event bus;
- не expose arbitrary route to LLM.

---

# IR-7 — Finalization barrier

## Цель

Guarantee no stale final/waiting response ignores accepted input/control.

## Target files

```text
src/input_runtime/finalization.py
src/input_runtime/checkpoints.py
src/mcp finalization/derived mixins narrow integration
src/api/api.py
src/interaction/output_assembly/completion/store as required
```

## Integration

- final candidate remains non-terminal;
- pre-final processing checkpoint;
- prepared finalization record;
- short recheck expected watermarks;
- persist result/output;
- second terminal recheck;
- terminal commit enables delivery.

Output metadata adds:

```text
context_revision_id
consumed_through_cycle_sequence
consumed_through_control_sequence
```

## Tests

```text
tests/test_input_runtime_finalization.py
tests/test_input_runtime_finalization_races.py
tests/test_input_runtime_waiting_barrier.py
```

Deterministic barriers inject input/control:

- before final processing;
- during final processing;
- after result persistence;
- after output ready;
- immediately before terminal commit.

## Done

- all pre-terminal events abort stale finalization;
- post-terminal input creates new cycle;
- output not claimable before terminal commit;
- no duplicate cycle after restart.

## Не делать

- не couple delivery success to execution success;
- не resend unknown client delivery blindly.

---

# IR-8 — Startup recovery и lifecycle

## Цель

Recover durable input runtime before accepting new work and shut it down without
leaving orphan claims/runners.

## Target files

```text
src/input_runtime/recovery.py
src/input_runtime/service.py
src/api/api.py start/stop
src/gateway.py readiness/lifecycle if required
src/runtime/session_execution.py shutdown/wakeup integration
```

## Startup order

Implement order from `finalization-and-recovery.md`. New admission disabled until
mandatory reconciliation complete.

## Shutdown

- stop accepting new runner starts;
- persist interrupted/paused snapshots at safe boundary where possible;
- cancel in-process wakeup tasks;
- do not cancel durable inbox/admission records;
- leave ambiguous external operations unknown;
- close repositories after agent/tool lifecycle.

## Tests

```text
tests/test_input_runtime_startup_recovery.py
tests/test_input_runtime_shutdown.py
tests/test_input_runtime_restart_matrix.py
```

Use two service instances over same temporary root to simulate restart.

## Done

- pause/wait/inbox/finalization survive recreation;
- committed unadmitted batches repaired;
- no new work before recovery ready;
- shutdown leaves deterministic resumable state.

---

# IR-9 — Client projections, diagnostics и configuration examples

## Цель

Expose coherent user/admin status without leaking raw content and preserve
client independence.

## Target files

```text
src/core/message_processor.py
src/api routes/facades
src/servers/telegram/*
src/interaction/*
src/localization/catalogs/ru.json
src/localization/catalogs/en.json
.env.example
src/api/mcp.config.example
docs/configuration-contract.md if config surface changes
```

## `/status`

Add:

```text
runtime status/generation
active cycle
accepted/applied sequences
queued/claimed/applying additions
oldest queued age
pending/applied controls
emission states
finalization state
```

No raw user text/filenames beyond existing bounded safe policies.

## User-facing projections

- initial request: existing status/final behavior;
- running addition: accepted/current-task acknowledgement;
- paused addition: accepted but paused;
- applied addition: optional/coalesced completion;
- stop pending/applied;
- continue outcome;
- recovery/interrupted notices.

## Tests

```text
tests/test_input_runtime_localization.py
tests/test_input_runtime_status_projection.py
tests/test_input_runtime_client_capabilities.py
```

Examples audit must pass.

## Done

- RU/EN keys complete;
- Telegram without editing has safe fallback;
- Web/CLI can consume structured outcome;
- config examples/documentation synchronized.

---

# IR-10 — Full acceptance, roast и live validation

## Цель

Prove contracts under unit, race, restart and real transport behavior.

## Automated suites

At minimum:

```text
input-runtime focused suite
artifact/storage/plans/planning/API regression suites
Telegram transport/audit suite
full repository baseline
compile/config examples audit
```

## Race/randomized matrix

- concurrent commit/admission;
- addition at every checkpoint/finalization boundary;
- stop/continue/reset order randomization;
- claim expiry/restart;
- lost wakeup;
- output ready vs new input;
- duplicate transport requests;
- multiple additions with files;
- pause with active collection in same session;
- shutdown while queued/claimed/applying.

Every randomized run records seed and exact scenario selector.

## Synthetic no-network roast

Must not call real LLM/MCP/network/Telegram. Use deterministic fake runtime and
transport sinks.

## Maintainer live Telegram acceptance

Mandatory scenarios:

1. long agent run, send one text addition;
2. send several additions and files while tools execute;
3. `/stop` during visible work;
4. several additions while paused;
5. `/continue` and verify same cycle uses all additions;
6. intermediate agent message delivered while work continues;
7. addition immediately before final answer;
8. `/reset` while active;
9. restart while paused/waiting/queued;
10. existing `/collect`/`/send`/`/cancel` and artifact delivery regressions.

Live evidence records IDs/statuses without secrets or raw private payload.

## Completion gate

- zero deterministic failures;
- no unexplained flaky race;
- full baseline green;
- no production code bypasses admission for committed batches;
- no unresolved canonical documentation conflict;
- implementation status/current/roadmap/PR description updated;
- draft PR marked ready only after acceptance evidence.

---

# Допустимая параллельность patches

После IR-1:

- filesystem repositories;
- emission policy/model tests;
- localization drafts;
- synthetic test harness foundation

могут разрабатываться параллельно в независимых files.

Admission, checkpoint integration, controls и finalization выполняются
последовательно, потому что разделяют session state/watermarks.

IR-6 emissions можно интегрировать после stable cycle identity/context revision,
но до finalization barrier completion.

---

# Рекомендуемые commit boundaries

```text
feat(input-runtime): add domain contracts and config
feat(input-runtime): add filesystem repositories and claims
feat(input-runtime): route committed batches through admission
feat(input-runtime): apply additions at safe checkpoints
feat(input-runtime): add durable stop and continue controls
feat(input-runtime): add intermediate agent emissions
feat(input-runtime): guard waiting and finalization races
feat(input-runtime): recover durable active runtime state
test(input-runtime): add race restart and transport coverage
docs(input-runtime): finalize acceptance and current baseline
```

Названия могут уточняться, но один commit не должен одновременно вводить новый
state contract, переписывать agent loop и менять Telegram presentation без
характеризационных tests.
