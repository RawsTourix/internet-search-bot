---
id: design.v0.4.index
version: v0.4
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-08-05
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
| 8 | [`v0.4-input-runtime`](v0.4-input-runtime/README.md) | partial | IR-1/IR-2 implemented; IR-3—IR-10 planned: production admission, active-cycle apply, `/stop`/`/continue`, emissions и finalization barrier |
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

`v0.4-input-runtime` остаётся update со статусом `partial`. IR-1 и IR-2
реализованы и подтверждены CI: domain/config/ports foundation, durable filesystem
repositories, bounded coordination, atomic writes, sequence repair, claims,
recoverable indexes и global identity fencing. Этапы IR-3—IR-10, включая
production admission, checkpoints, controls, emissions, finalization и recovery
lifecycle, остаются planned. Observable production runtime behaviour пока не
изменён.

Целевой package вводит durable admission, additions в один active AgentCycle,
safe checkpoints, линейные context revisions, `/stop`/`/continue`, durable
intermediate messages, stale-finalization barrier и filesystem recovery ports с
заделом на PostgreSQL `v0.5` и scheduler/interventions `v0.6`.

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
