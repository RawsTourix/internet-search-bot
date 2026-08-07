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
| 8 | [`v0.4-input-runtime`](v0.4-input-runtime/README.md) | partial | IR-1—IR-5 implemented; IR-6—IR-10 planned: emissions, durable finalization barrier, startup recovery и full acceptance |
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

`v0.4-input-runtime` остаётся update со статусом `partial`. IR-1—IR-5
реализованы и подтверждены CI; IR-6—IR-10 остаются planned.

IR-5 final code evidence:

- code/test HEAD `85c52d4b60a60786bdb10732eb0a52893a422eee`;
- `Validate Input Runtime` #173 — success, compile success, `278 passed`, `0 failed`;
- `Validate v0.4 file artifacts PR` #547 — success.

IR-5 реализует transport-neutral durable control service, monotonic control
sequence/idempotency, real pending/applied control watermarks, cooperative
safe-checkpoint `/stop`, paused FIFO input без auto-resume, same-cycle
`/continue` с pre-continue drain target и durable-generation `/reset` с old-work
fencing. Telegram `/stop`/`/continue` используют общий Gateway/application
contract, а `/cancel` остаётся ingress collection command.

IR-5 checkpoint-level pause/reset suppression перед terminal transition не
является полным IR-7 barrier. Late race после последнего checkpoint/recheck и до
durable terminal commit остаётся IR-7. Startup-wide paused/interrupted/ambiguous
reconstruction/reconciliation остаётся IR-8; полная corruption/restart matrix
`recover_cycle_authority()` остаётся IR-8/IR-10.

IR-1—IR-4 implementation evidence и исторические boundary подробно сохранены в
[`v0.4-input-runtime/README.md`](v0.4-input-runtime/README.md) и
[`v0.4-input-runtime/implementation-sequence.md`](v0.4-input-runtime/implementation-sequence.md).

Этапы IR-6—IR-10 planned. Явно пока не реализованы:

- IR-6 durable `AgentEmission`;
- IR-7 durable finalization barrier;
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
- IR-1—IR-5 уже реализованы; durable semantic emissions, IR-7 barrier, IR-8
  startup recovery, IR-9 completion и IR-10 full acceptance ещё planned.
- `/stop` является cooperative safe-checkpoint pause, paused additions не
  auto-resume runner, `/continue` возобновляет тот же cycle, а `/reset` использует
  durable generation как authority.
- Checkpoint-level stale input/control candidate suppression не является IR-7
  durable finalization barrier; late terminal race остаётся IR-7.
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
