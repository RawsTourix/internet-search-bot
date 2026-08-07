---
id: design.v0.4.input-runtime.finalization-recovery
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: planned
last_reviewed: 2026-08-08
---

# Finalization и recovery

## Current boundary after IR-6

Этот документ остаётся `planned`, потому что durable finalization barrier и
startup recovery принадлежат следующим stages.

IR-6 уже реализует только sequential emission fencing вокруг terminal state:

- если cycle уже terminal, новый `send_user_message` rejected;
- если semantic emission уже `READY`, но terminal state стал authoritative до
  начала claim, новая client delivery не начинается и record отменяется;
- reset переводит old-generation `READY → CANCELLED`, а already claimed
  `DELIVERING → UNKNOWN`, потому что transport side effect мог произойти;
- expired/ambiguous emission delivery также сохраняется `UNKNOWN` и не
  blind-retry-ится.

IR-6 **не** закрывает атомарный race:

```text
AgentEmission claim begins
↔ finalization/terminal commit
```

Если closure требует общей durable finalization authority, он принадлежит IR-7
вместе с late input/control-vs-terminal races. Startup reconstruction и
reconciliation retained `READY/UNKNOWN`, interrupted/paused/waiting runtime и
ambiguous side effects принадлежат IR-8. Поэтому IR-6 sequential terminal fencing
не должно интерпретироваться как готовый finalization barrier/recovery pipeline.

## Назначение

Документ закрывает гонки между новым input/control и завершением AgentCycle,
определяет durable order записи результата и conservative recovery после crash.

## Проблема stale final answer

Недопустимый сценарий:

```text
agent сформировал DONE
→ runtime проверил пустой inbox
→ пользовательский batch durable accepted
→ старый ответ всё равно отправлен
```

То же относится к `WAITING_USER`, `/stop` и `/reset`.

После IR-6 к terminal race matrix добавляется ещё одна граница: READY semantic
intermediate не должен начинать новую delivery после уже-known terminal state, но
claim, начавшийся concurrently с terminal commit, требует IR-7 coordination.

## Watermarks

Для active cycle runtime хранит:

```text
accepted_through_cycle_sequence
applied_through_cycle_sequence
pending_control_sequence
applied_control_sequence
```

Terminal commit разрешён только когда:

```text
accepted_through_cycle_sequence == applied_through_cycle_sequence
pending_control_sequence == applied_control_sequence
session generation == cycle generation
cycle status == finalizing
```

Final result сохраняет:

```text
context_revision_id
consumed_input_batch_ids or bounded recent set
consumed_through_cycle_sequence
consumed_through_control_sequence
```

## Finalization protocol

### Phase 1: candidate

Agent loop создаёт final candidate, но ещё не меняет cycle в terminal state и не
разрешает client delivery.

### Phase 2: pre-processing checkpoint

`CP-BEFORE-FINAL-PROCESSING` проверяет control/input. При mismatch candidate
отбрасывается и cycle продолжается/останавливается.

### Phase 3: final processing

Final audit/formatting/grounding работает с immutable context revision и
зафиксированными expected watermarks.

### Phase 4: prepared record

Создаётся `CycleFinalizationRecord(state=prepared)` с expected values.

### Phase 5: short coordination recheck

Под короткой session boundary:

1. прочитать latest session state;
2. проверить generation/active cycle ownership;
3. проверить input/control watermarks;
4. проверить отсутствие effective pause/reset;
5. перевести cycle в `finalizing`, если ещё не переведён;
6. зарезервировать finalization ID.

Mismatch даёт:

```text
aborted_new_input
aborted_control
```

Cycle возвращается в checkpoint без terminal delivery.

### Phase 6: durable result и output

Порядок:

```text
persist final AgentResult/content reference
→ mark finalization result_persisted
→ assemble/persist OutputBatch READY
→ mark finalization output_ready
```

`OutputBatch` не доставляется до terminal commit.

### Phase 7: terminal commit

Под той же semantic coordination boundary повторно проверяются generation и
watermarks. Затем атомарно насколько возможно для filesystem:

```text
cycle terminal state
session active cycle cleared/terminal reference set
finalization terminal_committed
output delivery_allowed=true
```

Filesystem records могут записываться последовательно, поэтому recovery record
обязателен.

