---
id: design.v0.4.input-runtime.control-plane
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-08-07
---

# Control plane: `/stop`, `/continue`, `/reset`

## Implementation evidence

IR-5 control-plane slice реализован и подтверждён на code/test HEAD
`85c52d4b60a60786bdb10732eb0a52893a422eee`:

- `Validate Input Runtime` #173 — success, compile success, `278 passed`,
  `0 failed`;
- `Validate v0.4 file artifacts PR` #547 — success;
- deterministic tests покрывают concurrent sequence allocation, duplicate
  deliveries, record-first publication repair, pause/continue marker crash
  windows, blocked LLM, complete multi-tool block, rapid stop/continue, paused
  FIFO additions, same-cycle resume, reset generation/cleanup fencing,
  reset/pause vs `CP-BEFORE-TERMINAL-COMMIT` и Telegram source identity.

`SessionInputRuntimeState + SessionControlCommand + ActiveCycleSnapshot` являются
durable semantic authority. `SessionExecutionCoordinator` остаётся defensive
in-process lease/wake/generation cache и diagnostics; coordinator generation не
определяет reset semantics.

Граница implementation намеренно не включает IR-7/IR-8: checkpoint-level
control suppression реализовано, но late race `последний checkpoint/recheck →
новый control/input → terminal commit` остаётся IR-7. Startup reconstruction
paused/interrupted runner после process restart и общий reconciliation остаются
IR-8. Полная `recover_cycle_authority()` corruption matrix остаётся IR-8/IR-10.

## Назначение

Runtime controls отделяются от ordinary user input и ingress collection
commands.

```text
ordinary input
→ CommittedInputBatch → CycleInbox

runtime control
→ SessionControlCommand → safe checkpoint

input collection control
→ /collect | /send | /cancel → ingress draft control plane
```

`/cancel` текущего Telegram collection не означает остановку AgentCycle.

## Команды v0.4

```text
/stop      cooperative pause active cycle
/continue  resume paused/resumable cycle
/reset     invalidate generation and clear session runtime
```

Команды доступны через общий application API, а Telegram adapter создаёт
idempotent control request. Surface остаётся transport-neutral и пригоден для
будущих Web/CLI adapters.

## `/stop` semantics

`/stop` сохраняет всё authoritative состояние задачи:

- dialog/cycle messages;
- working memory;
- active plan и revisions;
- tool results и result refs;
- artifact refs/activations;
- already admitted/applied additions;
- durable snapshot context/trace evidence;
- resumability metadata.

Durable semantic intermediate emissions как отдельная IR-6 lifecycle ещё не
реализованы; IR-5 не добавляет их ради pause.

Он не:

- удаляет сообщения;
- откатывает подтверждённые side effects;
- сбрасывает session;
- создаёт новый cycle;
- помечает current result успешным;
- очищает queued additions.

### State flow

```text
running
→ pause command queued/acknowledged
→ pause_requested
→ current atomic block completes
→ snapshot persisted at safe checkpoint
→ paused_by_user
```

Если cycle уже:

- `paused_by_user` — вернуть idempotent semantic `already_paused`;
- `pause_requested` — вернуть `pause_pending`;
- `waiting_user` — перевести в `paused_by_user`, сохранив waiting question и
  context; pause wins;
- `interrupted` — зафиксировать user pause поверх resumable snapshot;
- terminal — вернуть `no_active_cycle`;
- `idle` — вернуть `no_active_cycle`.

Duplicate transport delivery с тем же stable idempotency key возвращает тот же
durable command/control ID/sequence; semantic already-paused/pause-pending — это
отдельные deterministic outcomes для независимых commands.

## Atomic stop boundary

IR-5 не пытается прервать произвольный Python await или внешний side effect.

### Во время LLM request

```text
record pause
→ дождаться bounded LLM attempt outcome
→ control checkpoint
→ не начинать следующий tool/LLM block
→ pause
```

Production runner наблюдает control после bounded main LLM attempt и до первого
tool/следующего LLM semantic block.

### Во время tool call

```text
record pause
→ текущий tool call завершается
→ complete remaining calls of same assistant tool block
→ persist all matching role=tool results
→ CP-AFTER-TOOL-BLOCK
→ pause
```

Почему завершается весь block: сохранение OpenAI-compatible sequence и отсутствие
частично исполненного assistant message важнее минимальной latency остановки.
Deterministic barrier test удерживает выполнение внутри multi-tool block,
принимает pause, затем проверяет полный `assistant → tool → tool` history и
отсутствие следующего LLM.

