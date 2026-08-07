---
id: design.v0.4.index
version: v0.4
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-07
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
| 8 | [`v0.4-input-runtime`](v0.4-input-runtime/README.md) | partial | IR-1—IR-4 implemented; IR-5—IR-10 planned: controls, emissions, durable finalization barrier, startup recovery и full acceptance |
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

`v0.4-input-runtime` остаётся update со статусом `partial`. IR-1—IR-4
реализованы и подтверждены CI; IR-5—IR-10 остаются planned.

IR-1/IR-2 предоставляют domain/config/ports foundation, durable filesystem
repositories, bounded coordination, atomic writes, session-local sequence repair,
claims, global identity fencing и crash-recoverable indexes. Global
create/append/prepare использует record-first writes; missing/dangling stable,
relation и cycle-authority indexes восстанавливаются до competing create, а
ambiguous durable identity возвращает managed consistency error.

IR-3 подключает repositories в production composition и маршрутизирует каждый
immutable `CommittedInputBatch` через `InputAdmissionService`. Initial batch
получает service-owned `cycle_id` и запускает один runner. Running additions
получают durable admission, monotonic session/cycle sequence и FIFO
`CycleInboxItem` того же cycle, после чего transport возвращает structured
acknowledgement без второго `MCPClient.process_query()`.

Финальный IR-3 hardening закрепил четыре boundary:

1. **Crash-safe capacity authority.** Count/byte reservation определяется
   authoritative pending/admitted admissions активного cycle. Missing inbox после
   crash не освобождает reservation и не позволяет обойти limit.
2. **Durable runtime handoff.** До потенциально side-effecting runtime invocation
   сохраняется `RuntimeHandoffRecord`; duplicate после marker не вызывает
   `process_query()` повторно, а неоднозначность становится `AMBIGUOUS` и
   `interrupted`.
3. **Cancellation-safe cleanup.** Initial и WAITING paths отдельно обрабатывают
   `asyncio.CancelledError`. Durable cleanup запускается отдельной task, ожидается
   через `asyncio.shield`, переживает повторную cancellation и только затем
   повторно выбрасывается исходная отмена.
4. **Storage-neutral handoff port.** `RuntimeHandoffRepository` содержит только
   command-oriented `get/begin/complete/mark_ambiguous`. Application service не
   создаёт filesystem adapter и не зависит от root/locks/serialization;
   filesystem composition выполняется infrastructure factory.

Pre-handoff cancellation оставляет initial admission retryable. В IR-3 WAITING
path claim после захвата, но до marker, requeue-ится. После marker claim не
requeue-ится для blind replay, marker становится `AMBIGUOUS`, cycle —
`interrupted`, а claim остаётся evidence для будущего IR-8 reconciliation.
Duplicate после любого post-handoff cancellation не запускает второй runtime
invocation.

Exact-cycle wake fencing также сохраняется: late wake старого cycle возвращает
`False` и не изменяет event нового cycle. Stale handoff token не завершает другую
attempt, а terminal timestamps валидируются относительно `handed_off_at`.

IR-3 final code evidence:

- основной commit:
  `e8192380cc3104668ea9b0f3f017d3c962fd65e4` —
  `fix(input-runtime): make IR-3 handoff cancellation-safe and portable`;
- узкий IR-3 test-fixture fix:
  `c36e4cc38095e15f54f63ae81c29b4829defec1f`;
- `Validate Input Runtime` #84 — success, `198 passed`;
- `Validate v0.4 file artifacts PR` #503 — success;
- deterministic no-parallel test: три additions, один `target_cycle_id`,
  `process_query call count == 1`.

IR-4 реализует active-context apply поверх admission/handoff boundary:

1. **Initial context authority.** Initial cycle проходит `CP-RESUME` до первого
   main LLM/result; создаются initial `R1` и durable `ActiveCycleSnapshot`.
2. **Linear context revisions.** Каждый successful applied range создаёт ровно
   следующую `CycleContextRevision`; no-op checkpoint revision не создаёт.
