---
id: design.v0.4.input-runtime.finalization-recovery
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-08
---

# Finalization и recovery

## Current boundary after corrected IR-7

Этот документ остаётся `partial`: durable finalization barrier реализован IR-7,
а startup recovery/reconstruction остаётся IR-8 planned.

Corrected IR-7 production path активирует existing `CycleFinalizationRecord` и
закрывает:

- late durable input/control vs stale DONE;
- admission decision vs terminal commit durable ordering;
- late durable input/control vs stale WAITING question;
- `PREPARED → RESULT_PERSISTED → OUTPUT_READY → TERMINAL_COMMITTED`;
- exact admitted `RuntimeHandoff` completion между final recheck и terminal
  snapshot/session authority;
- final `OutputBatch` persistence отдельно от claim/delivery authority;
- exact-session `AgentEmission READY claim ↔ terminal commit` ordering;
- repository recreation/direct retry собственного finalization protocol;
- completed-handoff/incomplete-terminal direct convergence без LLM/tool replay.

IR-8 по-прежнему отвечает за startup-wide reconstruction/reconciliation retained
`READY/UNKNOWN`, interrupted/paused/waiting runners, ambiguous handoffs,
incomplete finalizations и startup readiness ordering. IR-7 не добавляет startup
scanner или global recovery coordinator. Normal live admission-vs-terminal race
разрешается IR-7 в исходном in-process admission call и не зависит от IR-8
committed-but-unadmitted discovery.

IR-6 conservative emission semantics сохранены:

- если cycle уже terminal, новый `send_user_message` rejected;
- terminal-first не позволяет начать новую READY delivery old-cycle emission;
- reset переводит old-generation `READY → CANCELLED`, а already claimed
  `DELIVERING → UNKNOWN`, потому что transport side effect мог произойти;
- expired/ambiguous emission delivery сохраняется `UNKNOWN` и не blind-retry-ится.

IR-7 использует общий exact-session ordering для атомарных races:

```text
AgentEmission READY claim
↔ finalization/terminal commit

InputAdmission durable allocation
↔ finalization/terminal commit
```

Wall clock, task creation order и optimistic application state read не являются
authority.

## Назначение

Документ закрывает гонки между новым input/control и завершением AgentCycle,
определяет durable order записи результата и conservative recovery после crash.

Finalization часть реализована IR-7. Startup/process-restart recovery часть
остаётся IR-8 specification.

## Проблема stale final answer

Недопустимый сценарий:

```text
agent сформировал DONE
→ runtime проверил пустой inbox
→ пользовательский batch durable accepted
→ старый ответ всё равно отправлен
```

То же относится к `WAITING_USER`, `/stop` и `/reset`.

IR-7 не использует empty-inbox как terminal authority. Он повторно читает durable
session input/control watermarks непосредственно под coordination boundary.

Отдельно недопустим stale admission decision:

```text
application прочитал FINALIZING cycle A
→ классифицировал batch как CONTINUE_RUNNING(A)
→ terminal commit durable выиграл ordering
→ stale candidate позже мутирует уже terminal session
```

Corrected IR-7 не ослабляет terminal-state validation. Repository распознаёт
устаревшую classification до writes и application reclassifies тот же committed
batch против latest state.

## Watermarks и exact invocation authority

Для active cycle runtime хранит:

```text
accepted_through_cycle_sequence
applied_through_cycle_sequence
pending_control_sequence
applied_control_sequence
```

Terminal eligibility разрешена только когда:

```text
accepted_through_cycle_sequence == applied_through_cycle_sequence
pending_control_sequence == applied_control_sequence
session generation == cycle generation
active cycle == finalizing cycle
cycle status == finalizing
```

Exact finalization candidate additionally сохраняет `context_revision_id` и
expected accepted/applied/control sequence. Exact admitted invocation передаёт
runtime-owned `admission_id + handoff_token`, которые проверяются против durable
`RuntimeHandoff`. Эти values не задаются LLM/client и не выводятся из mutable
global state. Persisted result evidence связано с тем же stable finalization ID.

## Finalization protocol — implemented in corrected IR-7

### Phase 1: candidate

Agent loop создаёт final candidate, но candidate ещё не является terminal
authority и не разрешает client delivery.

### Phase 2: pre-processing checkpoint

`CP-BEFORE-FINAL-PROCESSING` проверяет control/input. При mismatch candidate
отбрасывается и cycle продолжает existing input/control transition.

