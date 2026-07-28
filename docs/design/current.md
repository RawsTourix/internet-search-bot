---
id: design.current
version: cross-version
spec_status: accepted
implementation_status: mixed
last_reviewed: 2026-07-28
---

# Текущий архитектурный baseline

Этот файл определяет, какие версии следует применять при анализе текущего
проекта. Статус конкретного обновления перед release-решением дополнительно
проверяется по коду и тестам.

## Реализованный baseline

`v0.3` является реализованной основой:

- JSON-протокол `AgentAction`;
- разделение dialog memory, LLM context и cycle trace;
- `pending_cycle` и resumable cycle;
- progress events;
- lifecycle-aware MCP Server Manager;
- final processing pipeline.

Канонический индекс: [`versions/v0.3/README.md`](versions/v0.3/README.md).

## Активное развитие v0.4

`v0.4` является принятой архитектурой agent workspace.

По документации и текущему коду реализованы либо доведены до устойчивого
filesystem runtime:

- `v0.4-storage-foundation`;
- `v0.4-result-compaction`;
- `v0.4-cycle-compaction`;
- `v0.4-dag-planning`;
- `v0.4-file-artifacts`;
- `v0.4-file-artifacts-advanced`, включая `AF-24` durable ingress reservation
  hardening.

`AF-24` прошёл автоматические regression suites и повторный реальный Telegram
workflow 2026-07-28: media group из 10 файлов и отдельная поздняя инструкция были
собраны в один `CommittedInputBatch` с `artifact_count=10`, `text_part_count=1`
и одним agent cycle. Полный локальный прогон завершён успешно: 591 test,
`skipped=4`.

Следующий основной функциональный этап:

```text
v0.4-input-runtime
```

После его завершения запланирован архитектурный этап:

```text
v0.4-runtime-modularization
```

Он декомпозирует центральный orchestration core без изменения принятого
поведения v0.4 и подготавливает ports/repositories/composition root для v0.5 и
v0.6.

Канонический индекс: [`versions/v0.4/README.md`](versions/v0.4/README.md).

## Будущие версии

| Версия | Роль |
|---|---|
| `v0.5` | PostgreSQL, lazy indexing, embeddings и RAG |
| `v0.6` | AgentRun/TaskRun, workers, queues и workflow orchestration |
| `v0.7` | Skills и extension platform |
| `v0.8` | Identity, authorization и multi-user workspace |
| `v0.9` | Single-node isolated execution через sandbox backend |
| `v0.10` | Distributed execution plane и runner fleet |

Будущая версия не должна использоваться как описание текущего поведения, если
это явно не подтверждено кодом, тестами или соответствующей migration.

## Правило анализа

Для вопроса о текущем поведении:

1. используйте v0.3 как реализованный baseline;
2. применяйте отмеченные реализованные updates v0.4;
3. учитывайте `AF-24` как реализованный filesystem hardening shared ingress;
4. проверяйте затронутый код и тесты для точного implementation status;
5. используйте незавершённый v0.4 и v0.5–v0.10 только как будущие ограничения;
6. не смешивайте `AgentCycle`, будущий `AgentRun` и `TaskRun` в одну сущность.