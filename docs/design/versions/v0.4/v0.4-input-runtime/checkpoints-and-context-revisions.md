---
id: design.v0.4.input-runtime.checkpoints
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: planned
last_reviewed: 2026-08-05
---

# Safe checkpoints и context revisions

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

## Обязательные checkpoints

### `CP-RESUME`

После восстановления/`/continue`, до первой новой main iteration.

Порядок:

```text
validate generation/snapshot
→ apply effective controls
→ drain bounded inbox range
→ rebuild runtime projections
→ continue
```

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

### `CP-BEFORE-FINAL-PROCESSING`

Перед final audit/formatting/grounding. Позволяет не тратить дополнительный LLM
вызов на уже устаревший draft.

### `CP-BEFORE-TERMINAL-COMMIT`

После final processing, перед durable result/outbox/terminal state. Выполняется
под finalization protocol из `finalization-and-recovery.md`.

### `CP-AFTER-INTERRUPTION`

При controlled infrastructure/context interruption перед сохранением resumable
snapshot. Применение ordinary input допускается только если current protocol
block уже закрыт.

## Checkpoint order

Каждый checkpoint выполняет единый deterministic pipeline:

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
lock. Он только сохраняет canonical event/emission intent.

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
- prompt-injection policy для пользовательских файлов сохраняется.

## Context revision creation

Initial batch создаёт `R1`.

Каждый успешный apply range создаёт ровно одну следующую revision:

```text
R1 --apply ibat_2,ibat_3--> R2
```

Если checkpoint не применил input и не выполнил semantic resume/recovery change,
новая revision не создаётся.

`/continue` без input может создать revision с reason `resumed`, если это нужно
для audit. Эта revision не добавляет LLM message автоматически; runtime notice
может быть trace-only.

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

Scheduler/workflow revision появятся отдельно.

## Artifact integration

При apply:

- exact input artifact refs добавляются к cycle access set;
- существующие refs не теряются;
- current/session/workspace scopes сохраняют принятые ограничения;
- новый batch не подменяет `original_input_batch_id`;
- artifact catalog projection перестраивается из authoritative stores;
- duplicate apply не создаёт повторную activation/version;
- blocked signature/side-effect policies проверяются как для initial input.

Текущий WAITING_USER compatibility path, временно заменяющий
`original_input_batch_id`, должен быть удалён после перехода на общий applier.

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
control command policy; initial `/stop` также ждёт конца atomic block.

## Input перед final candidate

```text
model returned DONE based on R4
→ accepted watermark advanced to 5
→ CP-BEFORE-FINAL-PROCESSING detects mismatch
→ DONE suppressed
→ input applied as R5
→ continue
```

Если input приходит после final processing, тот же guard выполняется в
`CP-BEFORE-TERMINAL-COMMIT`.

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

Recovery видит applied watermark и завершает marking без повторного LLM append.

### Progress/emission failure

Не откатывает уже применённый input. Projection/delivery retry выполняется
отдельно.

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

## Acceptance

- update никогда не появляется между assistant tool call и tool result;
- input во время LLM применяется после завершённого tool block;
- multiple additions создают один ordered update/revision;
- fresh update не compact-ится до первого meaningful response;
- compaction не уничтожает replay identity;
- plan state не сбрасывается неявно;
- artifact refs прежних/new batches остаются доступными по policy;
- snapshot-write/mark-applied crash не дублирует user message;
- stale DONE/WAITING_USER подавляется на обоих final checkpoints.
