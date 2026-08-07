---
id: design.v0.4.input-runtime.emissions
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-08-08
---

# Agent emissions и client projections

## Implementation evidence

IR-6 реализован на code/test boundary
`4447d1bfe487bfd764829e701f274655aa8c3c50`.

- `Validate Input Runtime` #297 — success, compile success, `350 passed`,
  `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #609 — success;
- deterministic IR-6 suites используют fake clock, explicit concurrency barriers,
  controlled persistence/cancellation faults, repository recreation и fake
  Telegram/http transport;
- real LLM, MCP, Telegram, Web и internet calls для этих tests не выполняются.

## Назначение

IR-6 вводит устойчивое промежуточное общение агента с пользователем, не смешивая
его с transient progress, waiting question и terminal final response.

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
- не меняет context revision;
- restart не обязан сохранять его как отдельное semantic событие.

Current `AgentAction.agent_request` остаётся transient
`ProgressEvent(type="agent_message")`. IR-6 не делает global migration этого
path и не создаёт `AgentEmission` из обычного progress автоматически.

### Intermediate `AgentEmission`

Смысловая промежуточная реплика:

```text
Нашёл противоречие в правилах восстановления. Проверяю finalization race.
```

Свойства:

- durable;
- имеет stable `emission_id`;
- относится к exact cycle/generation/context revision;
- доставляется независимо от final answer;
- после delivery может быть optional reply target;
- не terminalizes cycle;
- не переводит cycle в `waiting_user`;
- persistence/delivery не создают новую input context revision.

### Question

Question остаётся отдельным waiting transition. IR-6 не превращает
`send_user_message` в `ask_user` и не мигрирует existing waiting lifecycle.
Canonical ordering `candidate → CP-BEFORE-WAITING → recheck → waiting commit`
остаётся связан с future IR-7 finalization/waiting barrier.

### Final response

Final response остаётся `OutputBatch` и finalization pipeline. Intermediate
`AgentEmission` не создаёт fake final `OutputBatch` и не является mini-final.

## Manager tool `send_user_message`

Production builtin schema:

```json
{
  "message": "Нашёл потенциальную проблему. Продолжаю проверку.",
  "kind": "intermediate",
  "importance": "normal"
}
```

Schema содержит только semantic arguments. LLM не может передать:

```text
session_id
cycle_id
generation
context_revision_id
client/chat/conversation/thread IDs
reply target
capability snapshot
emission_id
idempotency key
```

Description разрешает tool только для meaningful partial result,
риска/противоречия, важного нового этапа долгой работы, существенной смены
подхода или явно запрошенных содержательных updates. Он не предназначен для
каждой iteration/tool call, обычного «работаю», debug log, final answer или
`ask_user`.

Production contract:

```text
validate semantic arguments
→ resolve runtime-owned exact execution context
→ resolve trusted route
→ atomically enforce policy + persist READY
→ best-effort delivery wake
→ return structured role=tool result
→ continue AgentCycle
```

Handler не вызывает Telegram/Web API напрямую и не ждёт обязательной client
delivery.

## Runtime-owned execution context

`ManagerToolExecutionContext` получает от runtime:

```text
session_id
cycle_id
generation
context_revision_id
tool_call_id
original_input_batch_id
```

Production path использует scoped active-cycle `ContextVar`, который уже
устанавливается/reset-ится token-wise вокруг exact cycle execution. Native
assistant `tool_call_id` берётся из trace уже выпущенного current tool call.
Concurrent sessions не используют shared mutable `current_session/current_cycle`
authority; deterministic barrier test подтверждает отсутствие context bleed.

`context_revision_id` — именно revision, на котором модель сформировала tool call.
Late input, ожидающий следующего protocol-safe checkpoint, не меняет provenance
уже выданного `send_user_message`.

## Stable idempotency и persistence-before-success

Logical identity строится из:

```text
send_user_message namespace
+ cycle_id
+ generation
+ assistant tool_call_id
```

Она не зависит от text, wall clock, random emission ID или transport attempt.

- same logical replay → same `AgentEmission` и same `emission_id`;
- same key + changed semantic message/importance → managed
  `idempotency_conflict`;
- concurrent same-key calls → один record;
- crash `record durable → index publication missing` repair-ится по exact-cycle
  durable record без второго emission;
- cancellation после durable READY, но до tool result, оставляет intent durable;
  replay возвращает тот же emission.

Manager tool не сообщает success до durable persistence. Wake failure после READY
не меняет accepted result и не удаляет pending intent.

## Policy

Реально подключены:

```text
max_intermediate_messages_per_cycle
min_intermediate_message_interval_seconds
max_intermediate_message_chars
```

String нормализуется безопасно; empty/non-string/over-limit message отклоняется.
Count относится к exact `cycle_id + generation` semantic intermediate intents и
не уменьшается из-за FAILED/UNKNOWN network outcome. Interval использует durable
`created_at` предыдущего intent того же cycle/generation. Tests используют fake
clock, не `sleep()`.

Count/rate acceptance и persistence выполняются одним command-oriented repository
operation под короткой exact-session coordination. Application layer не знает
filesystem locks/layout и естественно переносится на transactional
`SELECT ... FOR UPDATE`/`INSERT ... ON CONFLICT` semantics PostgreSQL v0.5.

## Trusted response route

Route выбирает runtime из authoritative original `CommittedInputBatch`, response
anchor и capability snapshot:

```text
client_type
client_instance_id
conversation_id
thread_id
reply/reference metadata
capability_snapshot_id
```

LLM arguments не участвуют. Snapshot JSON-safe, bounded и не сохраняет
`response_route.metadata`, callback auth, bot/API tokens или иные secrets.
Route mismatch/unavailable даёт controlled `route_unavailable`; arbitrary fallback
conversation не используется.

Ordinary additions не переключают active-cycle delivery route по принципу
«последнее сообщение выигрывает». IR-6 использует canonical original run/session
interaction authority.

## Delivery lifecycle

```text
AgentEmission READY
→ exact worker claim
→ DELIVERING
→ client renderer/sink
→ durable receipt/outcome
→ DELIVERED | FAILED | UNKNOWN
```

Execution lifecycle и delivery lifecycle независимы. READY persistence завершает
manager-tool durable contract; agent loop не ждёт network receipt.

### Claim fencing

- first valid claim: `READY → DELIVERING`, durable attempt count/token/lease;
- retry same claim token после потерянного HTTP response возвращает тот же
  DELIVERING attempt и не создаёт второй client send;
- different token while DELIVERING → conflict;
- worker authority повторно проверяется server-side по exact session, client type
  и client instance;
- route-filtered READY listing bounded.

### Durable receipt

Generic receipt сохраняет:

```text
emission/session/cycle/generation
claim token + attempt number
client type + instance
conversation/thread
external message ID
 delivered_at
