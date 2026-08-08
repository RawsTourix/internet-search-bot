---
id: design.v0.4.input-runtime.checkpoints
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-08-08
---

# Safe checkpoints и context revisions

## Статус реализации IR-4 и IR-7 integration

IR-4 реализован на code/test HEAD
`1d31b6fbd1d5e88966d3964dc35cf4680f32f522` и подтверждён:

- `Validate Input Runtime` #115 — success, compile success, `241 passed`,
  `0 failed`;
- `Validate v0.4 file artifacts PR` #518 — success, validation suites и status
  enforcement success.

Regression-fix проход после `224911a…` менял только tests; production code на
этом проходе не менялся.

Реализованный IR-4 checkpoint/context contract:

- до первого main LLM/result initial cycle проходит `CP-RESUME`; создаются initial
  `R1` и durable `ActiveCycleSnapshot`;
- context revisions линейны: каждый applied range создаёт ровно одну следующую
  `CycleContextRevision`;
- additions применяются bounded contiguous FIFO ranges в `cycle_sequence` order;
- checkpoint использует accepted-at-entry watermark: input, admitted после входа
  в checkpoint, остаётся для следующего checkpoint;
- каждый applied range создаёт один `input_batch_update`, независимо от числа
  contiguous batches внутри range;
- checkpoint matrix не вставляет input внутрь незакрытого LLM/tool atomic block;
- WAITING reply проходит общий FIFO `CP-RESUME`, не владеет legacy semantic path
  и не подменяет `original_input_batch_id`;
- snapshot-first persistence делает durable snapshot watermark authority для
  crash reconciliation: незавершённые inbox/admission marks домаркировываются без
  второго update/revision;
- claim acquisition и apply cancellation-safe;
- successful runtime handoff завершается до terminal snapshot synchronization;
- stale WAITING/final candidates подавляются на checkpoint-level, когда accepted
  watermark уже опережает applied watermark.

IR-7 не меняет IR-4 safe-checkpoint semantics, а добавляет обязательную вторую
линию защиты после них. После corrective pass на code/test boundary
`eb93918b33ce7503d0e2d5d032b7e600f51e5661` подтверждены:

- `CP-BEFORE-FINAL-PROCESSING` остаётся ранним stale-DONE suppression point;
- clean DONE candidate фиксирует exact generation/context/input/control authority
  и exact runtime-owned `admission_id + handoff_token`, но final-processing LLM
  call не резервирует terminal right;
- `PREPARED/FINALIZING` и второй pre-terminal recheck используют authoritative
  accepted/applied input и pending/applied control watermarks;
- second terminal recheck выполняется **до** durable RuntimeHandoff completion;
- successful order: `second recheck → RuntimeHandoff COMPLETED → terminal
  snapshot/session → TERMINAL_COMMITTED`;
- mismatch на second recheck abort-ит finalization, оставляя handoff `HANDED_OFF`
  и cycle способным продолжить работу;
- `CP-BEFORE-WAITING` дополняется short exact input/control recheck перед одной
  durable waiting-question authority;
- late durable input/control между checkpoint observation и commit suppresses
  stale final/question candidate;
