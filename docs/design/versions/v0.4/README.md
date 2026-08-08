---
id: design.v0.4.index
version: v0.4
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-08
---

# v0.4 — реестр обновлений Agent Workspace

`v0.4` создаёт agent workspace с внешним хранением полного content,
управляемым LLM-контекстом, optional DAG, атомарным input, доставкой артефактов,
устойчивым модульным runtime и локальным MCP capability registry foundation.

Основная рабочая единица документации — именованное обновление. Крупное
обновление может быть папкой с собственным README и внутренними разделами.

Application/hosting profiles определены в
[`../../runtime-and-deployment-profiles.md`](../../runtime-and-deployment-profiles.md).

## Именованные обновления

| Порядок | Обновление | Статус | Результат |
|---:|---|---|---|
| 1 | [`v0.4-storage-foundation`](v0.4-storage-foundation.md) | implemented | `ContentStore`, `ArtifactStore`, refs и filesystem backend |
| 2 | [`v0.4-result-compaction`](v0.4-result-compaction.md) | implemented | Сохранение и компактное представление tool results |
| 3 | [`v0.4-cycle-compaction`](v0.4-cycle-compaction.md) | implemented | `CycleWorkingMemory` и compaction закрытых segments |
| 4 | [`v0.4-dag-planning`](v0.4-dag-planning.md) | implemented | Optional runtime-owned DAG без scheduler |
| 5 | [`v0.4-file-artifacts`](v0.4-file-artifacts.md) | implemented | Artifact identity, versions, manager tools и delivery foundation |
| 6 | [`v0.4-file-artifacts-advanced`](v0.4-file-artifacts-advanced/README.md) | implemented | Semantic input/output, capabilities, localization, `OutputBatch` и durable Telegram/file recovery |
| 7 | [`v0.4-batch-workflows`](v0.4-batch-workflows/README.md) | implemented | AUTO/EXPLICIT assembly, canonical controls, collection/run presentations, output grouping и bounded same-session artifact handoff |
| 8 | [`v0.4-input-runtime`](v0.4-input-runtime/README.md) | partial | IR-1—IR-7 implemented; IR-8—IR-10 planned: startup recovery, complete projections и full acceptance |
| 9 | [`v0.4-runtime-modularization`](v0.4-runtime-modularization/README.md) | planned | Reusable `AgentRuntime`, Service Application composition, independent ports, `ConfigProvider`, `agent.config` и revisioned configuration snapshots |
| 10 | [`v0.4-mcp-registry-foundation`](v0.4-mcp-registry-foundation/README.md) | planned | Local scopes, trusted MCP metadata, retry/outcome semantics, remote-resource lifecycle и profile-aware transport admission |

`AF-24` порядка `grouping → durable InputBatchDraft → streaming` реализован и
подтверждён live Telegram workflow.

Robustness tests выявили follow-up hardening:

- `AF-25`: terminal failure, failed-group tombstone, `/reset` и automatic
  process-restart reconciliation;
- `AF-26`: exact active-album sequencing, native document-group delivery,
  READY authority cleanup, terminal status fallback и явный artifact format.

`AF-24`–`AF-26` завершили automated и maintainer live acceptance 2026-08-04.

`v0.4-batch-workflows` реализует:

- text-only AUTO input без artificial delay и с user-facing initial status;
- file-first AUTO draft и explicit text/files/mixed collection;
- только `/collect`, `/send`, `/cancel`;
- authenticated shared control plane;
- canonical persisted `EXPLICIT_COLLECTION` с migration старых records;
- active-collection relocation через presentation generations;
- persistent terminal collection snapshot после `/send`/`/cancel`;
- отдельный execution status под `/send` с run-scoped progress overlay;
- receipt-driven `result_ready → terminal delivery status`;
- настраиваемое отдельное `Готово.` через
  `TELEGRAM_FINAL_STATUS_MODE=always|artefacts_only|never`;
- FIFO admission dispatcher на exact Telegram conversation/thread;
- continuation suspended `WAITING_USER` cycle через новый committed package;
- stable semantic OutputPart grouping и Telegram multipart mapping;
- bounded handoff последнего набора artifact refs между последовательными cycles
  одной session;
- очистку bounded handoff вместе с session при `/reset`;
- `artifact_list(scope=current|session|workspace)` и exact activation более старой
  истории за пределами текущего handoff.