### Во время compaction/final processing

Операция завершается либо даёт controlled failure. Перед следующим semantic или
terminal шагом checkpoint получает возможность применить control. IR-5 не
force-cancel эти операции.

## `/continue` semantics

```text
paused_by_user
→ durable continue command
→ reacquire same-cycle in-process lease if previous runner unwound
→ CP-RESUME
→ apply queued additions in FIFO up to continue acceptance target
→ running same cycle
```

Все additions, отправленные во время паузы, сохраняются, но сами не запускают
runner.

### Continue without additions

Разрешён. AgentCycle продолжает с того же durable snapshot/checkpoint. Новый
`input_batch_update`, новый user reply и новая context revision только ради
continue не создаются.

### Continue states

| State | Outcome |
|---|---|
| `paused_by_user` | resume same cycle |
| `pause_requested` | pending pause может быть neutralized reducer-ом; same cycle continues |
| `waiting_user` | reject `still_waiting_for_input`, если ответа нет |
| `interrupted` resumable | controlled same-cycle resume в доступном current-process state |
| `running` | `already_running` |
| terminal/idle | `nothing_to_continue` |

Новый ordinary input в `waiting_user` остаётся основным способом resume. Команда
`/continue` не подменяет отсутствующий пользовательский ответ. Process-restart
runner reconstruction для interrupted/paused cycle остаётся IR-8.

## Additions во время паузы

Admission kind `queue_paused`:

```text
CommittedInputBatch
→ admission + inbox queued
→ accepted watermark advanced
→ user acknowledgement
→ no runner wakeup
```

Несколько batches сохраняют FIFO order. Continue command durable metadata
фиксирует accepted input target на coordination boundary. `CP-RESUME` применяет
все additions до этого target в bounded chunks по существующим
`max_batches_per_checkpoint`/`max_batch_bytes_per_checkpoint`; между chunks нет
LLM request.

Additions, admitted после `/continue` boundary, не затягиваются в initial resume
drain и обрабатываются обычным running checkpoint.

## `/reset` semantics

`/reset` — destructive session operation и имеет высший приоритет.

```text
durable reset command
→ advance durable SessionInputRuntimeState.generation exactly once
→ fence old runner/work
→ cancel old-generation admissions/inbox/pending controls
→ cancel old active snapshot and dormant finalization/emission records
→ synchronize in-process coordinator to durable generation
→ wait old execution lease boundary before shared mutable memory clear
→ cancel open drafts/collections
→ preserve immutable audit evidence
→ session idle in new generation
```

`/reset` не обязан физически удалять immutable content/artifact/audit files
немедленно. Old-generation records получают explicit cancellation/fencing
semantics и не становятся authority новой generation.

Durable generation является source of truth. Coordinator после durable
transition лишь инвалидирует локальную очередь/lease cache и не может сам сделать
process-local generation canonical.

Same reset delivery с тем же stable idempotency key не повышает generation
повторно. Если durable generation уже продвинута, а old-generation cleanup упал,
retry того же reset продолжает reconciliation старых records без второго
semantic reset effect.

## Command idempotency

Client adapter формирует stable idempotency identity из фактической source command
identity:

```text
client_type
+ client instance/bot instance
+ conversation/thread
+ source command message/update
+ resolved session
+ command
```

Повторная доставка одной команды возвращает прежний durable command/outcome.
Один key не может молча изменить command/source relation.

Для Web/first-party API рекомендуется explicit `Idempotency-Key`.

## Command publication и watermarks

Control sequence выделяется под короткой session coordination boundary — не через
unsafe `read max → await → max+1`.

Filesystem IR-5 publication:

```text
allocate monotonic sequence
→ persist SessionControlCommand
→ persist/index identity
→ advance SessionInputRuntimeState.pending_control_sequence
→ semantic acknowledgement/application
```

Если record уже durable, а pending-watermark write упал, retry того же key
находит тот же record/sequence и repair-ит missing watermark. Control application
сначала сохраняет state/snapshot effect, затем marking; retry не дублирует
pause/resume/reset effect.

`applied_control_sequence <= pending_control_sequence`. Applied watermark
продвигается только через contiguous terminal control records (`applied`,
`rejected`, `cancelled`) и не перепрыгивает head gap.

## Command acknowledgement и application

Разделяются два момента:

```text
acknowledged
→ команда durable принята runtime

applied
→ active cycle достиг safe checkpoint и state изменено
```

