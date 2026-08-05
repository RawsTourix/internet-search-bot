---
id: design.current
version: cross-version
spec_status: accepted
implementation_status: mixed
last_reviewed: 2026-08-05
---

# Текущий архитектурный baseline

Этот файл определяет, какие версии следует применять при анализе текущего
проекта. Release-решение дополнительно проверяется по коду, tests и live gates.

Application/hosting profiles и будущая граница Local Agent определены в
[`runtime-and-deployment-profiles.md`](runtime-and-deployment-profiles.md).

## Текущий application profile

Текущий проект является Service Application в single-process self-hosted
разработке. Telegram и будущие Web/network clients обращаются к server-side
runtime через Gateway/application boundary.

```text
application profile = Service Application
hosting mode = self-hosted
topology = single-process
environment = development
```

Это не Future Local Agent Application. Отдельный local executable profile пока
не реализован и не используется как описание текущего поведения.

## Реализованный baseline

`v0.3` остаётся реализованной основой:

- JSON-протокол `AgentAction`;
- dialog memory, LLM context и cycle trace;
- `pending_cycle` и resumable cycle;
- progress events;
- lifecycle-aware MCP Server Manager;
- final processing pipeline.

Канонический индекс: [`versions/v0.3/README.md`](versions/v0.3/README.md).

## Активное развитие v0.4

`v0.4` является принятой архитектурой agent workspace.

Реализованы либо доведены до устойчивого filesystem runtime:

- `v0.4-storage-foundation`;
- `v0.4-result-compaction`;
- `v0.4-cycle-compaction`;
- `v0.4-dag-planning`;
- `v0.4-file-artifacts`;
- `v0.4-file-artifacts-advanced`;
- `v0.4-batch-workflows`.

`v0.4-file-artifacts-advanced` завершил automated и maintainer live acceptance.
Durable reservation, recovery, forwarded sequencing, native Telegram document
albums, delivery receipts и commit/run boundary реализованы, покрыты regression
suites и подтверждены финальной прожаркой.

## v0.4-batch-workflows

Update реализован и завершил client-facing workflow до admission в AgentCycle:

- AUTO text-only input запускается без artificial delay и получает user-facing
  initial status `Сообщение принято. Обрабатываю…`;
- files-first открывает durable draft;
- explicit `/collect → /send | /cancel` поддерживает text-first/files-first/mixed;
- `/batch` и `/done` отсутствуют как дублирующие aliases;
- shared HTTP control plane проверяет exact client-instance authority;
- persisted canonical grouping mode — `explicit_collection`;
- rollout-era `immediate_text` drafts/indexes migration-ятся при reconcile;
- active-collection presentation relocation использует generations и
  `create → durable bind → supersede → best-effort delete`;
- collection snapshot после `/send`/`/cancel` остаётся terminal audit evidence и не
  удаляется ради запуска;
- AgentCycle progress получает отдельный run status через execution-scoped
  non-persisted metadata overlay;
- explicit `/send` передаёт exact run status напрямую;
- committed AUTO text, чей status создаётся после ingress, использует bounded
  one-shot binding `input_batch_id → run progress metadata`, потребляемый exact
  `/run` один раз;
- durable `InputBatch.response_route` не мутируется ради UI presentation;
- receipt-driven finalization переводит tracked status из `result_ready` в
  terminal delivery state; подтверждённый `delivered` становится
  `✅ Задача завершена.`;
- отдельное сообщение `Готово.` управляется
  `TELEGRAM_FINAL_STATUS_MODE=always|artefacts_only|never`, default —
  `artefacts_only`;
- obsolete progress redirect registry удалён;
- exact Telegram conversation/thread использует один FIFO dispatcher на входе
  `Application.process_update`, а разные sessions остаются параллельными;
- `/collect` упаковывает пользовательский ввод и не является reset-командой;
- committed package продолжает suspended `WAITING_USER` cycle, сохраняя его
  messages, working memory и artifact refs;
- additions во время реально выполняющегося cycle всё ещё требуют будущий
  `CycleInbox`;
- late album callback после `/send` или `/cancel` подавляется terminal tombstone;
- последний bounded набор artifact refs завершённого cycle наследуется следующим
  cycle той же session;
- session handoff не пересекает session boundary и очищается вместе с session при
  `/reset`;
- более старая история доступна через
  `artifact_list(scope="session|workspace")` и explicit activation;
- output grouping и native Telegram multipart delivery сохраняют порядок.

Thematic CI закрывающего PR проверяет:

```text
compile
artifact suite
storage suite
plans suite
planning suite
API suite
```

Финальная acceptance завершена 2026-08-04:

- полный Windows baseline: `775 passed, 4 skipped, 0 failed`;
- synthetic Web/Telegram/artifact roast: `5 062 passed, 1 skipped, 0 failed,
  0 flaky`;
- `RACE-001`: `1 300/1 300 passed`;
- `RACE-002`: `3 462/3 462 passed`;
- Telegram audit matrix: `104 passed`;
- maintainer live Telegram scenarios подтвердили AUTO/EXPLICIT workflows,
  authoritative presentation, restart/recovery, WAITING_USER continuation,
  same-session artifact handoff, `/reset`, FIFO commands и cancellation во время
  album settling;
- synthetic audit не вызвал AgentRuntime, LLM, MCP, внешнюю сеть или реальный
  Telegram.