IR-7 должен также определить shared ordering с началом `AgentEmission` delivery,
если linearizable closure race `claim ↔ terminal commit` требует той же boundary.
IR-6 не удерживает network delivery под session lock и не создаёт temporary
finalization record ради этого race.

### Phase 8: delivery

Delivery выполняется вне session lock. Execution success и delivery success
различаются.

## Why repeated recheck

Input может прийти:

- до final processing;
- во время final processing LLM call;
- после prepared record;
- после result persistence, но до terminal commit.

Поэтому одного раннего inbox check недостаточно.

После terminal commit новый input начинает новый cycle. До terminal commit он
обязан abort stale finalization.

## Finalization race matrix

| New event | До prepared | После prepared, до result | После result, до terminal | После terminal |
|---|---|---|---|---|
| user input | apply/continue | abort | abort; retained result diagnostic | new cycle |
| `/stop` | pause | abort/pause | abort/pause | no active cycle |
| `/reset` | reset | invalidate generation | invalidate, cancel output | new generation |
| duplicate input | no change | no change | no change | idempotent/new decision |
| final output delivery retry | n/a | n/a | forbidden | delivery subsystem |
| intermediate emission create | allowed while running | policy/finalization-specific | must not bypass terminal authority | rejected/new-cycle decision |
| intermediate emission claim | delivery subsystem | delivery subsystem | IR-7 shared race fence if required | no new claim for old cycle |

Persisted stale result после abort не считается final answer и не доставляется.
Он может быть retained как diagnostic content с relation `superseded`/`cancelled`.

IR-6 гарантирует только уже-visible terminal decision для intermediate emission;
таблица не означает, что concurrent claim/finalization race уже атомарно закрыт.

## WAITING_USER commit

Waiting transition использует облегчённый barrier:

```text
candidate question
→ CP-BEFORE-WAITING
→ short input/control recheck
→ persist waiting snapshot + question emission intent
→ waiting_user
```

Если input принят до waiting commit, question suppressed.

Если input приходит после waiting commit, admission `resume_waiting` продолжает
тот же cycle.

IR-6 не мигрирует question в `send_user_message` и не создаёт второй durable
question path. Полная waiting/finalization ordering остаётся IR-7.

## Pause commit

```text
pause requested
→ complete atomic block
→ persist active snapshot
→ apply pause control watermark
→ cycle/session paused_by_user
→ acknowledge applied pause
```

Additions accepted до pause commit остаются queued unless checkpoint policy
успела применить их до pause. Default: pause имеет priority; input сохраняется на
resume.

IR-6 READY semantic intent, persisted до safe pause, не отменяется только из-за
pause и может быть доставлен отдельно. Paused runner сам не генерирует новые
emissions до resume.

## Restart classification

После process restart runtime не делает вид, что in-flight operation завершена.

| Persisted state | Recovery classification |
|---|---|
| `running` | `interrupted`, resumable from last safe snapshot |
| `pause_requested` | finish as `paused_by_user` if safe snapshot exists, else `interrupted` with pause intent |
| `paused_by_user` | remain paused |
| `waiting_user` | remain waiting |
| `finalizing` + prepared | reconcile finalization record |
| terminal committed | do not rerun cycle |
| inbox claim expired | reconcile/requeue |
| AgentEmission `READY` | retain durable pending intent; startup scheduling/reconcile policy is IR-8 |
| AgentEmission `DELIVERING` lease expired | `UNKNOWN`, never automatic `READY` |
| AgentEmission `UNKNOWN` | retain ambiguity; no blind client resend |

IR-6 repository recreation уже сохраняет READY/UNKNOWN/DELIVERED records и
classifies expired in-flight claim as UNKNOWN. Он не выполняет startup-wide
runner/outbox reconstruction; это IR-8.

## Startup recovery sequence

Recommended order:

```text
1. load/repair session runtime states
2. reconcile committed batches without admission
3. reconcile admission/session watermarks
4. expire/reconcile inbox claims
5. reconcile control commands/generation
6. reconcile active-cycle snapshots
7. reconcile emission claims/UNKNOWN receipts without blind resend
8. reconcile finalization records/results/outputs
9. list resumable cycles and pending deliveries
10. connect MCP/tool runtime
11. allow new admission/runners
```

