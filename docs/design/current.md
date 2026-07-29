---
id: design.current
version: cross-version
spec_status: accepted
implementation_status: mixed
last_reviewed: 2026-07-29
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
- `v0.4-file-artifacts`.

`v0.4-file-artifacts-advanced` временно имеет статус `partial` из-за live gate
для `AF-25` и `AF-26`.

`AF-24` durable reservation подтверждён live Telegram workflow 2026-07-28:
media group из 10 файлов и отдельная инструкция сформировали один
`CommittedInputBatch` с `artifact_count=10`, `text_part_count=1` и одним agent
cycle.

Robustness tests выявили два следующих failure-класса.

### AF-25 — durable failure recovery

Transient Windows `PermissionError` при публикации artifact metadata мог
оставить open zombie draft. Следующие инструкции присоединялись к нему либо
получали ложную `InputGroupingAmbiguityError`; `/reset` и process restart не
очищали durable ingress полностью.

`AF-25` добавляет:

- bounded retry только для immutable metadata publish;
- terminal failure для reserved draft;
- запрет resurrection FAILED/CANCELLED/ABANDONED drafts;
- exact failed-group tombstone;
- session-level отмену open inputs через `/reset`;
- automatic startup reconciliation: ready drafts commit-ятся без agent run,
  остальные open drafts становятся `ABANDONED` и исключаются из grouping.

### AF-26 — Telegram/output robustness

Даже после reservation hardening отдельный text update мог войти в Gateway на
несколько миллисекунд раньше первого file request. Native document group
стабильно уходил в fallback без точного Telegram error; старые legacy READY не
имели claimable instance authority; terminal network error могла остаться
невидимой пользователю.

`AF-26` добавляет:

- exact active-album sequencing в instance-scoped Telegram bridge до Gateway;
- bounded join window, exact bot/chat/thread scope и explicit ambiguity для двух
  active albums;
- streaming document group, bounded eager retry после known-unsent BadRequest,
  exact diagnostic log и safe individual fallback;
- startup cancellation только explicit legacy-sentinel READY с сохранением
  audit history и логированием authority остальных batches;
- deterministic edit известного status message после exhausted send-new timeout;
- базовые system-prompt rules для explicit `format_id` и проверки returned
  artifact metadata без runtime-эвристики по расширению.

Автоматический validation suite успешен: artifact suite содержит **165 тестов**,
включая конкурентный text submit при заблокированном file HTTP request,
document-group representation paths, output authority reconciliation, terminal
status fallback и prompt-contract.

До статуса `implemented` остаются полный локальный suite и live Windows-проверка
AF-25/AF-26 без ручного cleanup или удаления `storage`.

Следующий основной функциональный этап после закрытия этого gate:

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
3. учитывайте `AF-24` как реализованный reservation hardening;
4. учитывайте `AF-25` и `AF-26` как code-complete, live-pending hardening;
5. проверяйте затронутый код и тесты для точного implementation status;
6. используйте незавершённый v0.4 и v0.5–v0.10 только как будущие ограничения;
7. не смешивайте `AgentCycle`, будущий `AgentRun` и `TaskRun` в одну сущность.