```

Receipt persistence предшествует authoritative `DELIVERED` state write. Lost
receipt HTTP response и повтор того же receipt идемпотентны; changed relation
конфликтует. External message ref остаётся durable audit/reply-binding evidence.

### `FAILED` vs `UNKNOWN`

`FAILED` используется только когда известно, что user-visible message не была
доставлена, например deterministic Telegram rejection/preflight failure.

`UNKNOWN` используется, когда transport side effect мог произойти:

- timeout/connection loss после возможной отправки;
- missing reliable receipt;
- expired in-flight claim;
- reset во время active delivery attempt.

Expired `DELIVERING` становится `UNKNOWN`, а не `READY`. UNKNOWN не появляется в
READY outbox и не blind-retry-ится. Full startup reconciliation таких records —
IR-8.

## Pause, continue, reset

Pause не отменяет READY semantic intent, созданный до safe pause. Cooperative
pause завершает current protocol-valid block; paused runner сам не генерирует
новые tool calls до continue.

Same-cycle `/continue` сохраняет durable emission history. Replay того же logical
tool call попадает в stable idempotency identity.

Reset fences old generation:

```text
old READY      → CANCELLED
old DELIVERING → UNKNOWN (reset_during_delivery)
```

DELIVERING нельзя безопасно объявить CANCELLED, потому что client мог уже получить
message. Claim token очищается/fenced; stale old-generation writer не может
позднее записать DELIVERED/FAILED поверх authoritative reset state.

## Sequential terminal fencing и IR-7 boundary

IR-6 реализует sequential cases:

- cycle уже terminal → новый `send_user_message` rejected, READY record не
  создаётся;
- emission READY, затем cycle уже стал terminal до claim → worker не начинает
  новый client send; record становится controlled CANCELLED/superseded.

IR-6 **не объявляет** закрытым concurrent race:

```text
emission claim begins
↔ terminal/finalization commit
```

Если его полная атомарность требует shared finalization record/barrier, это
IR-7. IR-6 sequential terminal fencing != IR-7 atomic terminal/finalization race
closure.

## Telegram delivery

Telegram consumer работает через authenticated internal emission outbox и
separate durable semantic lifecycle.

- semantic intermediate отправляется `send_message`, не transient progress edit;
- `parse_mode=None`, поэтому raw LLM text не интерпретируется как unescaped HTML;
- `conversation_id → chat_id`, `thread_id → message_thread_id`, trusted response
  anchor → optional reply target;
- claim/receipt HTTP retries используют stable attempt identity;
- Telegram network send не повторяется после ambiguous outcome;
- `message_id` successful send сохраняется в durable generic receipt.

Final `OutputBatch` worker остаётся отдельным lifecycle и не переиспользует
`AgentEmission` как final output.

## Safe reply binding

Telegram ingress уже получает `reply_to_message.message_id` server-side из
trusted Update. User/client не передаёт произвольный internal
`reply_to_emission_id`.

После successful delivery external ID сопоставляется с emission только при exact
scope:

```text
session
client type
client instance
conversation
thread
external message ID
```

Совпадение numeric message ID в другой session/chat/thread не создаёт binding.
При успешном match addition projection может содержать:

```json
{
  "reply_to": {
    "emission_id": "emit_...",
    "kind": "intermediate"
  }
}
```

Relation не создаёт branch, не меняет FIFO/admission sequence и не форсит старый
cycle против ordinary admission policy. Если relation нет, input остаётся
обычным.

## Intermediate message и LLM history

IR-6 выбирает native strategy без duplicate assistant text:

```text
assistant tool_call(send_user_message)
→ role=tool agent_emission_result(emission_id)
```

Второй самостоятельный assistant message с тем же text не вставляется. Context
compaction позже может сжать старый tool-call/result block, но stable emission ID
и delivered content остаются recoverable из durable repository/trace.

## Addendum projections

Полный lifecycle/timeline:

```text
input_addendum_admitted
input_addendum_applying
input_addendum_applied
input_addendum_cancelled
input_addendum_failed
```

и полный Telegram/Web/CLI UX **не реализуются IR-6**. Это IR-9. IR-6 добавляет
только минимальную transport-neutral emission outbox/reply projection foundation,
не расширяя `/status` и не создавая новый WebSocket/event-stream framework.

## Questions

IR-6 не меняет existing `WAITING_USER` ownership. Полный question ordering и late
waiting/finalization barrier остаются IR-7. Нельзя создавать второй durable
question поверх legacy path или считать intermediate emission вопросом.

## Localization и rendering

LLM-authored semantic text не переводится transport adapter автоматически.
Runtime notices локализуются через existing catalog. Client renderer отвечает за
safe presentation; Telegram IR-6 использует plain text.

## Security

- manager tool schema — semantic-only и `additionalProperties=false`;
- route/idempotency/provenance authority runtime-owned;
- raw secret/callback metadata не попадает в stored route;
- exact client-instance/session/route fencing выполняется server-side на claim и
  outcome;
- duplicate tool/claim/receipt identities идемпотентны;
- cross-session external reply spoof не bind-ится;
- network awaits не выполняются под filesystem/session coordination lock.

## Current-code integration

Реализовано:

- `AgentEmissionService` и command-oriented repository boundary;
- hardened filesystem delivery semantics + durable generic receipts;
- builtin `send_user_message` в production manager-tool MRO;
- runtime-owned execution context из exact scoped active cycle;
- authenticated internal emission READY/claim/receipt API;
- Telegram emission outbox worker beside final OutputBatch worker;
- server-resolved optional reply binding в input projection;
- reset/terminal sequential fencing;
- compile coverage production IR-6 paths в read-only workflow.

Полное выделение `RuntimeEventSink`/notification service остаётся будущей
modularization/v0.6 задачей.

## Acceptance

IR-6 acceptance подтверждает:

- intermediate message сохраняется до tool success и cycle продолжает работу;
- emission не переводит cycle в `waiting_user` и не меняет context revision;
- same tool-call replay/concurrent same key не создают duplicate emission;
- policy linearizable и не обходится concurrency;
- trusted route не выбирается LLM;
- same-token claim и duplicate receipt идемпотентны;
- ambiguous/expired delivery становится UNKNOWN без blind replay;
- delivery failure не уничтожает AgentCycle и не блокирует final answer;
- reset безопасно fences old READY/DELIVERING;
- already-terminal cycle не принимает новую semantic emission;
- READY emission не начинает новую delivery после уже-visible terminal state;
- Telegram semantic message — отдельный new message;
- optional reply binding scoped и cross-session-safe;
- ordinary progress/`agent_request` не становится durable dialog message;
- final `OutputBatch` остаётся отдельной authority;
- IR-7 concurrent finalization barrier, IR-8 startup reconstruction, IR-9 complete
  projections и IR-10 full roast остаются deferred.
