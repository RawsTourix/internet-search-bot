---
id: design.v0.4.input-runtime.finalization-recovery
version: v0.4
update: v0.4-input-runtime
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-08
---

# Finalization и recovery

## Current boundary after IR-7

Этот документ теперь `partial`: durable finalization barrier реализован IR-7,
а startup recovery/reconstruction остаётся IR-8 planned.

IR-7 production path активирует existing `CycleFinalizationRecord` и закрывает:

- late durable input/control vs stale DONE;
- late durable input/control vs stale WAITING question;
- `PREPARED → RESULT_PERSISTED → OUTPUT_READY → TERMINAL_COMMITTED`;
- final `OutputBatch` persistence отдельно от claim/delivery authority;
- exact-session `AgentEmission READY claim ↔ terminal commit` ordering;
- repository recreation/direct retry собственного finalization protocol;
- partial terminal snapshot/session convergence без premature final delivery.

IR-8 по-прежнему отвечает за startup-wide reconstruction/reconciliation retained
`READY/UNKNOWN`, interrupted/paused/waiting runners, ambiguous handoffs,
incomplete finalizations и startup readiness ordering. IR-7 не добавляет startup
scanner или global recovery coordinator.

IR-6 conservative emission semantics сохранены:

- если cycle уже terminal, новый `send_user_message` rejected;
- terminal-first не позволяет начать новую READY delivery old-cycle emission;
- reset переводит old-generation `READY → CANCELLED`, а already claimed
  `DELIVERING → UNKNOWN`, потому что transport side effect мог произойти;
- expired/ambiguous emission delivery сохраняется `UNKNOWN` и не blind-retry-ится.

IR-7 добавляет общий exact-session ordering для атомарного race:

```text
AgentEmission READY claim
↔ finalization/terminal commit
```

Claim-first attempt легитимно остаётся `DELIVERING`; terminal-first не позволяет
начать новый old-cycle attempt. Network delivery не выполняется под session lock.

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
active cycle == finalizing cycle
cycle status == finalizing
```

Exact finalization candidate additionally сохраняет `context_revision_id` и
expected accepted/applied/control sequence. Persisted result evidence связано с
тем же stable finalization ID.

## Finalization protocol — implemented in IR-7

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

### Phase 7: second recheck и terminal commit

Непосредственно перед terminal marker под той же exact-session coordination
повторно проверяются generation, active cycle, context/finalization identity и
input/control watermarks. Late durable input/control до этого ordering point
выигрывает и abort-ит stale finalization.

Filesystem не имеет multi-file SQL transaction, поэтому terminal convergence
использует последовательные durable writes. Finalization
`TERMINAL_COMMITTED` записывается последним и является server-owned delivery
fence. До этого marker final output остаётся неclaimable.

Repository recreation/direct retry поддерживает partial windows. Lost terminal
commit response не создаёт второй result/output/terminal commit. Если crash успел
оставить terminal snapshot, но terminal authority не завершилась и затем late
input/control стал durable, abort repair-ит stale terminal snapshot обратно к
RUNNING authority.

### Phase 8: delivery

Final `OutputBatch` становится claimable через normal delivery path только после
`TERMINAL_COMMITTED`. Delivery выполняется вне session lock. Execution success и
delivery success остаются разными lifecycle states.

## Why repeated recheck

Input/control может стать durable:

- до final processing;
- во время final processing LLM call;
- после PREPARED;
- после result persistence;
- после OutputBatch persistence/OUTPUT_READY;
- непосредственно перед terminal commit.

Поэтому одного раннего inbox/context check недостаточно.

После terminal commit новый ordinary input начинает новый cycle. До terminal
commit durable accepted input/control обязан suppress stale finalization.

Duplicate transport delivery, не изменившая authoritative accepted/pending
watermark, сама по себе finalization не abort-ит.

## Finalization race matrix

| New event | До PREPARED | После PREPARED, до result | После result/output, до terminal | После terminal |
|---|---|---|---|---|
| user input | apply/continue | abort | abort; retained evidence not deliverable | new cycle |
| `/stop` | pause | abort/pause | abort/pause; output fenced | post-terminal control semantics |
| `/reset` | reset | invalidate generation | invalidate; output fenced | new-generation post-terminal reset semantics |
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
→ terminal commit
```

Attempt легитимно начался до terminal authority и завершает IR-6 lifecycle.
Terminal commit не превращает уже claimed attempt обратно в READY/CANCELLED.

### Terminal commit linearized first

```text
TERMINAL_COMMITTED
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
параллельный durable question lifecycle не создаётся.

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
| `OUTPUT_READY` | delivery remains forbidden until terminal marker |
| partial terminal snapshot/session | finalization marker still gates delivery; retry converges or aborts on newer durable authority |
| `TERMINAL_COMMITTED` response lost | repeat returns same terminal authority; no duplicate final output |
| abort after result/output persistence | retained evidence never becomes claimable/deliverable |

Эта таблица не является startup reconstruction coordinator.

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
8. reconcile incomplete finalization records/results/outputs
9. list resumable cycles and pending deliveries
10. connect MCP/tool runtime
11. allow new admission/runners
```

Admission should not race startup reconciliation. Gateway readiness policy for
этот sequence относится к IR-8, не IR-7.

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
добавить обнаружение и orchestration incomplete records на process startup.

### `PREPARED`

Startup coordinator решает abort/rerun final processing только после
input/control authority recheck.

### `RESULT_PERSISTED`

Startup coordinator может переиспользовать exact persisted result и продолжить
output/terminal convergence без rerun whole AgentCycle.

### `OUTPUT_READY`

Startup coordinator должен проверить terminal authority:

- terminal committed — enable/reconcile delivery;
- not terminal and watermarks unchanged — complete terminal commit;
- new input/control exists — preserve abort/superseded evidence and resume cycle.

### `TERMINAL_COMMITTED`

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

IR-6 command-oriented emission acceptance/claim/receipt и IR-7 command-oriented
finalization operations естественно маппятся на SQL transactions/row locks без
application dependency on filesystem layout.

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

### IR-7 — implemented and validated

- input accepted at every finalization phase aborts stale result until terminal
  commit;
- `/stop`/`/reset` before terminal commit suppress stale final output;
- duplicate input/control without watermark change does not phantom-abort;
- `WAITING_USER` question is suppressed by precommit input/control;
- output is never claimable before terminal commit;
- stale/aborted output never becomes deliverable;
- concurrent AgentEmission claim-vs-terminal commit has deterministic shared
  exact-session ordering;
- network await does not occur under session/finalization coordination;
- repository recreation/direct retry does not duplicate result/output/terminal
  authority;
- post-terminal ordinary input is admitted as new cycle-level work.

Code/test evidence:

- `c58ab05c8354d7e76d4176e39ebf481edc4c613b`;
- `Validate Input Runtime` #355 — success, compile success, `376 passed`,
  `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #638 — success.

### IR-8 — still planned

- startup reconstruction of interrupted/paused/waiting runners;
- global ambiguous runtime handoff reconciliation;
- startup reconciliation retained READY/UNKNOWN emissions;
- startup discovery/convergence incomplete finalizations;
- corruption matrix and startup readiness gate;
- shutdown/startup lifecycle recovery ordering.

Focused IR-7 repository recreation tests не переводят эти IR-8 contracts в
implemented.