- `Validate Input Runtime` #387 — success, production compile success,
  `384 passed`, `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #654 — success;
- workflow permission остаётся `contents: read`.

Ambiguous handoff/startup reconciliation остаётся IR-8. Полная corruption matrix
`recover_cycle_authority()` остаётся IR-8/IR-10.

## Назначение

Документ определяет, где существующий agent loop может наблюдать control/input,
как additions превращаются в protocol-valid LLM history и как semantic context
revision взаимодействует с compaction, planning и artifacts.

## Atomic runtime blocks

Новый input не изменяет уже начатый atomic block.

В первой реализации atomic blocks:

```text
one main LLM request/response
one complete assistant tool-call block + all matching role=tool results
one compaction operation
one inbox apply operation
one final-processing LLM operation
```

Обычный user input не вставляется внутрь этих blocks.

`/stop` также по умолчанию применяется после полного block. Отдельная остановка
между несколькими native tool calls не входит в первоначальную реализацию: она
потребовала бы synthetic cancellation results и отдельной durable незавершённой
tool-block state machine.

`/stop` и durable control application реализованы IR-5 поверх этой checkpoint
matrix и не меняют atomic-block contract IR-4.

## Обязательные checkpoints

### `CP-RESUME`

После восстановления/`/continue`, до первой новой main iteration.

Порядок accepted full contract:

```text
validate generation/snapshot
→ apply effective controls
→ drain bounded inbox range
→ rebuild runtime projections
→ continue
```

IR-4 реализовал generation/snapshot validation, initial context initialization и
common FIFO input drain. IR-5 добавил `/continue` и effective durable controls.

### `CP-BEFORE-LLM`

Непосредственно перед каждым main LLM request.

Гарантирует, что input, already accepted до request construction, попадает в
следующую context revision.

### `CP-AFTER-TOOL-BLOCK`

После того как все tool calls одного assistant message получили matching
`role=tool` result и durable result/artifact handling завершён.

Это основная точка применения additions, пришедших во время tool execution.

### `CP-BEFORE-WAITING`

После candidate `ask_user`, но до фиксации `waiting_user` и доставки вопроса.

Если accepted input уже существует на checkpoint:

```text
suppress candidate question
→ apply input
→ continue cycle
```

IR-4 реализует это checkpoint-level suppression. IR-7 дополняет его отдельным
коротким exact-session recheck непосредственно перед durable waiting commit. Если
input/control становится durable после checkpoint observation, но до waiting
commit, stale question всё равно suppressed. После successful waiting commit
late input использует existing `RESUME_WAITING` same-cycle semantics.

Corrective handoff ordering не меняет WAITING path и не связывает question с IR-6
intermediate emission lifecycle.

### `CP-BEFORE-FINAL-PROCESSING`

Перед final audit/formatting/grounding. Позволяет не тратить дополнительный LLM
вызов на уже устаревший draft.

IR-7 после clean checkpoint фиксирует exact candidate authority
`session/cycle/generation/context_revision/expected input+control watermarks` и
exact current RuntimeHandoff relation. Final processing выполняется вне
coordination lock и не резервирует terminal authority: новый durable input/control
всё ещё может отменить candidate позже.

### `CP-BEFORE-TERMINAL-COMMIT`

После final processing, перед durable result/outbox/terminal state. Полный
terminal commit выполняется под finalization protocol из
`finalization-and-recovery.md`.

IR-4 checkpoint-level recheck остаётся первой линией suppression. IR-7 не считает
его достаточным terminal barrier: после него existing finalization protocol делает
`PREPARED` exact-session recheck, persist result/output, затем обязательный второй
short authoritative recheck.

Corrective ordering после этого second recheck:

```text
if mismatch:
    ABORTED_NEW_INPUT | ABORTED_CONTROL
    RuntimeHandoff remains HANDED_OFF
    same cycle may continue
else:
    RuntimeHandoff COMPLETED
    → terminal snapshot/session convergence
    → TERMINAL_COMMITTED
    → output claim eligible
```

Поэтому late input/control после checkpoint observation, после PREPARED, после
result persistence или после `OUTPUT_READY` всё ещё может выиграть до handoff
completion/terminal authority. Нельзя complete handoff раньше second recheck,
потому что после `COMPLETED` этот exact side-effecting invocation больше не должен
выполнять LLM/tool work.

### `CP-AFTER-INTERRUPTION`

При controlled infrastructure/context interruption перед сохранением resumable
snapshot. Применение ordinary input допускается только если current protocol
block уже закрыт.

## Checkpoint order

Полный current pipeline для `v0.4-input-runtime`:

```text
1. load SessionInputRuntimeState
2. reject stale generation/cycle ownership
3. read effective control commands
4. apply reset/pause decision
5. if terminal pause/reset reached: persist snapshot and return
6. claim bounded contiguous inbox range
7. load and validate committed batches
8. project input_batch_update
9. activate artifacts/runtime refs
10. create context revision
11. persist cycle snapshot + watermarks
12. mark inbox/admission applied
13. publish canonical events/projections
14. return CheckpointOutcome
```

IR-4 реализует input/context slice этого pipeline: cycle/generation authority,
accepted-at-entry watermark, bounded contiguous claim/apply, committed-batch
validation/projection, context revision, snapshot-first persistence,
mark-applied reconciliation и typed checkpoint outcome. Steps 3—5 реализованы
IR-5 durable controls; durable `AgentEmission` в step 13 принадлежит IR-6;
durable terminal/finalization ownership реализован IR-7 как отдельный protocol
после соответствующих checkpoint hooks.

```python
class CheckpointOutcome(BaseModel):
    checkpoint: str
    decision: Literal[
        "continue",
        "paused",
        "reset",
        "input_applied",
        "no_change",
        "interrupted",
    ]
    context_revision_id: str | None
    applied_input_batch_ids: list[str]
    applied_through_sequence: int
