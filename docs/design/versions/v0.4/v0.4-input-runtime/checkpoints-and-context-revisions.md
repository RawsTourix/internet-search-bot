---
id: design.v0.4.input-runtime.checkpoints
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-08-07
---

# Safe checkpoints и context revisions

## Статус реализации IR-4

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

IR-4 не является durable finalization/recovery completion. Late terminal race
между последним checkpoint-level recheck и durable terminal commit остаётся IR-7.
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

`/stop` и durable control application из этого абзаца относятся к accepted IR-5
contract и на IR-4 ещё не реализованы.

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

На IR-4 реализованы generation/snapshot validation, initial context
initialization и common FIFO input drain. `/continue` и effective durable controls
остаются IR-5.

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

Если accepted input уже существует:

```text
suppress candidate question
→ apply input
→ continue cycle
```

Это suppression реализовано на checkpoint-level. Durable delivery/finalization
barrier для более поздних races не входит в IR-4.

### `CP-BEFORE-FINAL-PROCESSING`

Перед final audit/formatting/grounding. Позволяет не тратить дополнительный LLM
вызов на уже устаревший draft.

### `CP-BEFORE-TERMINAL-COMMIT`

После final processing, перед durable result/outbox/terminal state. Полный
terminal commit выполняется под finalization protocol из
`finalization-and-recovery.md`.

IR-4 реализует checkpoint-level input recheck/suppression на этой boundary, но не
IR-7 durable finalization barrier. Поэтому input, пришедший после последнего
checkpoint recheck в late terminal window, остаётся явным IR-7 race.

### `CP-AFTER-INTERRUPTION`

При controlled infrastructure/context interruption перед сохранением resumable
snapshot. Применение ordinary input допускается только если current protocol
block уже закрыт.

## Checkpoint order

Полный accepted pipeline для `v0.4-input-runtime` остаётся:

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
mark-applied reconciliation и typed checkpoint outcome. Steps 3—5 принадлежат
IR-5 durable controls; durable `AgentEmission` в step 13 принадлежит IR-6;
durable terminal/finalization ownership относится к IR-7.

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

Accepted outcome surface сохраняет будущие pause/reset decisions. Их наличие в
specification не означает, что IR-5 controls уже реализованы.

Checkpoint не вызывает client delivery синхронно внутри session coordination
lock. Он только сохраняет canonical runtime state/outcome; durable emission intent
будет принадлежать IR-6.

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

`/continue` без input может создать revision с reason `resumed`, если это нужно
для audit. Эта accepted возможность относится к IR-5; durable `/continue` на IR-4
ещё не реализован. Такая revision не добавляет LLM message автоматически;
runtime notice может быть trace-only.

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

Scheduler/workflow revision и parallel branches появятся отдельно; IR-4 их не
реализует.

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
control command policy; `/stop` относится к IR-5 и также должен ждать конца
atomic block.

## Input перед final candidate

```text
model returned DONE based on R4
→ accepted watermark advanced to 5
→ CP-BEFORE-FINAL-PROCESSING detects mismatch
→ DONE suppressed
→ input applied as R5
→ continue
```

Если input приходит после final processing, тот же checkpoint-level guard
выполняется в `CP-BEFORE-TERMINAL-COMMIT`.

Это не заменяет IR-7 durable finalization barrier. Input, accepted после
последнего checkpoint observation, но до durable terminal commit, должен быть
закрыт отдельным IR-7 short recheck/commit protocol.

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
отдельно. Durable semantic `AgentEmission` относится к IR-6 и ещё не реализован.

## Runtime handoff и terminal snapshot

IR-3 `RuntimeHandoffRecord` остаётся authority side-effecting runtime invocation.
На успешном path порядок IR-4:

```text
runtime result
→ complete RuntimeHandoffRecord
→ synchronize terminal ActiveCycleSnapshot
```

Terminal snapshot synchronization не выполняется вместо handoff completion и не
ослабляет post-handoff ambiguity contract.

## Current-code integration points

До modularization hooks добавляются минимально:

```text
MCPClient.process_query
→ after cycle create/resume
→ before main LLM request
→ after complete tool handling
→ before WAITING_USER
→ before final processing
→ before terminal return
```

Hooks делегируют `InputRuntimeCheckpointService`. Они не получают filesystem path
и не реализуют store logic внутри `mcp_client.py`.

`ActiveAgentCycle` получает только bounded runtime fields и service-facing
identity. Concrete repositories создаются composition root `Api`.

## Deferred после IR-4

Пока не реализованы:

- IR-5 controls, `/stop`, `/continue` и `/reset` redesign;
- IR-6 durable emissions;
- IR-7 durable finalization barrier и late terminal race closure;
- IR-8 startup recovery, ambiguous handoff/startup reconciliation;
- `recover_cycle_authority()` corruption matrix — IR-8/IR-10;
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

IR-4 acceptance **не** утверждает, что закрыт late terminal race, durable
finalization, startup/ambiguous recovery или corruption recovery matrix; эти
contracts остаются соответственно IR-7, IR-8 и IR-8/IR-10.