### Phase 3: final processing

Final audit/formatting/grounding работает относительно exact immutable candidate
view: session/cycle/generation/context revision и expected input/control
watermarks. Начатый final-processing LLM call не резервирует terminal right.

### Phase 4: prepared record

Durable stable `CycleFinalizationRecord(state=PREPARED)` создаётся идемпотентно.
Logical retry той же exact candidate authority получает тот же finalization ID.
Для normal admitted-run path finalization durable связывается с exact current
`RuntimeHandoff` relation (`admission_id + handoff_token`).

### Phase 5: short coordination recheck

Под короткой exact-session boundary:

1. читается latest authoritative session state;
2. проверяются generation и active-cycle ownership;
3. проверяются accepted/applied input watermarks;
4. проверяются pending/applied control watermarks и effective pause/reset state;
5. проверяется exact finalization identity;
6. cycle переводится в `FINALIZING` и finalization authority закрепляется.

Mismatch даёт:

```text
ABORTED_NEW_INPUT
ABORTED_CONTROL
```

Cycle не получает stale terminal delivery.

### Phase 6: durable result и output

Implemented order:

```text
persist final AgentResult evidence
→ RESULT_PERSISTED
→ assemble/persist normal final OutputBatch
→ OUTPUT_READY
```

`RESULT_PERSISTED` и `OUTPUT_READY` не являются terminal/delivery authority.
Ready outbox скрывает такой final batch, а direct claim отвергается.

Если finalization abort-ится после output persistence, stale unclaimed aggregate
не доставляется и освобождает cycle-final commit-once binding, чтобы следующий
корректный same-cycle final result не конфликтовал с superseded batch.

### Phase 7: second recheck → RuntimeHandoff completion → terminal commit

Непосредственно перед любым successful terminal authority под одной короткой
exact-session coordination повторно проверяются generation, active cycle,
context/finalization identity и input/control watermarks.

Corrected successful order:

```text
OUTPUT_READY
→ acquire exact-session finalization coordination
→ second authoritative terminal recheck
→ matching RuntimeHandoff HANDED_OFF → COMPLETED
→ terminal ActiveCycleSnapshot
→ terminal SessionInputRuntimeState
→ CycleFinalizationRecord TERMINAL_COMMITTED
→ release coordination
→ final OutputBatch claim eligibility
```

Late durable input/control до second recheck выигрывает и abort-ит stale
finalization **до** RuntimeHandoff completion. Поэтому handoff остаётся
`HANDED_OFF`, same cycle может продолжить LLM/tool работу и отсутствует ложное
утверждение, что side-effecting invocation завершена.

`RuntimeHandoffRepository.complete()` обычно сам берёт session lock, поэтому
corrected filesystem adapter использует command-oriented lock-aware internal
completion primitive внутри уже удерживаемой finalization coordination. Application
layer не знает filesystem layout/lock implementation; future PostgreSQL adapter
может выразить тот же command одной transaction/row-lock boundary.

Если durable transition `HANDED_OFF → COMPLETED` падает или cancellation
происходит до его persistence:

```text
NO terminal snapshot
NO terminal session DONE
NO TERMINAL_COMMITTED
NO final-output delivery eligibility
```

Existing handoff ambiguity/cancellation policy остаётся authoritative.

Filesystem не имеет multi-file SQL transaction, поэтому после successful handoff
completion terminal convergence использует последовательные durable writes.
Finalization `TERMINAL_COMMITTED` записывается последним и является server-owned
delivery fence. До этого marker final output остаётся неclaimable.

### Phase 8: delivery

Final `OutputBatch` становится claimable через normal delivery path только после
`TERMINAL_COMMITTED`. Для normal admitted-run path delivery gate дополнительно
проверяет, что matching exact `RuntimeHandoff` уже `COMPLETED`. Delivery выполняется
вне session lock. Execution success и delivery success остаются разными lifecycle
states.

## Admission decision ↔ terminal commit — implemented IR-7

Application admission может прочитать `FINALIZING` и построить optimistic
`CONTINUE_RUNNING` candidate до repository coordination. Этот read не является
linearization point.

Repository allocation и terminal commit используют общий durable
`root identity → session` ordering.

### Admission allocation linearized first

```text
candidate CONTINUE_RUNNING(A)
→ repository durable allocation to cycle A
→ accepted watermark advances
→ terminal second recheck
→ accepted > applied
→ ABORTED_NEW_INPUT
→ RuntimeHandoff remains HANDED_OFF
→ session/cycle A RUNNING
```