Thematic PR CI проверяет compile и отдельные artifact/storage/plans/planning/API
suites. Точный последний head и результаты run фиксируются в GitHub Actions и
описании PR, а не дублируются здесь после каждого закрывающего коммита.

Финальная acceptance `v0.4-file-artifacts-advanced` и
`v0.4-batch-workflows` завершена 2026-08-04. Полный Windows baseline,
расширенная synthetic Web/Telegram/artifact прожарка, race/randomized checks и
живые Telegram scenarios подтвердили принятые контракты без новых deterministic
или flaky defects. Точное evidence находится в
[`../../../../reports/v0.4-transport-artifact-roast.md`](../../../../reports/v0.4-transport-artifact-roast.md),
тематических README и описании PR.

`v0.4-input-runtime` остаётся update со статусом `partial`. IR-1—IR-7
реализованы и подтверждены CI; IR-8—IR-10 остаются planned.

IR-5 final code evidence:

- corrected code/test HEAD `0fabe15c6730a4e8db6be8b54ecec2c13ea773c7`;
- `Validate Input Runtime` #219 — success, compile success, `291 passed`,
  `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #570 — success.

IR-5 реализует transport-neutral durable control service, monotonic control
sequence/idempotency, real pending/applied control watermarks, cooperative
safe-checkpoint `/stop`, paused FIFO input без auto-resume, same-cycle
`/continue` и durable-generation `/reset` с old-work fencing. Resume input target
для `/continue` замораживается атомарно внутри той же durable session coordination
boundary, которая упорядочивает input admission: input, coordinated раньше
continue, входит в initial resume drain; input, coordinated позже, остаётся
следующему running checkpoint. Telegram `/stop`/`/continue` используют общий
Gateway/application contract, а `/cancel` остаётся ingress collection command.

IR-6 добавляет отдельный durable semantic output lifecycle поверх active cycle,
не превращая его ни в progress UI, ни в question, ни в final `OutputBatch`:

- builtin manager tool `send_user_message` принимает только semantic
  `message/kind/importance`; session/cycle/generation/context revision, route,
  client instance, reply target и idempotency принадлежат runtime;
- exact `ManagerToolExecutionContext` связывает native assistant `tool_call_id` с
  exact `cycle_id + generation + context_revision_id + original_input_batch_id`;
- stable idempotency строится от logical tool-call identity; replay того же call
  возвращает тот же `emission_id`, а changed semantic arguments дают controlled
  conflict;
- policy limits `max_intermediate_messages_per_cycle`,
  `min_intermediate_message_interval_seconds` и
  `max_intermediate_message_chars` реально применяются; count/interval acceptance
  linearizable под exact session coordination;
- trusted route snapshot берётся только из authoritative committed input/capability
  state и не сохраняет transport secrets/route metadata;
- persistence `READY` завершается до manager-tool success; optional wake остаётся
  best-effort и не является delivery acknowledgement;
- delivery lifecycle: `READY → DELIVERING → DELIVERED | FAILED | UNKNOWN`, с
  same-token claim retry, competing-token fence, durable generic receipt и
  idempotent receipt replay;
- expired/ambiguous in-flight delivery становится `UNKNOWN`, а не `READY`; blind
  external replay запрещён;
- deterministic preflight/client rejection может стать `FAILED`, а timeout/
  connection ambiguity — только `UNKNOWN`;
- Telegram доставляет semantic intermediate как отдельное plain-text message, не
  progress edit; внешний message ID сохраняется как receipt evidence;
- server-owned Telegram reply metadata может безопасно связать external reply с
  exact delivered emission по session/client instance/conversation/thread и
  добавить optional `reply_to.emission_id` в input projection без branch/FIFO
  semantics;
- delivery failure не меняет `AgentCycle`, WAITING state, context revision или
  input/control watermarks и не блокирует будущий final answer;
- reset переводит old-generation `READY → CANCELLED`, а уже claimed
  `DELIVERING → UNKNOWN`; stale claim writer больше не может завершить его;
- sequential terminal fencing не позволяет создать emission после terminal state
  и не начинает новую delivery для `READY`, если cycle уже terminal.

IR-6 final code evidence:

- code/test boundary `4447d1bfe487bfd764829e701f274655aa8c3c50`;
- `Validate Input Runtime` #297 — success, compile success, `350 passed`,
  `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #609 — success;
- tests используют fake clock, repository recreation, controlled persistence/
  cancellation faults и fake Telegram/HTTP transport; real LLM/MCP/Telegram/Web/
  internet calls не требуются.

