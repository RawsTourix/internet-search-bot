---
id: design.v0.4.input-runtime.control-plane
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: planned
last_reviewed: 2026-08-05
---

# Control plane: `/stop`, `/continue`, `/reset`

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

Команды должны быть доступны через общий API, а Telegram/Web/CLI adapters лишь
создают idempotent control request.

## `/stop` semantics

`/stop` сохраняет всё authoritative состояние задачи:

- dialog/cycle messages;
- working memory;
- active plan и revisions;
- tool results и result refs;
- artifact refs/activations;
- already admitted/applied additions;
- intermediate agent emissions;
- trace/progress evidence;
- resumability metadata.

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

- `paused_by_user` — вернуть idempotent `already_paused`;
- `pause_requested` — вернуть `pause_pending`;
- `waiting_user` — перевести в `paused_by_user`, сохранив waiting question как
  historical context, либо сохранить composite reason; default: pause wins;
- `interrupted` — зафиксировать user pause поверх resumable snapshot;
- terminal — вернуть `no_active_cycle`;
- `idle` — вернуть `no_active_cycle`.

## Atomic stop boundary

Initial implementation не пытается безопасно прервать произвольный Python await
или внешний side effect.

### Во время LLM request

Default:

```text
record pause
→ дождаться bounded LLM attempt outcome
→ не начинать следующий tool/LLM block
→ pause checkpoint
```

Optional transport cancellation допустима только если provider adapter гарантирует
controlled cancellation и не меняет semantic outcome. Это не обязательный gate.

### Во время tool call

```text
record pause
→ текущий tool call завершается
→ complete remaining calls of same assistant tool block
→ persist all role=tool results
→ pause
```

Почему завершается весь block: сохранение OpenAI-compatible sequence и отсутствие
частично исполненного assistant message важнее минимальной latency остановки.

В будущем dispatcher с side-effect classes сможет останавливать между calls и
создавать synthetic cancelled results. Это не входит в первый scope.

### Во время compaction/final processing

Операция завершается либо даёт controlled failure. Перед terminal commit pause
имеет приоритет и aborts finalization.

## `/continue` semantics

```text
paused_by_user
→ continue command applied
→ CP-RESUME
→ apply queued additions in FIFO
→ new context revision if needed
→ running same cycle
```

Все additions, отправленные во время паузы, сохраняются, но сами не запускают
runner.

### Continue without additions

Разрешён. AgentCycle продолжает со snapshot/checkpoint, где был остановлен.

### Continue states

| State | Outcome |
|---|---|
| `paused_by_user` | resume same cycle |
| `pause_requested` | cancel pending pause only if atomic block ещё идёт; затем continue |
| `waiting_user` | reject `still_waiting_for_input`, если ответа нет |
| `interrupted` resumable | controlled resume same cycle |
| `running` | `already_running` |
| terminal/idle | `nothing_to_continue` |

Новый ordinary input в `waiting_user` остаётся основным способом resume. Команда
`/continue` не подменяет отсутствующий пользовательский ответ.

## Additions во время паузы

Admission kind `queue_paused`:

```text
CommittedInputBatch
→ admission + inbox queued
→ accepted watermark advanced
→ user acknowledgement
→ no runner wakeup
```

Пользовательская projection:

```text
Дополнение №N принято. Задача остаётся приостановленной.
Для продолжения отправьте /continue.
```

Несколько batches сохраняют порядок. `/continue` применяет bounded range; если
очередь превышает checkpoint limit, cycle может сделать несколько resume
checkpoints до первого LLM request, пока required initial drain policy не
выполнена. Рекомендуемый default: перед первым post-resume LLM request применить
все additions, уже accepted до `/continue`, в bounded chunks без промежуточного
LLM вызова.

Additions, пришедшие после `/continue`, обрабатываются обычными running
checkpoints.

## `/reset` semantics

`/reset` — destructive session operation и имеет высший приоритет.

