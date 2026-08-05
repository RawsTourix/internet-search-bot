---
id: design.v0.4.input-runtime.emissions
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: planned
last_reviewed: 2026-08-05
---

# Agent emissions и client projections

## Назначение

Обновление вводит устойчивое промежуточное общение агента с пользователем, не
смешивая его с transient progress и terminal final response.

## Четыре вида исходящего взаимодействия

### `ProgressEvent`

Короткое состояние выполнения:

```text
Ищу данные…
Читаю файлы…
Проверяю результат…
```

Свойства:

- canonical structured event + localized bounded projection;
- может coalesce/edit/throttle;
- не обязан становиться отдельной dialog message;
- не требует ответа пользователя;
- не меняет context revision.

### Intermediate `AgentEmission`

Смысловая промежуточная реплика:

```text
Нашёл противоречие в правилах восстановления. Проверяю finalization race.
```

Свойства:

- durable;
- имеет `emission_id`;
- относится к exact cycle/context revision;
- доставляется независимо от final answer;
- может быть reply target;
- не terminalizes cycle;
- не переводит cycle в `waiting_user`.

### Question

Смысловая реплика, после которой runtime фиксирует `waiting_user` только при
успешном final input recheck.

Question может использовать общий emission/outbox transport, но имеет отдельный
cycle transition.

### Final response

Создаётся только finalization pipeline и остаётся `OutputBatch`. Intermediate
emission никогда не заменяет final response.

## Manager tool `send_user_message`

На переходном этапе наиболее безопасный interface — builtin manager tool:

```json
{
  "message": "Нашёл потенциальную проблему. Продолжаю проверку.",
  "kind": "intermediate",
  "importance": "normal"
}
```

Tool contract:

```text
validate arguments/policy
→ persist AgentEmission READY
→ enqueue/trigger delivery
→ return emission_id + persistence state
→ continue AgentCycle
```

Handler не вызывает Telegram/Web API напрямую и не ждёт обязательной успешной
client delivery для продолжения agent loop.

## Когда агенту разрешено писать intermediate message

Разрешённые причины:

- найден значимый частичный результат;
- обнаружен риск/противоречие, влияющее на дальнейшую работу;
- длительная задача перешла в новый важный этап;
- агент явно сообщает об изменении подхода;
- пользователь просил периодические содержательные updates.

Не разрешается использовать emission как шумный лог каждой iteration/tool call.

Runtime policy:

```text
max_intermediate_messages_per_cycle
min_intermediate_message_interval_seconds
max_intermediate_message_chars
```

Policy может отклонить/свернуть лишнее сообщение с structured result для модели.

## Persistence и delivery

`AgentEmissionRepository` хранит semantic message intent. Delivery использует
существующие interaction/outbox patterns либо отдельный emission outbox adapter.

```text
AgentEmission READY
→ delivery claim
→ client renderer/sink
→ receipt
→ DELIVERED | FAILED | UNKNOWN
```

Execution и delivery раздельны:

- failure доставки intermediate message не делает cycle failed;
- final result не отменяется из-за старого failed emission;
- duplicate delivery signal deduplicates по `emission_id`/attempt;
- unknown delivery не повторяется blind, если клиент мог принять сообщение;
- restart сохраняет emission intent и reconciliation metadata.

## Response route

Emission получает route snapshot, разрешённый admission/runtime context:

```text
client_type
client_instance_id
conversation_id
thread_id
reply/reference metadata
capability_snapshot_id
```

LLM не выбирает raw route. Она передаёт только semantic message/kind.

Если active cycle был запущен из нескольких additions с разными response anchors,
общая policy выбирает current run presentation/delivery route. В `v0.4` один
cycle остаётся привязанным к одной session/client interaction context.

## Reply binding

Client может ответить на конкретную emission. Adapter сохраняет внешний reply ref
в `ClientInputEnvelope`/metadata и, если binding доступен, внутренний
`reply_to_emission_id`.

Это relation/provenance, а не автоматическое создание отдельной branch.

В LLM projection addition может содержать safe marker:

```json
{
  "reply_to": {
    "emission_id": "emit_...",
    "kind": "intermediate"
  }
}
```

Если binding недоступен, input остаётся обычным ordered addition.

## Addendum projections

Input Runtime публикует canonical события:

```text
input_addendum_admitted
input_addendum_applying
input_addendum_applied
input_addendum_cancelled
input_addendum_failed
```