```

Checkpoint не вызывает client delivery синхронно внутри session coordination
lock. Он только сохраняет canonical runtime state/outcome. IR-6 durable emission
и IR-7 result/output/finalization delivery gates также не выполняют network await
под exact-session coordination.

## Accepted-at-entry watermark

Checkpoint фиксирует accepted watermark при входе. Drain может выполнить несколько
bounded FIFO apply operations, чтобы дойти именно до этого watermark, но не
расширяет текущую semantic boundary за счёт additions, admitted позже.

```text
entry accepted = 5
current applied = 2
→ apply 3..4 bounded range
→ apply 5 bounded range
→ stop at 5
```

Input с sequence `6`, admitted во время checkpoint, остаётся queued для следующего
safe checkpoint. Это сохраняет deterministic relation между atomic runtime block и
использованной context revision.

Для finalization этот accepted-at-entry contract намеренно не является terminal
authority: IR-7 rechecks latest durable session watermarks после final processing
и непосредственно перед RuntimeHandoff completion/terminal writes.

## `input_batch_update` projection

Несколько contiguous batches представляются одним runtime-owned user message:

```json
{
  "type": "input_batch_update",
  "context_revision_id": "ctxrev_...",
  "batches": [
    {
      "input_batch_id": "ibat_...",
      "cycle_sequence": 1,
      "text_parts": [],
      "artifact_refs": [],
      "continuation_of_batch_id": null,
      "correction_of_batch_id": null
    }
  ],
  "runtime_generated": true
}
```

Требования:

- batches упорядочены по `cycle_sequence`;
- границы batches сохраняются;
- text parts внутри batch сохраняют canonical order/kind;
- raw binary payload не помещается в LLM history;
- artifact projection содержит exact refs, safe metadata и runtime-owned access;
- route/client metadata не выдаётся модели без необходимости;
- runtime marker защищает сообщение от трактовки как transport-generated system
  instruction;
- prompt-injection policy для пользовательских файлов сохраняется;
- один applied contiguous range создаёт ровно один `input_batch_update`; retry
  persisted snapshot range не создаёт второй message.

## Context revision creation

Initial batch создаёт `R1` на `CP-RESUME` до первого main LLM/result. Вместе с
`R1` durable создаётся initial `ActiveCycleSnapshot`, который становится authority
для последующих applied watermarks и context identity.

Каждый успешный apply range создаёт ровно одну следующую revision:

```text
R1 --apply ibat_2,ibat_3--> R2
```

Context revisions в IR-4 линейны: next revision имеет ровно предыдущую active
revision как parent. Identity сохраняет возможность multiple parents для будущей
архитектуры, но branch/merge semantics здесь не реализованы.

Если checkpoint не применил input и не выполнил semantic resume/recovery change,
новая revision не создаётся.

IR-5 `/continue` без input не создаёт fake input message/revision. Resume audit
остаётся отдельной control/snapshot authority.

## Physical prompt и semantic revision

Semantic revision не равна prompt snapshot.

```text
context revision R3
→ compaction generation 1
→ provider overflow rebuild
→ compaction generation 2
```

Все три prompt representation могут относиться к R3. Compaction не создаёт
новую semantic revision, если не добавлены новые user constraints/artifacts.

Active cycle сохраняет:

```text
active_context_revision_id
working_memory generation/reference
applied_through_cycle_sequence
```

Durable `ActiveCycleSnapshot` сохраняет ту же semantic authority вместе с
messages/runtime refs, generation, applied batch IDs, safe checkpoint и snapshot
revision.

IR-7 finalization candidate сохраняет exact `active_context_revision_id`, но
terminal eligibility определяется latest authoritative generation/cycle и
accepted/applied input + pending/applied control watermarks. Новая context
revision, возникшая из late input, инвалидирует stale exact candidate.

## Compaction integration

### Непрочитанные additions

Queued/claimed input не включается в compaction. Source of truth остаётся inbox и
committed batch store.

### Свежий update

Новый `input_batch_update` считается open semantic segment и не должен быть
сразу скрыт в summary до первого содержательного LLM/tool response на эту
revision.

### Replay identity

Compaction summary не является доказательством применения batch. Даже если
сообщение удалено из physical prompt, admission/snapshot/context revision records
сохраняют точные IDs и watermarks.

### Перед apply

Если текущая closed history близка к budget, runtime может compact закрытый
segment до append update. Нельзя compact queued additions вместо их применения.

## Planning integration

Новый input не сбрасывает активный DAG автоматически.

После apply модель/plan runtime получает update и может:

- продолжить текущий plan;
- вызвать существующий plan revision flow;
- изменить active node после valid transition;
- задать вопрос пользователю;
- завершить plan reconciliation.

Runtime-owned guards продолжают действовать. Если update делает текущий plan
очевидно несогласованным, checkpoint публикует trace `input_applied_to_active_plan`,
но не придумывает новую plan semantics детерминированно.

Для v0.6 сохраняются relations:

```text
context_revision_id
active_plan_id/revision/node
input admission IDs
```

Scheduler/workflow revision и parallel branches появятся отдельно; IR-4/IR-7 их
не реализуют.

## Artifact integration

При apply:

- exact input artifact refs добавляются к cycle access set;
- существующие refs не теряются;
- current/session/workspace scopes сохраняют принятые ограничения;
- новый batch не подменяет `original_input_batch_id`;
- artifact catalog projection перестраивается из authoritative stores;
- duplicate apply не создаёт повторную activation/version;
- blocked signature/side-effect policies проверяются как для initial input.

Legacy WAITING compatibility adapter больше не владеет semantic continuation.
`WAITING_USER` reply admitted в тот же cycle и проходит общий FIFO `CP-RESUME`;
более ранние queued additions применяются сначала по cycle sequence, а initial
`original_input_batch_id` сохраняется.

## Result и tool sequence integrity

Checkpoint разрешён только если:

```text
validate_openai_tool_sequence(messages_for_llm) == valid
```

После append update sequence снова валидируется в tests/debug policy.

Если tool result persistence завершилась, но artifact projection ещё нет, это не
safe checkpoint: сначала завершается весь tool handling contract.

IR-7 WAITING/final suppression не создаёт synthetic unmatched tool result и не
перепрофилирует `send_user_message` как question. Native OpenAI tool/message
sequence остаётся той же IR-4/IR-6 authority.

## Input во время LLM request

```text
LLM request uses R2
→ user addition admitted as sequence 3
→ LLM returns tool calls based on R2
→ complete tool block
→ CP-AFTER-TOOL-BLOCK applies input
→ next LLM request uses R3
```

Уже выбранное действие не отменяется автоматически. Исключение — отдельная
control command policy; IR-5 `/stop` также ждёт конца atomic block.

## Input перед final candidate

```text
model returned DONE based on R4
→ accepted watermark advanced to 5
→ CP-BEFORE-FINAL-PROCESSING detects mismatch
→ DONE suppressed
→ input applied as R5
→ continue
```

Если checkpoint clean, IR-7 фиксирует exact final candidate authority. Возможный
late input после final-processing start не вставляется внутрь этого immutable
LLM block, но durable accepted watermark немедленно лишает старый candidate права
на terminal commit:

```text
final processing based on R4
→ late input durable accepted
→ PREPARED or second terminal recheck sees mismatch
→ ABORTED_NEW_INPUT before RuntimeHandoff completion
→ stale result/output not deliverable
→ handoff remains active
→ same cycle continues and later applies input
```

То же правило действует для durable pending control. Persisted result или
`OutputBatch READY` не заменяют terminal authority.

## Error handling

### Apply validation error

- claim не отмечается applied;
- error code сохраняется;
- cycle переводится в controlled interrupted/error по severity;
- raw payload не логируется;
- permanent corruption требует explicit terminal decision.

### Snapshot persist failed

Inbox items не отмечаются applied. Retry/recovery сверяет repository state.

### Mark-applied failed после snapshot

Persisted snapshot watermark является authority. Reconciliation завершает
inbox/admission marking без повторного LLM append, без новой context revision и
без второго `input_batch_update`. Повторный checkpoint после repair является
no-op относительно уже applied range.

### Cancellation во время claim/apply

Claim acquisition и apply имеют cancellation-safe cleanup. До durable snapshot
claim может быть безопасно requeued/reconciled по состоянию; после snapshot
watermark retry завершает marking из snapshot authority. Cancellation не должна
создавать duplicate update/revision.

### Progress/emission failure

Не откатывает уже применённый input. Projection/delivery retry выполняется
отдельно. Durable semantic `AgentEmission` реализован IR-6; IR-7 shared terminal
ordering не превращает delivery failure в AgentCycle failure.

### Cancellation/failure в terminal coordination

До durable RuntimeHandoff completion terminal authority не появляется; existing
handoff ambiguity cleanup остаётся применимым. После durable COMPLETED handoff не
может быть понижен обратно в AMBIGUOUS; если terminal marker ещё не записан,
output остаётся fenced и known-ID direct retry завершает convergence без LLM/tool
replay.

## Runtime handoff и terminal snapshot

IR-3 `RuntimeHandoffRecord` остаётся authority side-effecting runtime invocation.
На successful runtime path exact ordering теперь фактически обеспечен IR-7:

```text
second authoritative terminal recheck
→ RuntimeHandoff COMPLETED
→ terminal ActiveCycleSnapshot
→ terminal SessionInputRuntimeState
→ Finalization TERMINAL_COMMITTED
→ final OutputBatch claim eligibility
```

Handoff нельзя завершать до second recheck: если late input/control выиграл,
finalization abort-ится, handoff остаётся active и same `process_query()` может
продолжить semantic work. После COMPLETED новый LLM/tool side effect этого exact
invocation не запускается.

`TERMINAL_COMMITTED` marker записывается последним и является final-output
delivery fence. Crash после COMPLETED, но до terminal marker, direct-retry-ится
по известным IDs; startup-wide discovery этого состояния остаётся IR-8.

## Current-code integration points

До modularization hooks добавлены минимально:

```text
Api admitted-cycle lifecycle
→ begin exact RuntimeHandoff + task-local runtime-owned identity
→ MCPClient.process_query
   → after cycle create/resume
   → before main LLM request
   → after complete tool handling
   → before WAITING_USER
   → before final processing
   → before terminal return