3. **Bounded contiguous FIFO.** `CycleInputApplier` применяет contiguous ranges в
   `cycle_sequence` order с configured count/byte bounds.
4. **Accepted-at-entry watermark.** Checkpoint фиксирует accepted watermark при
   входе и drain-ит только input, относящийся к этой boundary; более поздний input
   ждёт следующего checkpoint.
5. **One update per range.** Несколько contiguous batches внутри одного applied
   range представлены одним runtime-owned `input_batch_update`.
6. **Protocol-safe checkpoint matrix.** Apply встроен в resume/create,
   before-LLM, after-complete-tool-block, before-WAITING,
   before-final-processing, before-terminal-commit и interruption boundaries без
   разрыва OpenAI tool-call sequence.
7. **WAITING common FIFO path.** WAITING reply проходит общий FIFO `CP-RESUME`,
   не обходит более ранние additions, не владеет legacy semantic path и не
   подменяет initial `original_input_batch_id`.
8. **Snapshot-first reconciliation.** Persisted snapshot watermark является
   authority после crash между snapshot persistence и inbox/admission marking;
   reconciliation завершает marking без duplicate update/revision.
9. **Cancellation-safe claim/apply.** Cancellation во время claim acquisition или
   apply сохраняет recoverable durable state и не создаёт duplicate semantic
   application.
10. **Terminal ordering.** Successful runtime handoff completion предшествует
    terminal snapshot synchronization.
11. **Checkpoint stale-candidate suppression.** Accepted input, замеченный до
    WAITING/final checkpoint, подавляет stale candidate и продолжает cycle после
    apply.

IR-4 final code evidence:

- code/test HEAD:
  `1d31b6fbd1d5e88966d3964dc35cf4680f32f522`;
- `Validate Input Runtime` #115 — success, compile success, `241 passed`,
  `0 failed`;
- `Validate v0.4 file artifacts PR` #518 — success, validation suites и status
  enforcement success;
- regression-fix проход после `224911a…` затронул только tests; production code
  не менялся.

IR-4 не расширяет ownership следующих stages. Stale WAITING/final suppression
пока существует только на checkpoint-level. Late terminal race после последнего
checkpoint recheck и до durable terminal commit остаётся IR-7. Ambiguous runtime
handoff/startup reconciliation остаётся IR-8. Полная corruption/restart matrix
`recover_cycle_authority()` остаётся IR-8/IR-10.

Этапы IR-5—IR-10 planned. Явно пока не реализованы:

- IR-5 durable controls;
- `/stop`;
- `/continue`;
- `/reset` redesign на общий durable generation/control contract;
- IR-6 durable `AgentEmission`;
- IR-7 durable finalization barrier;
- IR-8 startup recovery;
- IR-9 complete client projections/diagnostics/config examples;
- IR-10 full race/restart/synthetic/live acceptance;
- scheduler/parallel branches/fork-join semantics;
- Telegram history rewind по edited message.

Целевой package при этом по accepted specification по-прежнему вводит durable
admission, additions в один active AgentCycle, safe checkpoints, линейные context
revisions, `/stop`/`/continue`, durable intermediate messages,
stale-finalization barrier и filesystem recovery ports с заделом на PostgreSQL
`v0.5` и scheduler/interventions `v0.6`.

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
- IR-1—IR-4 уже реализованы; при анализе текущего baseline следует учитывать
  cancellation-safe/storage-neutral handoff, initial `R1`/snapshot,
  accepted-at-entry bounded FIFO apply, linear revisions, snapshot-first
  reconciliation и common FIFO WAITING continuation.
- Не приписывайте IR-5+ текущему baseline: `/stop`, `/continue`, durable control
  plane, `/reset` redesign, durable emissions, finalization barrier и startup
  recovery ещё planned.
- Checkpoint-level stale final candidate suppression не является IR-7 durable
  finalization barrier; late terminal race остаётся IR-7.
- Ambiguous/startup reconciliation остаётся IR-8; полная
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