```text
reset command
→ advance session generation
→ reject old runner ownership
→ cancel queued inbox/control of old generation
→ wait current atomic block/safe lease boundary
→ terminalize active cycle as cancelled/reset
→ clear session memory/runtime handoff
→ cancel open drafts/collections
→ preserve immutable audit records by retention policy
→ session idle in new generation
```

`/reset` не обязан физически удалять immutable content/artifact/audit files
немедленно. Он удаляет их из active session access/projection; cleanup выполняется
отдельной retention policy.

Current `reset_runtime_session()` уже повышает execution generation, отменяет open
drafts и ждёт run lease. Новый runtime должен перенести ownership на durable
control/finalization services, сохранив observable behavior.

## Command idempotency

Client adapter формирует stable idempotency key:

```text
client_type + client_instance_id + conversation/thread + source command message
```

Повторная доставка одной команды возвращает прежний outcome.

Для Web/first-party API рекомендуется explicit `Idempotency-Key`.

## Command acknowledgement и application

Разделяются два момента:

```text
acknowledged
→ команда durable принята runtime

applied
→ active cycle достиг safe checkpoint и state изменено
```

Для `/stop` пользователь может получить:

```text
Останавливаю задачу после текущего безопасного шага…
```

После application:

```text
Задача приостановлена.
```

Если transport не поддерживает edit, это могут быть два сообщения либо одно
terminal acknowledgement по capability policy.

## Control priority и races

### Stop vs input

Оба durable сохраняются. Pause применяется первым; additions остаются queued.

### Continue vs new input

Input admitted до coordination snapshot `/continue` применяется в initial resume
drain. Поздний input попадёт в running checkpoint.

### Stop vs finalization

Pause command, accepted до terminal commit, aborts finalization.

### Reset vs finalization

Reset generation invalidates finalization record и запрещает delivery old result.
Persisted result/output may remain diagnostic/cancelled, но не доставляется как
успешный terminal response.

### Stop and continue arrive rapidly

Records сохраняются по sequence. Effective reducer учитывает generation и current
state. Если `continue` следует за ещё не применённым `pause`, итогом может быть
`running` без observable paused state, но обе команды остаются в audit.

## API contract

Service-neutral commands:

```python
request_pause(session_id, idempotency_key, source_ref) -> ControlOutcome
request_continue(session_id, idempotency_key, source_ref) -> ControlOutcome
request_reset(session_id, idempotency_key, source_ref) -> ControlOutcome
```

```python
class ControlOutcome(BaseModel):
    control_id: str
    command: str
    state: Literal["acknowledged", "applied", "rejected"]
    runtime_status: str
    target_cycle_id: str | None
    reason_code: str
```

Adapters не читают `MCPClient.session_states` напрямую для принятия решения.

## Telegram integration

- `/stop` и `/continue` получают отдельные high-priority command handlers;
- handlers не смешиваются с `batch_commands.py` `/collect|/send|/cancel`;
- exact session/thread resolution используется тот же, что ordinary input;
- handler прекращает lower-priority processing через transport mechanism;
- messages локализованы и capability-aware;
- live tests включают command во время LLM, tool block, finalization и pause.

## Web/CLI preparation

Web/CLI используют те же control endpoints. UI может показывать кнопку
Stop/Continue, но domain command остаётся тем же.

Собственный Web chat branch editing не входит в этот update.

## Acceptance

- `/stop` не удаляет и не переписывает cycle messages;
- текущий complete tool block остаётся protocol-valid;
- после pause не начинается новый LLM/tool block;
- additions during pause admitted without auto-resume;
- `/continue` resumes same cycle and applies queued additions;
- duplicate `/stop`/`/continue` idempotent;
- `/continue` не заменяет missing answer в `waiting_user`;
- `/reset` fencing blocks old cycle/final delivery;
- command accepted before terminal commit wins over stale finalization;
- Telegram command handlers не ломают existing collection commands.