Structured payload минимум:

```text
admission_id
input_batch_id
cycle_id
cycle_sequence
runtime_status
file_count
text_part_count
```

Не включаются raw text/file content.

### Telegram projection

Пример admitted:

```text
Дополнение №2 принято и будет учтено в текущей задаче.
```

Paused:

```text
Дополнение №3 принято. Задача остаётся приостановленной.
```

Applied:

```text
Дополнение №2 учтено.
```

Telegram может:

- редактировать admission acknowledgement при apply;
- отправить отдельный completion message;
- coalesce несколько additions в один summary;
- не показывать applied event, если это создаёт шум.

Выбор принадлежит capability/client policy, structured lifecycle сохраняется.

### Web projection

Web может показывать timeline и replayable event sequence. Durable run API
полностью относится к v0.6, но v0.4 events уже имеют stable IDs/sequence.

### CLI projection

TTY может показывать lines/spinner, non-TTY — JSONL. Это не меняет domain model.

## Intermediate message и LLM history

После успешного persistence emission runtime добавляет в cycle history
assistant-visible semantic evidence только один раз.

Возможные стратегии:

1. native assistant message уже существует как tool-call message; после tool
   result отдельный runtime marker фиксирует delivered emission;
2. emission content остаётся частью manager tool call/result и не дублируется
   отдельным assistant message.

Для initial implementation предпочтительно не вставлять второй assistant message
самостоятельно. Tool call и result уже доказывают, что агент отправил сообщение.
Context projection может compact это позже, сохраняя emission ref.

## Questions

Question lifecycle:

```text
model candidate ask_user
→ CP-BEFORE-WAITING input recheck
→ persist question emission/output intent
→ transition waiting_user
→ deliver
```

Если delivery failed, cycle остаётся waiting/resumable, а diagnostics показывают
failure. Policy может разрешить retry based on client semantics.

Если input already accepted, candidate question не persist/deliver как current
question; он остаётся trace evidence suppressed by input.

## Final response relation

Final OutputBatch сохраняет:

```text
cycle_id
context_revision_id
consumed_through_sequence
related emission IDs optional
```

Delivery adapter закрывает run presentation только после final result terminal
commit. Late intermediate emission после terminal commit запрещена.

## Progress compatibility

Текущий `ProgressEvent(type="agent_message")` должен быть инвентаризирован.
Если он фактически используется как transient UI hint, остаётся progress. Если
он предназначен как смысловое durable сообщение, caller мигрирует на
`send_user_message`.

Нельзя автоматически превращать каждый `agent_request`/progress text в
AgentEmission.

## Localization

Domain emission text, явно сформированный LLM для пользователя, не переводится
transport adapter автоматически. Runtime notices и addendum lifecycle templates
локализуются через общий catalog.

Structured kind/state не зависит от языка.

## Security и safety

- emission arguments проходят length/sanitization policy;
- raw secrets/tool payload не отправляются автоматически;
- internal/debug emission не попадает пользователю;
- model не задаёт arbitrary callback URL/chat ID;
- route authority берётся из trusted runtime state;
- markdown/HTML escaping выполняет client renderer;
- duplicate tool call с тем же idempotency context не создаёт вторую реплику.

## Current-code integration

- добавить manager tool spec/handler рядом с current manager tools;
- handler делегирует `AgentEmissionService`;
- `Api` composition создаёт repository/service и связывает interaction layer;
- Telegram transport получает endpoint/callback для claim/delivery либо
  переиспользует общий output gateway;
- `ProgressEvent` protocol расширяется addendum lifecycle event types при
  необходимости;
- `/status` показывает READY/FAILED/UNKNOWN emission counts без raw text.

Полное выделение `RuntimeEventSink`/Notification service остаётся
`v0.4-runtime-modularization`/`v0.6`.

## Acceptance

- intermediate message сохраняется и cycle продолжает работу;
- emission не переводит cycle в `waiting_user`;
- duplicate manager call не создаёт duplicate client message;
- delivery failure не уничтожает AgentCycle;
- reply binding сохраняет internal emission relation, если доступен;
- ordinary progress не становится dialog message;
- question не доставляется, если accepted input suppresses waiting transition;
- no late intermediate emission after terminal commit;
- Telegram/Web projections строятся из одной canonical lifecycle model.