IR-7 закрывает durable pre-terminal races, которые IR-6 intentionally оставлял
открытыми:

- existing `CycleFinalizationRecord` становится production authority с stable
  logical finalization identity;
- DONE проходит `CP-BEFORE-FINAL-PROCESSING`, exact candidate authority и durable
  `PREPARED`; final processing сам по себе не резервирует terminal right;
- exact admitted invocation добавляет runtime-owned `admission_id + handoff_token`
  relation к finalization; LLM/client не могут задавать эту identity;
- short prepare recheck и второй pre-terminal recheck используют authoritative
  generation, active-cycle ownership, accepted/applied input watermarks и
  pending/applied control watermarks;
- final result durable сохраняется как `RESULT_PERSISTED`, normal final
  `OutputBatch` — как `OUTPUT_READY`, но оба состояния ещё не дают delivery
  authority;
- corrected terminal order:
  `OUTPUT_READY → second recheck → RuntimeHandoff COMPLETED → terminal snapshot/session → TERMINAL_COMMITTED`;
- mismatch на second recheck abort-ит finalization **до** handoff completion, так
  что same cycle может продолжить работу; completed handoff не появляется раньше
  окончательной terminal eligibility;
- final output становится visible/claimable только после
  `CycleFinalizationRecord=TERMINAL_COMMITTED`; normal admitted-run gate также
  требует matching `RuntimeHandoff=COMPLETED`;
- durable failure transition `HANDED_OFF → COMPLETED` не допускает terminal
  snapshot/session/marker и оставляет output fenced;
- late durable input/control до terminal marker даёт `ABORTED_NEW_INPUT` или
  `ABORTED_CONTROL`; stale persisted output не доставляется и его unclaimed
  cycle-final identity освобождается для следующего same-cycle финала;
- durable admission allocation и terminal commit используют общий session
  ordering: admission-first продвигает accepted watermark и abort-ит stale
  finalization; terminal-first заставляет discard stale optimistic non-start
  classification до writes и transparently reclassify тот же committed batch в
  new-cycle `START_CYCLE` без transport retry;
- terminal-first stale candidate не создаёт old-cycle admission/index/inbox/
  watermark mutation; one input_batch_id получает ровно один admission;
- only dedicated stale-decision conflict разрешает bounded reclassification;
  IR-2 record-first repair/corruption conflicts не маскируются;
- waiting question получает одну durable authority только после
  `CP-BEFORE-WAITING` и exact input/control recheck;
- `AgentEmission READY claim ↔ terminal commit` linearizable через общий
  exact-session coordination lock: claim-first attempt остаётся legitimate,
  terminal-first не позволяет начать новый old-cycle attempt;
- crash после durable handoff COMPLETED, но до terminal marker, direct-retry-ится
  по известному finalization ID с теми же handoff/finalization/result/output IDs и
  без LLM/tool replay; startup discovery остаётся IR-8;
- network/LLM/tool awaits не выполняются под finalization/session lock.

Corrected IR-7 final code evidence:

