---
id: design.v0.4.index
version: v0.4
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-07-29
---

# v0.4 — реестр обновлений Agent Workspace

`v0.4` создаёт agent workspace с внешним хранением полного content,
управляемым LLM-контекстом, optional DAG, атомарным input, доставкой артефактов
и устойчивым модульным runtime.

Основная рабочая единица документации — именованное обновление. Крупное
обновление может быть папкой с собственным README и внутренними разделами.

## Именованные обновления

| Порядок | Обновление | Статус | Результат |
|---:|---|---|---|
| 1 | [`v0.4-storage-foundation`](v0.4-storage-foundation.md) | implemented | `ContentStore`, `ArtifactStore`, refs и filesystem backend |
| 2 | [`v0.4-result-compaction`](v0.4-result-compaction.md) | implemented | Сохранение и компактное представление tool results |
| 3 | [`v0.4-cycle-compaction`](v0.4-cycle-compaction.md) | implemented | `CycleWorkingMemory` и compaction закрытых segments |
| 4 | [`v0.4-dag-planning`](v0.4-dag-planning.md) | implemented | Optional runtime-owned DAG без scheduler |
| 5 | [`v0.4-file-artifacts`](v0.4-file-artifacts.md) | implemented | Artifact identity, versions, manager tools и delivery foundation |
| 6 | [`v0.4-file-artifacts-advanced`](v0.4-file-artifacts-advanced/README.md) | partial (`AF-25`/`AF-26` live gate) | Semantic input/output, capabilities, localization, `OutputBatch` и durable Telegram/file recovery |
| 7 | [`v0.4-input-runtime`](v0.4-input-runtime.md) | partial/planned | `CycleInbox`, safe checkpoints и active-cycle input |
| 8 | [`v0.4-runtime-modularization`](v0.4-runtime-modularization/README.md) | planned | Декомпозиция orchestration core и подготовка ports для v0.5–v0.6 |

`AF-24` порядка `grouping → durable InputBatchDraft → streaming` реализован и
подтверждён live Telegram workflow.

Robustness tests выявили два follow-up hardening-этапа:

- `AF-25`: terminal failure, failed-group tombstone, `/reset` и automatic
  process-restart reconciliation для open drafts;
- `AF-26`: exact active-album sequencing до Gateway, native document-group
  diagnostics/retry, legacy READY authority cleanup, terminal status fallback и
  базовый prompt contract для явного artifact format.

Кодовые patches и automatic regression suites завершены успешно:

- runtime storage/integrity failure переводит draft в `FAILED`;
- ready drafts после restart commit-ятся без agent run, остальные становятся
  `ABANDONED` до приёма новых запросов;
- text при заблокированном file HTTP request получает exact album group key;
- два active albums дают explicit ambiguity вместо guessing;
- document group проверяется через streaming, bounded eager retry и safe fallback;
- legacy sentinel READY становится `CANCELLED`, valid READY сохраняет authority;
- terminal send timeout обновляет exact status message;
- runtime не определяет `format_id` по расширению;
- artifact suite содержит **165 успешных тестов**.

Статус возвращается в `implemented` после полного локального suite и live
Windows-проверки AF-25/AF-26 без ручного `/reset`, удаления `storage`, files-only
cycle, ложной ambiguity и необъяснимого document-group fallback.

Статус является навигационным и перед release проверяется по коду и тестам.

## Связующие документы версии

| Документ | Назначение |
|---|---|
| [`overview.md`](overview.md) | Цель и граница v0.4 |
| [`v0.4-unified-input-artifact-architecture.md`](v0.4-unified-input-artifact-architecture.md) | Сквозная связь ingress, input batches, artifacts и runtime |
| [`v0.4-release-plan.md`](v0.4-release-plan.md) | Порядок реализации и общие acceptance criteria |

Связующие документы не являются дополнительными релизными обновлениями и не
меняют порядок таблицы выше.

## Зависимости обновлений

```text
v0.4-storage-foundation
├── v0.4-result-compaction
├── v0.4-cycle-compaction
├── v0.4-dag-planning
└── v0.4-file-artifacts
    └── v0.4-file-artifacts-advanced
        └── v0.4-input-runtime
            └── v0.4-runtime-modularization
```

## Как читать

- Для последовательного проектирования идите по реестру сверху вниз.
- Для конкретного патча открывайте документ с тем же именем, что и обновление.
- Для крупного update сначала открывайте README его папки.
- `v0.4-runtime-modularization` не добавляет новый product feature: он сохраняет
  принятые contracts и меняет ownership/границы реализации.

PostgreSQL/RAG начинаются в [`../v0.5/README.md`](../v0.5/README.md), workers и
distributed orchestration — в [`../v0.6/README.md`](../v0.6/README.md).