Таким образом late input действительно выигрывает stale finalization, а не просто
существует как committed batch вне runtime.

### Terminal authority linearized first

```text
candidate CONTINUE_RUNNING(A) already formed optimistically
→ terminal second recheck clean
→ RuntimeHandoff COMPLETED
→ terminal snapshot/session
→ TERMINAL_COMMITTED
→ stale candidate reaches repository allocation
→ dedicated stale-decision conflict before writes
→ application re-reads latest state
→ recomputes full admission decision
→ START_CYCLE(B), cycle_sequence=0
```

Reclassification выполняется bounded: только recognized stale-decision conflict
может запустить один repeat. Arbitrary corruption/consistency conflict не
retry-ится.

До reclassification stale candidate не оставляет:

```text
InputAdmissionRecord
admission index
CycleInboxItem
accepted watermark mutation
```

После resolution один `input_batch_id` имеет ровно один durable admission.
Duplicate replay возвращает existing relation.

Для raw `IDLE` repository сначала сохраняет IR-2 authoritative admission repair:
если state отстал после record-first START admission, он repair-ится до RUNNING;
если durable records corrupt, исходный corruption conflict остаётся видимым.
Только genuinely idle state после repair считается stale для non-start candidate.
Normal terminal `DONE/ERROR/CANCELLED` fenced до admission writes.

Этот live protocol **не** является IR-8 startup committed-but-unadmitted repair.

## Why repeated recheck

Input/control может стать durable:

- до final processing;
- во время final processing LLM call;
- после PREPARED;
- после result persistence;
- после OutputBatch persistence/OUTPUT_READY;
- непосредственно перед terminal coordination.

Поэтому одного раннего inbox/context check недостаточно.

После terminal commit новый ordinary input начинает новый cycle. До terminal
commit durable accepted input/control обязан suppress stale finalization.
Duplicate transport delivery, не изменившая authoritative accepted/pending
watermark, сама по себе finalization не abort-ит.

## Finalization race matrix

| New event | До PREPARED | После PREPARED, до result | После result/output, до terminal | После terminal |
|---|---|---|---|---|
| user input | apply/continue | abort | durable admission-first aborts before handoff completion; terminal-first reclassifies same batch to new cycle | new cycle |
| `/stop` | pause | abort/pause | abort before handoff completion; output fenced | post-terminal control semantics |
| `/reset` | reset | invalidate generation | invalidate before handoff completion; output fenced | new-generation post-terminal reset semantics |
| duplicate input/control | no watermark change | no phantom abort | no phantom abort | idempotent/new decision |
| final output delivery retry | n/a | forbidden | forbidden | delivery subsystem |
| intermediate emission create | allowed while running | finalization policy | cannot bypass terminal authority | rejected/new-cycle decision |
| intermediate emission claim | normal READY claim | shared exact-session order | shared exact-session order | no new old-cycle READY claim |

Persisted stale result/output после abort не считается final answer и не
доставляется.

## AgentEmission claim ↔ terminal commit — implemented in IR-7

Оба command path используют тот же exact-session coordination lock.

### Claim linearized first

```text
READY → DELIVERING
→ release lock
→ network send may start/continue
→ later terminal coordination
```

Attempt легитимно начался до terminal authority и завершает IR-6 lifecycle.
Terminal commit не превращает уже claimed attempt обратно в READY/CANCELLED.

### Terminal coordination linearized first

```text
second final recheck
→ RuntimeHandoff COMPLETED
→ terminal snapshot/session
→ TERMINAL_COMMITTED
→ old-cycle READY claim request
→ claim rejected/cancelled before transport attempt
```

Network await не выполняется под shared lock. Wall clock/task scheduling не
являются authority.

## WAITING_USER commit — implemented barrier

Waiting transition использует облегчённый durable barrier:

```text
candidate question
→ CP-BEFORE-WAITING
→ short exact input/control recheck
→ persist waiting snapshot + one question authority
→ WAITING_USER commit
```

Если input/control durable accepted до waiting commit, stale question suppressed.
Если input приходит после waiting commit, existing admission `RESUME_WAITING`
продолжает тот же cycle.

`send_user_message(kind=intermediate)` не используется как ask-user и второй
параллельный durable question lifecycle не создаётся. Corrective terminal/admission
ordering не меняет WAITING semantics.

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