Для running `/stop` acknowledgement соответствует `pause_requested`, а application
фиксирует `paused_by_user`. Waiting/interrupted pause может быть применён сразу на
уже safe resumable snapshot.

## Checkpoint reducer

Каждый checkpoint имеет deterministic control boundary:

```text
checkpoint entry
→ capture pending control watermark at entry
→ persist closed protocol-valid atomic block context
→ reduce/process controls through captured watermark
→ decide whether ordinary input may apply
→ only then enter next atomic semantic block
```

Control, accepted после checkpoint entry, не меняет уже начатый atomic block.
Priority effective semantics:

```text
reset > pause > continue > ordinary input
```

Audit sequence не теряется. Rapid `pause → continue` может дать effective
`running` без промежуточного user-visible `paused_by_user`, если pause ещё не был
применён; обе durable commands остаются в audit и получают terminal outcomes.

## Control priority и races

### Stop vs input

Оба durable сохраняются. Pause применяется первым; additions остаются queued и
не отменяют explicit user pause.

### Continue vs new input

Input admitted до coordination snapshot `/continue` применяется в initial resume
drain. Поздний input остаётся running checkpoint.

### Stop vs finalization

Если pause уже виден control checkpoint на `CP-BEFORE-TERMINAL-COMMIT`, stale
terminal candidate подавляется и cycle pauses. Это checkpoint-level IR-5
suppression, а не полный IR-7 terminal barrier.

### Reset vs finalization

Если reset принят до заблокированного/выполняемого
`CP-BEFORE-TERMINAL-COMMIT`, checkpoint видит generation/cycle fencing, old
candidate не становится current successful terminal state, old snapshot
cancelled. Окно после последнего checkpoint и до terminal persistence остаётся
IR-7.

### Stop and continue arrive rapidly

Records сохраняются по sequence. Effective reducer учитывает generation и current
state. Если `continue` следует за ещё не применённым `pause`, итогом может быть
`running` без observable paused state, но обе команды остаются в audit.

## API contract

Service-neutral surface реализован через application-layer control service:

```python
request_pause(...)
request_continue(...)
request_reset(...)
```

Каждая команда принимает `session_id`, stable `idempotency_key`,
`source_client_type`, safe `source_message_ref` и optional reason metadata и
возвращает structured `ControlOutcome` с durable `SessionControlCommand`.

Adapters не читают `MCPClient.session_states` напрямую для принятия semantic
решения.

## Telegram integration

- `/stop` и `/continue` зарегистрированы отдельным high-priority command handler
  group;
- handler не включает `/collect`, `/send`, `/cancel`; `/cancel` остаётся ingress
  collection control;
- exact session/thread resolution используется тот же, что ordinary input;
- source metadata включает bot/chat/thread/source message/update identity;
- команда forward-ится через существующий Gateway/application boundary, где
  вызывается общий control service;
- localizable projection выбирается вне domain/application semantic service;
- deterministic tests проверяют handler priority, exact session/source identity и
  stable message-specific idempotency.

Maintainer live Telegram acceptance остаётся IR-10 и не заявляется как IR-5
evidence.

## Web/CLI preparation

Web/CLI используют тот же transport-neutral application contract. Полноценные
новые UI/handlers в IR-5 не обязательны и не реализовывались.

Собственный Web chat branch editing не входит в этот update.

## Acceptance

IR-5 deterministic acceptance подтверждает:

- `/stop` не удаляет и не переписывает cycle messages/context identity;
- текущий complete tool block остаётся protocol-valid;
- после applied pause не начинается новый LLM/tool block;
- additions during pause admitted FIFO without auto-resume/wake;
- `/continue` resumes same cycle and applies pre-continue queued additions;
- continue without additions не создаёт fake semantic revision;
- duplicate `/stop`/`/continue`/`/reset` сохраняют logical identity;
- `/continue` не заменяет missing answer в `waiting_user`;
- `/reset` durable generation fencing blocks old cycle authority;
- partial reset cleanup repair не повышает generation второй раз;
- pause/reset visible at terminal checkpoint suppress stale candidate;
- compatibility AgentResult mapping не перезаписывает pause/reset durable state;
- Telegram runtime handlers не ломают ownership `/collect|/send|/cancel`.

Deferred acceptance:

- IR-6 durable semantic `AgentEmission`;
- IR-7 final atomic accepted/control-vs-terminal persistence barrier;
- IR-8 startup reconstruction/reconciliation;
- IR-9 complete diagnostics/client projections;
- IR-10 randomized/restart/synthetic/live roast;
- Telegram history rewind.