Точные метрики, coverage gaps и provenance запуска находятся в
[`../../reports/v0.4-transport-artifact-roast.md`](../../reports/v0.4-transport-artifact-roast.md)
и описании PR. Зафиксированные gaps относятся к отсутствию некоторых единых
synthetic seams и не являются обнаруженными production defects.

Канонический документ:
[`versions/v0.4/v0.4-batch-workflows/README.md`](versions/v0.4/v0.4-batch-workflows/README.md).

## Следующие этапы v0.4

```text
v0.4-input-runtime
→ v0.4-runtime-modularization
→ v0.4-mcp-registry-foundation
```

В `v0.4-input-runtime` реализован и подтверждён CI только foundation-этап IR-1:
domain models, enums, configuration и repository ports. Admission, filesystem
stores, checkpoints, `/stop`/`/continue`, emissions и finalization ещё не
подключены. Поэтому новое observable production behaviour этому update пока не
приписывается.

Принятый target включает:

- durable admission committed batches и `CycleInbox`;
- additions в один active AgentCycle;
- safe checkpoints и ordered `input_batch_update`;
- linear context revisions;
- `/stop`, `/continue` и generation-fenced `/reset`;
- additions during pause без automatic resume;
- durable intermediate AgentEmission;
- stale `DONE`/`WAITING_USER` suppression;
- finalization barrier и filesystem recovery;
- repository ports для PostgreSQL v0.5;
- identity/relations для interventions, task branches и scheduler v0.6.

Текущий in-process Telegram FIFO dispatcher не подменяет этот runtime и не
переживает restart. Текущий специальный WAITING_USER continuation является
переходным foundation, а не общим active-cycle admission.

Каноническая спецификация:
[`versions/v0.4/v0.4-input-runtime/README.md`](versions/v0.4/v0.4-input-runtime/README.md).

Пошаговая реализация:
[`versions/v0.4/v0.4-input-runtime/implementation-sequence.md`](versions/v0.4/v0.4-input-runtime/implementation-sequence.md).

Telegram history rewind по edited message зафиксирован как provisional/deferred
client-specific follow-up и не входит в текущий release gate.

`v0.4-runtime-modularization` декомпозирует orchestration core без изменения
принятых контрактов, вводит переиспользуемый `AgentRuntime`, `ToolDispatcher`,
независимый MCP runtime, composition ports, `ConfigProvider` и явный Service
Application composition root.

Future Local Agent Application этим update не реализуется. Модульные границы
должны позволить позднее создать отдельный local composition root без fork или
rewrite agent loop.

`v0.4-mcp-registry-foundation` затем добавит локальный config-backed registry со
scopes `builtin`, `instance`, `user`, `session`, trusted presentation/retry
metadata, lifecycle ownership opaque remote handles и profile-aware transport
admission.

Общий MCP runtime сохраняет Streamable HTTP и stdio/executable adapters. Новые
builtin definitions Service Application используют Streamable HTTP. Service не
запускает user/session-provided executable MCP; self-hosted operator-managed
instance stdio может быть разрешён только явной deployment policy.

Конкретные builtin MCP-сервисы и их внутренняя реализация в текущем baseline
отсутствуют.

Контракт внешней границы:
[`contracts/builtin-mcp-service-contract.md`](contracts/builtin-mcp-service-contract.md).

Канонический индекс v0.4: [`versions/v0.4/README.md`](versions/v0.4/README.md).

## Будущие версии

| Версия | Роль |
|---|---|
| `v0.5` | PostgreSQL, lazy indexing, embeddings и RAG |
| `v0.6` | AgentRun/TaskRun, workers, queues, workflow orchestration и distributed registry |
| `v0.7` | Skills и extension platform |
| `v0.8` | Identity, authorization и multi-user Service Application workspace |
| `v0.9` | Single-node isolated execution через sandbox backend |
| `v0.10` | Distributed execution plane и runner fleet |

Future Local Agent Application пока не имеет назначенного номера версии. Он
остаётся будущим отдельным application profile поверх стабилизированного
AgentRuntime.

Будущая версия не должна использоваться как описание текущего поведения без code,
test или migration evidence.

## Правило анализа

1. используйте v0.3 как реализованный baseline;
2. применяйте отмеченные реализованные updates v0.4;
3. учитывайте `AF-24`–`AF-26` как implemented и accepted;
4. учитывайте `v0.4-batch-workflows` как implemented и accepted;
5. считайте IR-1 `v0.4-input-runtime` реализованным foundation, но не
   приписывайте runtime behaviour этапов IR-2—IR-10;
6. не приписывайте durable active-cycle additions, `/stop`/`/continue` и
   intermediate emissions текущему `feature` до реализации;
7. не приписывайте `AgentRuntime`/Dispatcher/Service composition до modularization;
8. не приписывайте scopes, trusted presentation, admission и remote handle
   lifecycle до `v0.4-mcp-registry-foundation`;
9. не называйте текущий self-hosted Service Application Future Local Agent;
10. проверяйте затронутый код и tests для точного implementation status;
11. используйте v0.5–v0.10 только как будущие ограничения;
12. не смешивайте `AgentCycle`, будущий `AgentRun` и `TaskRun`;
13. не смешивайте sandbox execution backend и Future Local Agent Application.