## IR-7 direct crash/retry classification — implemented

IR-7 покрывает только replay собственного finalization protocol после repository
recreation, без startup scanner:

| Persisted point | Direct retry/convergence |
|---|---|
| `PREPARED` | reuse same finalization ID; later authority recheck still required |
| result evidence persisted, state write lost | same payload hash/ref reused; state converges to `RESULT_PERSISTED` |
| `RESULT_PERSISTED` | same logical result reused; no second main AgentCycle |
| OutputBatch persisted, `OUTPUT_READY` state write lost | same output identity is rebound/reused by retry path |
| `OUTPUT_READY`, handoff still `HANDED_OFF` | delivery remains forbidden; second recheck must still run before completion |
| handoff `COMPLETED`, terminal snapshot/session/marker incomplete | direct known-ID retry keeps same handoff token/completed_at, finalization ID, result_ref and OutputBatch ID; no LLM/tool replay |
| partial terminal snapshot/session | finalization marker still gates delivery; retry converges without second handoff completion |
| `TERMINAL_COMMITTED` response lost | repeat returns same terminal authority; no duplicate final output |
| abort after result/output persistence | retained evidence never becomes claimable/deliverable |

Durable `COMPLETED` handoff не переводится обратно в `HANDED_OFF`/`AMBIGUOUS`.
Поздний API compatibility call `complete_runtime_handoff()` идемпотентен и
возвращает тот же COMPLETED marker без нового token или `completed_at`.

Эта таблица не является startup reconstruction coordinator. IR-8 должен только
обнаруживать/reconcile такие records на startup; IR-7 direct retry уже умеет
сойтись, когда известны exact IDs.

## Cancellation windows — corrected IR-7

### До handoff completion

Cancellation/failure не создаёт terminal delivery authority. Existing API cleanup
может сохранить `AMBIGUOUS` согласно IR-3 policy. Исходный `CancelledError`
сохраняется существующим cancellation contract.

### После durable COMPLETED, до TERMINAL_COMMITTED

Handoff остаётся `COMPLETED` и не может быть понижен до `AMBIGUOUS`. Output всё
ещё fenced, потому что terminal marker отсутствует. Direct finalization retry
заканчивает terminal convergence без side-effect replay.

## Restart classification — planned IR-8

После process restart runtime не должен делать вид, что in-flight operation
завершена.

| Persisted state | Recovery classification |
|---|---|
| `running` | `interrupted`, resumable from last safe snapshot |
| `pause_requested` | finish as `paused_by_user` if safe snapshot exists, else `interrupted` with pause intent |
| `paused_by_user` | remain paused |
| `waiting_user` | remain waiting |
| `finalizing` + incomplete finalization | startup reconcile finalization record |
| terminal committed | do not rerun cycle |
| inbox claim expired | reconcile/requeue |
| AgentEmission `READY` | retain durable pending intent; startup scheduling/reconcile policy is IR-8 |
| AgentEmission `DELIVERING` lease expired | `UNKNOWN`, never automatic `READY` |
| AgentEmission `UNKNOWN` | retain ambiguity; no blind client resend |

IR-6/IR-7 repository recreation сохраняет durable records и direct retry
idempotency, но startup-wide runner/outbox reconstruction — IR-8.

## Startup recovery sequence — planned IR-8

Recommended order:

```text
1. load/repair session runtime states
2. reconcile committed batches without admission
3. reconcile admission/session watermarks
4. expire/reconcile inbox claims
5. reconcile control commands/generation
6. reconcile active-cycle snapshots
7. reconcile emission claims/UNKNOWN receipts without blind resend
8. discover/reconcile incomplete finalization and handoff records
9. list resumable cycles and pending deliveries
10. connect MCP/tool runtime
11. allow new admission/runners
```

Admission should not race startup reconciliation. Gateway readiness policy for
этот sequence относится к IR-8, не IR-7. Normal in-process admission-vs-terminal
race уже разрешена IR-7 и не является startup item.

## Active cycle recovery — planned IR-8

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

IR-6 уже реализует no-blind-retry правило для AgentEmission UNKNOWN; IR-7 его не
ослабляет. Startup-specific reconciliation UX/decision остаётся IR-8.

## Inbox recovery — planned/startup side

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

## Finalization startup recovery — planned IR-8