Admission should not race startup reconciliation. Gateway reports degraded/not
ready until mandatory runtime recovery complete.

Эта startup sequence остаётся specification IR-8, а не выполненной частью IR-6.

## Active cycle recovery

Recovery requires last durable safe checkpoint snapshot.

```text
snapshot valid + protocol-valid messages
→ status interrupted/waiting/paused
→ can_resume=true according to policy
```

Snapshot validation:

- session/cycle/generation ownership;
- OpenAI tool sequence valid;
- referenced committed batches exist;
- applied sequence consistent with context revision/admissions;
- required artifact/result refs resolvable or explicitly unavailable;
- no terminal finalization already committed.

Invalid snapshot produces controlled non-resumable error, not silent new cycle
using partial state.

## Ambiguous side effects

Не повторяются автоматически:

- mutating MCP/tool call с потерянным response;
- client delivery со state `unknown`;
- AgentEmission delivery с expired/ambiguous attempt;
- external operation, начатая после last safe snapshot;
- final output delivery без reliable receipt.

Recovery marks outcome unknown and requires agent/user reconciliation according to
existing tool/delivery policies.

Read-only/idempotent operations могут быть retried только при declared policy.

IR-6 уже реализует no-blind-retry правило для AgentEmission UNKNOWN, но
startup-specific reconciliation UX/decision остаётся IR-8.

## Inbox recovery

### `claimed` expired

Return to `queued` if snapshot watermark ниже item sequence.

### `applying` expired

```text
snapshot applied watermark >= item sequence
→ mark item/admission applied

snapshot lower
→ requeue claimed range
```

### Gaps

Non-contiguous admissions/inbox sequence block application and terminalization.
Recovery attempts deterministic repair from admission records. Missing immutable
record is terminal consistency defect.

## Finalization recovery

### `prepared`

No durable result/output: abort or rerun final processing only after input/control
recheck.

### `result_persisted`

No output: reuse exact persisted result and assemble output; do not rerun main
AgentCycle.

### `output_ready`

Recheck terminal state:

- terminal committed — enable/reconcile delivery;
- not terminal and watermarks unchanged — complete terminal commit;
- new input/control exists — mark output cancelled/superseded, resume cycle.

### `terminal_committed`

Cycle never reruns. Output delivery/retrieval handled independently.

## Retention

Recovery/audit records are not immediately deleted after success. Cleanup policy
may compact/archive old admissions, revisions, emissions and finalization records
only after they are no longer needed for replay, diagnostics or migration.

Committed user inputs and artifacts are not treated as temporary queue files.

## PostgreSQL migration

In `v0.5` admission, watermarks, finalization and output intent can be committed in
one or several explicit SQL transactions with row locks. Recovery semantics and
states remain the same; filesystem repair paths become migration/import tools.

IR-6 command-oriented emission acceptance/claim/receipt semantics естественно
маппятся на `INSERT ... ON CONFLICT`, row locking, fenced claim token и indexed
READY query без application dependency on filesystem layout.

## Distributed preparation

In `v0.6` add:

```text
worker lease
fencing token
run/task/workflow IDs
durable queue/event signal
```

PostgreSQL remains source of truth; Redis signal loss does not lose input.
AgentEmission semantic identity уже сохраняет session/cycle/generation/context
revision и не зависит от Telegram external message ID.

## Acceptance

IR-7/IR-8 finalization/recovery acceptance остаётся:

- input accepted at every finalization phase either aborts stale result or starts
  new cycle only after terminal commit;
- `/stop`/`/reset` accepted before terminal commit prevents success delivery;
- output is never delivered before terminal commit;
- concurrent AgentEmission claim-vs-terminal commit имеет explicit atomic ordering;
- restart after result persistence does not rerun whole AgentCycle;
- restart after terminal commit never creates duplicate AgentCycle;
- expired applying claim reconciles without duplicate LLM update;
- paused/waiting states survive restart;
- invalid snapshot fails controlled and preserves evidence;
- ambiguous side effect is never blindly repeated;
- startup recovery completes before new admission is enabled.

IR-6 уже подтверждает subset: sequential terminal emission fence, reset
READY/DELIVERING conservative semantics, expiry→UNKNOWN and no blind resend. Этот
subset не переводит данный document из `planned` в `implemented`.
