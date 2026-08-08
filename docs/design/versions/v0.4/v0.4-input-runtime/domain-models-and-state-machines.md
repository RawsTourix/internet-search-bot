---
id: design.v0.4.input-runtime.models
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-05
---

# Domain models и state machines

## Статус реализации

IR-1 foundation реализован: добавлены Pydantic v2 domain records, устойчивые
`str`-enums состояний и checkpoints, configuration model/loader и
command-oriented repository ports. Контракты подтверждены HEAD
`83f544669e5b658884bc717f7256ec22c062b264`, workflow #21 и #471 и targeted
suite `83 passed`.

Это evidence только для моделей, configuration и repository interfaces.
Описанные ниже runtime transitions, admission, filesystem persistence,
checkpoints, controls и finalization ещё не подключены к production runtime.

## Назначение

Документ фиксирует устойчивые domain IDs, authoritative records, watermarks и
переходы состояний. Имена Python-классов могут незначительно меняться при
реализации, но ownership, identity, transitions и idempotency relations являются
частью контракта.

## Общие правила identity

Все IDs создаются runtime и не зависят от идентификаторов клиента:

```text
adm_*       input admission
inbx_*      cycle inbox item
ctl_*       control command
ctxrev_*    semantic context revision
emit_*      durable agent emission
fin_*       finalization attempt
```

Существующие IDs сохраняются:

```text
session_id
cycle_id
input_batch_id
artifact_id
output_batch_id
```

Telegram/Web/CLI identifiers хранятся в response routes/client bindings и не
становятся domain primary key.

Время хранится в UTC. Для short in-process timing допускается monotonic clock,
но durable records используют wall-clock timestamp.

## `SessionInputRuntimeState`

Одна authoritative запись на session:

```python
class SessionInputRuntimeState(BaseModel):
    session_id: str
    generation: int

    active_cycle_id: str | None
    cycle_status: Literal[
        "idle",
        "running",
        "waiting_user",
        "pause_requested",
        "paused_by_user",
        "interrupted",
        "finalizing",
        "done",
        "error",
        "cancelled",
    ]

    accepted_through_session_sequence: int
    active_cycle_accepted_through_sequence: int
    active_cycle_applied_through_sequence: int

    pending_control_sequence: int
    applied_control_sequence: int

    active_context_revision_id: str | None
    finalization_id: str | None

    revision: int
    created_at: datetime
    updated_at: datetime
```

### Инварианты

- `generation` увеличивается при `/reset` и fencing старой работы;
- один `active_cycle_id` на session;
- `active_cycle_applied_through_sequence <= active_cycle_accepted_through_sequence`;
- terminal commit разрешён только при равенстве input/control watermarks;
- `revision` используется для optimistic compare-and-swap filesystem update;
- `idle` не имеет active cycle, кроме краткого recovery reconciliation window;
- `paused_by_user` сохраняется после restart;
- `waiting_user` и `paused_by_user` не являются взаимозаменяемыми.

## `InputAdmissionRecord`

Один record связывает immutable committed batch с runtime decision:

```python
class InputAdmissionRecord(BaseModel):
    admission_id: str
    session_id: str
    input_batch_id: str

    session_sequence: int
    target_cycle_id: str
    cycle_sequence: int
    admitted_generation: int

    admission_kind: Literal[
        "start_cycle",
        "continue_running",
        "resume_waiting",
        "queue_paused",
        "resume_interrupted",
    ]

    state: Literal[
        "admitted",
        "applied",
        "cancelled",
        "failed_terminal",
    ]

    idempotency_key: str
    admitted_at: datetime
    applied_at: datetime | None
    cancelled_at: datetime | None
    failure_code: str | None
```

### Уникальность

- unique `input_batch_id`;
- unique `(session_id, session_sequence)`;
- unique `(target_cycle_id, cycle_sequence)`;
- initial batch получает `cycle_sequence=0`;
- admitted decision immutable: record не перепривязывается молча к другому cycle;
- recovery может завершить или отменить прежнее решение, но создаёт trace reason.