- code/test boundary `6bd0dce0018b20520ed28236211fccdf0a8075fb`;
- `Validate Input Runtime` #417 — success, production compile success,
  `387 passed`, `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #669 — success;
- workflow permission остаётся `contents: read`;
- focused tests сохраняют предыдущие handoff/order/crash contracts и добавляют
  deterministic terminal-first/admission-first admission races без real
  LLM/MCP/Telegram/Web/internet calls.

Startup-wide reconstruction/reconcile `READY/UNKNOWN`, paused/interrupted/waiting
runtime, committed-but-unadmitted discovery и incomplete finalization scan
остаются IR-8. Полный client timeline, `/status`, Web/CLI projections и addendum
UX остаются IR-9; randomized/full-system/live roast — IR-10.

IR-1—IR-6 implementation evidence и исторические boundary подробно сохранены в
[`v0.4-input-runtime/README.md`](v0.4-input-runtime/README.md) и
[`v0.4-input-runtime/implementation-sequence.md`](v0.4-input-runtime/implementation-sequence.md).

Этапы IR-8—IR-10 planned. Явно пока не реализованы:

- IR-8 startup recovery/reconstruction;
- IR-9 complete client projections/diagnostics/config examples;
- IR-10 full race/restart/synthetic/live acceptance;
- scheduler/parallel branches/fork-join semantics;
- Telegram history rewind по edited message.

Общий статус `v0.4` остаётся `partial`, поскольку `v0.4-input-runtime`,
`v0.4-runtime-modularization` и `v0.4-mcp-registry-foundation` ещё не завершены.

## Связующие документы версии

| Документ | Назначение |
|---|---|
| [`../../runtime-and-deployment-profiles.md`](../../runtime-and-deployment-profiles.md) | Cross-version application profiles, hosting modes, configuration ownership и transport admission |
| [`overview.md`](overview.md) | Цель и граница v0.4 |
| [`v0.4-unified-input-artifact-architecture.md`](v0.4-unified-input-artifact-architecture.md) | Сквозная связь ingress, input batches, artifacts и runtime |
| [`v0.4-release-plan.md`](v0.4-release-plan.md) | Порядок реализации и общие acceptance criteria |
| [`../../contracts/builtin-mcp-service-contract.md`](../../contracts/builtin-mcp-service-contract.md) | Cross-version contract встроенных MCP-сервисов |

## Зависимости обновлений

```text
v0.4-storage-foundation
├── v0.4-result-compaction
├── v0.4-cycle-compaction
├── v0.4-dag-planning
└── v0.4-file-artifacts
    └── v0.4-file-artifacts-advanced
        └── v0.4-batch-workflows
            └── v0.4-input-runtime
                └── v0.4-runtime-modularization
                    └── v0.4-mcp-registry-foundation
```

## Как читать

- Для последовательного проектирования идите по реестру сверху вниз.
- Для конкретного патча открывайте документ с тем же именем, что и обновление.
- `v0.4-batch-workflows` заканчивается до durable active-cycle additions.
- Текущий Telegram FIFO dispatcher является in-process acceptance boundary, а не
  заменой `CycleInbox`.
- [`v0.4-input-runtime`](v0.4-input-runtime/README.md) отвечает за durable
  admission, `CycleInbox`, checkpoints, pause/resume, emissions и terminal
  barrier. Пошаговая реализация находится в
  [`implementation-sequence.md`](v0.4-input-runtime/implementation-sequence.md).
- IR-1—IR-7 уже реализованы; IR-8 startup recovery, IR-9 completion и IR-10 full
  acceptance ещё planned.
- `AgentEmission` — отдельное durable semantic intermediate message; transient
  `ProgressEvent`, `Question/WAITING_USER` и final `OutputBatch` остаются иными
  semantic lifecycles.
- `/stop` является cooperative safe-checkpoint pause, paused additions не
  auto-resume runner, `/continue` возобновляет тот же cycle, а `/reset` использует
  durable generation как authority.
- Persisted result и final `OutputBatch READY` не являются terminal authority;
  corrected IR-7 order сначала завершает matching RuntimeHandoff, затем terminal
  snapshot/session и только потом `TERMINAL_COMMITTED`, после которого разрешён
  claim.
- Admission-vs-terminal ordering также durable: admission-first aborts stale
  terminal candidate; terminal-first reclassifies stale optimistic continuation
  в new-cycle `START_CYCLE` within the same call. IR-8 startup repair для normal
  live race не требуется.
- Ambiguous/startup reconstruction/reconciliation остаётся IR-8; полная
  `recover_cycle_authority()` corruption matrix — IR-8/IR-10.
- Scheduler/parallel branches и Telegram history rewind не реализованы текущим
  input-runtime stage.
- `v0.4-runtime-modularization` меняет ownership, вводит reusable AgentRuntime,
  generic ports, явный Service Application composition и `ConfigProvider`;
  `mcp.config` становится compatibility filename, а `agent.config` — canonical
  service configuration.
- `v0.4-runtime-modularization` не реализует Future Local Agent Application, но
  не должен привязать AgentRuntime к server shell.
- `v0.4-mcp-registry-foundation` применяет ports к scopes, trusted MCP metadata,
  profile-aware admission и remote-resource lifecycle; он не реализует
  конкретный MCP-сервис.
- Новые builtin MCP integrations Service Application используют Streamable HTTP.
- stdio/executable остаётся поддерживаемым MCP runtime adapter, но Service
  Application не запускает user/session-provided executable MCP. Self-hosted
  operator-managed instance stdio требует явной deployment policy.

PostgreSQL/RAG начинаются в [`../v0.5/README.md`](../v0.5/README.md), workers,
distributed orchestration и distributed registry — в
[`../v0.6/README.md`](../v0.6/README.md).