→ IR-7 finalization command completes exact RuntimeHandoff only after second recheck
→ process_query returns
→ API compatibility complete_runtime_handoff() is idempotent
```

Hooks делегируют `InputRuntimeCheckpointService`. IR-7 production MRO поверх тех
же hooks делегирует durable candidate/finalization/waiting operations
`FinalizationBarrierService`. Application layer не получает filesystem path/lock
и не реализует store layout внутри `mcp_client.py`.

`ActiveAgentCycle` получает только bounded runtime fields и service-facing
identity. Concrete repositories создаются composition root `Api`.

## Deferred после IR-7

Уже реализованы отдельными stages:

- IR-5 controls, `/stop`, `/continue`, durable-generation `/reset`;
- IR-6 durable semantic emissions;
- corrected IR-7 durable finalization/waiting barrier, handoff-before-terminal
  ordering и late terminal race closure.

Остаются deferred:

- IR-8 startup recovery, ambiguous handoff/startup reconciliation;
- `recover_cycle_authority()` corruption matrix — IR-8/IR-10;
- IR-9 complete client projections/diagnostics;
- IR-10 randomized/restart/full-system/live acceptance;
- scheduler/parallel context branches;
- Telegram history rewind.

## Acceptance

IR-4 подтверждает:

- update никогда не появляется между assistant tool call и tool result;
- input во время LLM применяется после завершённого tool block;
- multiple additions в одном applied range создают один ordered update/revision;
- accepted-at-entry watermark ограничивает текущий checkpoint drain;
- initial `R1` и durable `ActiveCycleSnapshot` существуют до first main execution;
- fresh update не compact-ится до первого meaningful response;
- compaction не уничтожает replay identity;
- plan state не сбрасывается неявно;
- artifact refs прежних/new batches остаются доступными по policy;
- snapshot-write/mark-applied crash не дублирует user message/revision;
- WAITING reply использует общий FIFO `CP-RESUME` и сохраняет initial identity;
- stale DONE/WAITING_USER подавляется на checkpoint-level final boundaries;
- handoff completion предшествует terminal snapshot synchronization;
- claim/apply cancellation не создаёт duplicate application.

Corrected IR-7 acceptance дополнительно подтверждает:

- input/control durable accepted после checkpoint observation всё ещё suppresses
  stale DONE/question до terminal/waiting commit;
- final processing не резервирует terminal authority;
- finalization exact context/watermarks rechecked на PREPARED и непосредственно
  перед handoff completion/terminal authority;
- late mismatch abort-ит до RuntimeHandoff completion;
- successful handoff COMPLETED durable предшествует terminal snapshot/session и
  TERMINAL_COMMITTED;
- completion write failure не создаёт terminal authority;
- result/output persistence не открывает final delivery;
- normal admitted-run output claim требует terminal marker + matching completed
  handoff;
- completed-handoff/incomplete-terminal known-ID direct retry сохраняет exact IDs
  и не replay-ит LLM/tool work;
- stale output после late input/control не становится claimable;
- waiting commit создаёт одну durable question authority;
- no LLM/tool/network await выполняется под finalization/session coordination.

Corrected code evidence: `eb93918b33ce7503d0e2d5d032b7e600f51e5661`,
`Validate Input Runtime` #387 — `384 passed`, compile success; artifact validation
#654 — success.

Startup/ambiguous recovery и corruption recovery matrix остаются соответственно
IR-8 и IR-8/IR-10.