`state=failed_terminal` допускается только для повреждённого/недоступного
committed batch или необратимого consistency defect. Временная store ошибка не
переводит record в terminal state: операция retry/recovery остаётся идемпотентной.

## `CycleInboxItem`

Рабочая очередь для admitted additions:

```python
class CycleInboxItem(BaseModel):
    inbox_item_id: str
    admission_id: str
    session_id: str
    cycle_id: str
    input_batch_id: str
    cycle_sequence: int
    generation: int

    state: Literal[
        "queued",
        "claimed",
        "applying",
        "applied",
        "cancelled",
        "failed_terminal",
    ]

    claim_token: str | None
    claim_expires_at: datetime | None
    attempt_count: int
    last_error_code: str | None

    enqueued_at: datetime
    claimed_at: datetime | None
    applied_at: datetime | None
    cancelled_at: datetime | None
```

### Переходы

```text
queued → claimed → applying → applied
claimed → queued             lease expired before durable apply
applying → applied           replay state proves application completed
queued/claimed → cancelled   reset/generation invalidation
queued/claimed/applying → failed_terminal only on verified permanent defect
```

`applying` нужен для crash diagnostics. Replay authority определяется не только
состоянием item, а applied watermark и `applied_input_batch_ids` в active-cycle
snapshot.

### Claim semantics

- claim выдаётся только head FIFO range;
- один claim может включать несколько последовательных items;
- claimed range bounded configuration limits;
- claim token непрозрачный и проверяется при state transition;
- expired `claimed` возвращается в `queued`;
- expired `applying` проходит reconciliation с active-cycle snapshot до requeue;
- duplicate/replayed queue signal не создаёт второй item.

## `SessionControlCommand`

```python
class SessionControlCommand(BaseModel):
    control_id: str
    session_id: str
    target_cycle_id: str | None
    generation: int
    sequence_number: int

    command: Literal[
        "pause",
        "continue",
        "reset",
    ]

    state: Literal[
        "queued",
        "acknowledged",
        "applied",
        "rejected",
        "cancelled",
    ]

    idempotency_key: str
    source_client_type: str
    source_message_ref: dict | None
    reason: str | None

    created_at: datetime
    acknowledged_at: datetime | None
    applied_at: datetime | None
    rejection_code: str | None
```

Приоритет применения:

```text
reset > pause > continue > ordinary input
```

Порядок records сохраняется для аудита, но effective decision может coalesce:

- повторный `pause` не создаёт вторую остановку;
- несколько `continue` после одного pause дают один resume;
- `reset` supersedes pending pause/continue старой generation;
- `continue` до достигнутого `paused_by_user` снимает pause request только если
  policy явно допускает это; default — acknowledged как отмена ещё не применённой
  паузы, без запуска второго cycle.

## `ActiveCycleSnapshot`

Расширяет текущий `ActiveAgentCycle`, но repository record не обязан сериализовать
все Python-only cache objects напрямую.

```python
class ActiveCycleSnapshot(BaseModel):
    cycle_id: str
    session_id: str
    generation: int
    status: str

    original_input_batch_id: str
    original_user_request: str

    messages_for_llm: list[dict]
    cycle_trace: list[dict]
    working_memory_ref: str | None

    applied_input_batch_ids: list[str]
    applied_through_cycle_sequence: int
    active_context_revision_id: str

    waiting_question: str | None
    pause_reason: str | None
    interruption_reason: str | None

    active_plan_id: str | None
    active_plan_revision: int | None
    active_plan_node_id: str | None

    artifact_refs: list[str]
    read_artifact_refs: list[str]
    result_refs: list[str]

    config_revision: str | None
    snapshot_revision: int
    safe_checkpoint: str
    created_at: datetime
    updated_at: datetime
```

### Replay state

`applied_input_batch_ids` является bounded diagnostic/idempotency set для active
cycle. Основной order authority — `applied_through_cycle_sequence` плюс admission
records.