IR-7 уже делает direct retry/repository recreation идемпотентным. IR-8 должен
добавить обнаружение и orchestration incomplete records на process startup,
включая valid `RuntimeHandoff=COMPLETED` + incomplete terminal convergence.

### `PREPARED`

Startup coordinator решает abort/rerun final processing только после
input/control authority recheck.

### `RESULT_PERSISTED`

Startup coordinator может переиспользовать exact persisted result и продолжить
output/terminal convergence без rerun whole AgentCycle.

### `OUTPUT_READY`

Startup coordinator должен проверить terminal/handoff authority:

- terminal committed — enable/reconcile delivery;
- handoff completed + terminal incomplete — invoke safe direct terminal convergence;
- handoff still active + watermarks unchanged — run final recheck, then complete handoff and terminal commit;
- new input/control exists before handoff completion — preserve abort/superseded evidence and resume cycle.

### `TERMINAL_COMMITTED`

Cycle never reruns. Matching admitted-run RuntimeHandoff уже обязан быть
`COMPLETED`. Output delivery/retrieval handled independently.

## Retention

Recovery/audit records are not immediately deleted after success. Cleanup policy
may compact/archive old admissions, revisions, emissions and finalization records
only after they are no longer needed for replay, diagnostics or migration.

Committed user inputs and artifacts are not treated as temporary queue files.

## PostgreSQL migration

In `v0.5` admission, watermarks, handoff completion, finalization and output intent
can be committed in explicit SQL transactions with row locks. Recovery semantics
and states remain the same; filesystem repair paths become migration/import tools.

IR-6 command-oriented emission acceptance/claim/receipt и corrected IR-7
command-oriented terminal/admission operations естественно маппятся на SQL
transactions/row locks без application dependency on filesystem layout.

## Distributed preparation

In `v0.6` add:

```text
worker lease
fencing token
run/task/workflow IDs
durable queue/event signal
```

PostgreSQL remains source of truth; Redis signal loss does not lose input.
AgentEmission semantic identity сохраняет session/cycle/generation/context
revision и не зависит от Telegram external message ID.

## Acceptance

### IR-7 — implemented and validated after corrective passes

- input accepted at every finalization phase aborts stale result until terminal
  commit;
- `/stop`/`/reset` before terminal commit suppress stale final output;
- duplicate input/control without watermark change does not phantom-abort;
- `WAITING_USER` question is suppressed by precommit input/control;
- late final recheck occurs before RuntimeHandoff completion;
- abort never completes handoff prematurely;
- successful terminal path persists matching RuntimeHandoff COMPLETED before
  terminal snapshot/session and TERMINAL_COMMITTED;
- handoff completion persistence failure produces no terminal authority;
- output is never claimable before terminal commit and normal admitted-run
  delivery gate requires matching completed handoff;
- completed-handoff/incomplete-terminal direct retry preserves exact IDs and does
  not replay LLM/tool work;
- terminal-first stale admission transparently reclassifies the same committed
  batch to `START_CYCLE` in a new cycle without transport retry or raw Pydantic
  validation failure;
- admission-first durable allocation advances accepted watermark and makes the
  terminal barrier return `ABORTED_NEW_INPUT` before handoff completion;
- stale admission classification creates no old-cycle admission/index/inbox/state
  mutation before reclassification and one committed batch ends with exactly one
  admission;
- IR-2 state repair/corruption authority is not masked by stale-decision retry;
- concurrent AgentEmission claim-vs-terminal commit has deterministic shared
  exact-session ordering;
- network await does not occur under session/finalization coordination;
- repository recreation/direct retry does not duplicate result/output/terminal
  authority;
- post-terminal ordinary input is admitted as new cycle-level work.

Corrected code/test evidence:

- `6bd0dce0018b20520ed28236211fccdf0a8075fb`;
- `Validate Input Runtime` #417 — success, compile success, `387 passed`,
  `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #669 — success;
- workflow permission remains `contents: read`.

### IR-8 — still planned

- startup reconstruction of interrupted/paused/waiting runners;
- startup committed-but-unadmitted discovery/repair;
- global ambiguous runtime handoff reconciliation;
- startup reconciliation retained READY/UNKNOWN emissions;
- startup discovery/convergence incomplete finalizations;
- corruption matrix and startup readiness gate;
- shutdown/startup lifecycle recovery ordering.

Focused IR-7 live-race/direct-retry tests не переводят эти IR-8 contracts в
implemented.