При большом количестве additions допускается bounded representation:

```text
applied_through_cycle_sequence
+ recent applied_input_batch_ids
+ exact admission records in repository
```

Нельзя полагаться только на текст `input_batch_update` в LLM history.

## `CycleContextRevision`

Semantic revision отражает изменение задачи/контекста из-за input или resume, а
не каждую физическую перестройку prompt после compaction.

```python
class CycleContextRevision(BaseModel):
    context_revision_id: str
    cycle_id: str
    session_id: str
    revision_number: int

    parent_revision_ids: list[str]
    reason: Literal[
        "initial_input",
        "input_applied",
        "resumed",
        "recovered",
    ]

    applied_input_batch_ids: list[str]
    applied_through_cycle_sequence: int
    added_artifact_refs: list[str]
    constraint_summary: str | None

    created_at: datetime
```

В `v0.4` обязательны:

```text
revision_number: 1,2,3...
parent_revision_ids: [] для initial, [previous_revision_id] далее
```

Multiple parents schema-ready, но создание merge revision запрещено до v0.6.

## `AgentEmission`

```python
class AgentEmission(BaseModel):
    emission_id: str
    session_id: str
    cycle_id: str
    context_revision_id: str

    kind: Literal[
        "intermediate",
        "runtime_notice",
        "question",
    ]

    text: str
    visibility: Literal["user", "debug", "internal"]
    importance: Literal["normal", "high"]

    response_route: dict
    state: Literal[
        "ready",
        "delivering",
        "delivered",
        "failed",
        "unknown",
        "cancelled",
    ]

    idempotency_key: str
    created_at: datetime
    delivered_at: datetime | None
    error_code: str | None
```

`question` может использовать общий emission/outbox, но terminal state
`waiting_user` фиксируется отдельно. `intermediate` не меняет cycle status.

Progress events не создают `AgentEmission` автоматически. Только явно выбранное
runtime/manager действие создаёт durable message.

## `CycleFinalizationRecord`

```python
class CycleFinalizationRecord(BaseModel):
    finalization_id: str
    session_id: str
    cycle_id: str
    generation: int
    context_revision_id: str

    expected_accepted_sequence: int
    expected_applied_sequence: int
    expected_control_sequence: int

    state: Literal[
        "prepared",
        "aborted_new_input",
        "aborted_control",
        "result_persisted",
        "output_ready",
        "terminal_committed",
        "failed_recoverable",
        "failed_terminal",
    ]

    result_ref: str | None
    output_batch_id: str | None
    failure_code: str | None

    created_at: datetime
    updated_at: datetime
```

Record закрывает crash window между final LLM candidate, durable result,
`OutputBatch` и terminal cycle state.

## Aggregate state transitions

### Новый cycle

```text
session idle
→ admission(start_cycle, sequence=0)
→ snapshot running/context R1
→ execution lease
```

### Addition во время работы

```text
CommittedInputBatch
→ admission(continue_running)
→ inbox queued
→ accepted watermark advanced
→ safe checkpoint claim/apply
→ context Rn+1
→ applied watermark advanced
```

### Pause

```text
control pause queued
→ pause_requested
→ current atomic block complete
→ safe snapshot
→ paused_by_user
```

### Continue

```text
control continue
→ claim queued additions
→ apply/context revision if needed
→ running same cycle
```

### Waiting user

```text
agent question
→ final inbox recheck
→ no input: waiting_user
→ new batch admission(resume_waiting)
→ resume same cycle
```

### Finalization

```text
final candidate
→ prepared record
→ short coordination recheck
→ mismatch: abort and continue/pause
→ match: result persisted → output ready → terminal committed
```

## Persistence ownership

Каждый record имеет одного owner repository. Cross-record consistency достигается
короткой coordination service и recovery reconciliation, а не ad-hoc записью
нескольких JSON files из agent loop.

В `v0.5` те же entities могут стать таблицами или объединёнными transactional
aggregates, но их IDs и semantic transitions сохраняются.
