---
id: design.current
version: cross-version
spec_status: accepted
implementation_status: mixed
last_reviewed: 2026-08-07
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
- `v0.4-batch-workflows`;
- `v0.4-input-runtime` — partial update, в котором IR-1—IR-5 уже implemented.

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
- сам `v0.4-batch-workflows` заканчивается до durable active-cycle additions;
  текущий `v0.4-input-runtime` IR-3/IR-4 уже добавляет `CycleInbox` и common
  checkpoint apply поверх этого boundary;
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

В `v0.4-input-runtime` реализованы и подтверждены CI этапы IR-1—IR-5.
Общий update остаётся `partial`: IR-6—IR-10 planned.

Domain/config/ports и durable filesystem repositories подключены в production
composition через `InputAdmissionService`.

Каждый immutable `CommittedInputBatch` проходит общий admission boundary.
Initial input получает exact cycle identity, назначенную service, и запускает
один runner. Addition во время `running` получает durable admission, monotonic
session/cycle sequence и FIFO `CycleInboxItem` того же cycle; transport сразу
возвращает structured acknowledgement, а второй `MCPClient.process_query()` не
запускается. `SessionExecutionCoordinator` остаётся defensive in-process lease,
wakeup/generation cache и diagnostics, но не durable queue.

IR-3 hardening закрепил:

- authoritative count/byte reservation по pending/admitted admissions активного
  cycle даже при crash между admission и inbox publication;
- exact-cycle wake fencing: late wake старого cycle не изменяет event нового;
- durable runtime handoff boundary
  `pre-run setup → RuntimeHandoffRecord → process_query()` без blind replay;
- явную обработку `asyncio.CancelledError` для initial runner и временного
  `WAITING_USER` compatibility path;
- отдельную cleanup task, ожидание через `asyncio.shield` и завершение durable
  cleanup даже при повторной cancellation;
- pre-handoff cancellation остаётся retryable, post-handoff cancellation
  переводит marker в `AMBIGUOUS`, а session cycle — в `interrupted`;
- WAITING claim до marker requeue-ится, после marker остаётся durable evidence и
  не requeue-ится для автоматического повторного runtime invocation;
- storage-neutral `RuntimeHandoffRepository` port. Application service получает
  готовый repository; filesystem adapter собирается только infrastructure
  factory/bundle и переживает recreation поверх того же storage root;
- stale handoff token не завершает другую attempt, terminal timestamps не могут
  предшествовать `handed_off_at`.

Duplicate replay возвращает существующую admission/inbox relation и после
handoff не запускает runner повторно. Count/byte capacity block является typed и
retryable; committed batch сохраняется для повторного admission. Record-first
crash windows восстанавливают missing inbox без новой sequence или нового cycle.
`interrupted` не запускает unsafe automatic replay.

IR-3 final code evidence:

- основной cancellation-safe/storage-neutral commit:
  `e8192380cc3104668ea9b0f3f017d3c962fd65e4`;
- узкий IR-3 CI fixture fix:
  `c36e4cc38095e15f54f63ae81c29b4829defec1f`;
- `Validate Input Runtime` #84 — success, `198 passed`;
- `Validate v0.4 file artifacts PR` #503 — success;
- deterministic no-parallel contract: три additions, один target cycle,
  `process_query call count == 1`.

IR-4 добавил active-context apply поверх IR-3 admission/handoff boundary:

- initial `CP-RESUME` до первого main LLM/result создаёт initial `R1` и durable
  `ActiveCycleSnapshot`;
- каждый applied range создаёт следующую linear `CycleContextRevision`;
- additions drain-ятся bounded contiguous FIFO ranges по `cycle_sequence`;
- checkpoint фиксирует accepted-at-entry watermark и применяет только input,
  accepted к моменту входа в эту semantic boundary;
- каждый applied range создаёт ровно один runtime-owned `input_batch_update`;
- protocol-safe checkpoint matrix охватывает resume/create, before-LLM,
  after-tool-block, before-WAITING, before-final-processing,
  before-terminal-commit и controlled interruption boundaries;
- WAITING reply проходит общий FIFO `CP-RESUME`, не обходит более ранние queued
  additions и больше не владеет legacy semantic path; initial
  `original_input_batch_id` сохраняется;
- snapshot-first crash reconciliation использует persisted snapshot watermark как
  authority и домаркировывает inbox/admission без duplicate update/revision;
- claim acquisition/apply cancellation-safe;
- successful runtime handoff completion предшествует terminal snapshot sync;
- stale WAITING/final candidates подавляются на checkpoint-level, если accepted
  watermark уже опережает applied context.

IR-4 final code evidence:

- code/test HEAD:
  `1d31b6fbd1d5e88966d3964dc35cf4680f32f522`;
- `Validate Input Runtime` #115 — success, compile success, `241 passed`,
  `0 failed`;
- `Validate v0.4 file artifacts PR` #518 — success, validation suites и status
  enforcement success;
- regression-fix проход после `224911a…` был test-only; production code не
  менялся.

IR-5 добавил durable control plane поверх IR-1/IR-4 foundation:

- transport-neutral `InputRuntimeControlService` принимает pause/continue/reset и
  возвращает structured `ControlOutcome`;
- control sequence выделяется под короткой session coordination, stable
  idempotency сохраняет один logical command/sequence и repair-ит record-first
  publication crash window;
- `pending_control_sequence` и `applied_control_sequence` стали real monotonic
  authority: applied watermark продвигается только contiguous terminal records;
- control-aware checkpoint фиксирует pending-control watermark на entry и reduce-ит
  commands до ordinary input;
- `/stop` cooperative: bounded LLM attempt завершается, а production multi-tool
  assistant block сохраняет все matching tool results до pause; durable snapshot
  становится `paused_by_user` и compatibility result mapping не стирает pause;
- `pause_requested/paused_by_user` additions admitted FIFO как `QUEUE_PAUSED`
  без runner start/wake;
- `/continue` возобновляет тот же cycle; accepted input target замораживается
  атомарно внутри shared durable `root identity → session` coordination. Input,
  coordinated раньше continue, входит в initial `CP-RESUME` drain; input,
  coordinated позже, остаётся future running checkpoint;
- duplicate continue сохраняет тот же control ID/sequence/frozen target, а
  record-first continue publication crash repair-ит pending watermark без
  пересчёта target или duplicate sequence;
- continue without additions сохраняет original input/context revision;
- `WAITING_USER` без нового ответа не превращает `/continue` в fake user reply;
- rapid pause/continue reducer не создаёт phantom pause/second runner;
- `/reset` использует durable generation как authority, повышает её ровно один
  раз на logical command, fences/cancels old-generation work и clear-ит shared
  memory только после execution lease boundary;
- Telegram `/stop`/`/continue` используют exact existing session/thread identity,
  stable source command identity и общий Gateway/application control service;
  `/cancel` остаётся ingress-only.

IR-5 final code evidence:

- corrected code/test HEAD:
  `0fabe15c6730a4e8db6be8b54ecec2c13ea773c7`;
- `Validate Input Runtime` #219 — success, compile success, `291 passed`,
  `0 failed`, `0 skipped`;
- `Validate v0.4 file artifacts PR` #570 — success.

Граница current implementation остаётся точной. IR-5 checkpoint-level
pause/reset suppression не закрывает late terminal race после последнего recheck
и до durable terminal commit — это IR-7. Startup reconstruction/reconciliation
paused/interrupted/ambiguous runtime state — IR-8. Полная
`recover_cycle_authority()` corruption matrix — IR-8/IR-10.

Пока **не реализованы**:

- IR-6 durable `AgentEmission`;
- IR-7 durable finalization barrier;
- IR-8 startup recovery/reconstruction;
- IR-9 complete client projections/diagnostics/config examples;
- IR-10 full race/restart/synthetic/live acceptance;
- scheduler и parallel branches/fork-join semantics;
- Telegram history rewind по edited message.

Принятый target при этом не сокращается и включает:

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

Текущий in-process Telegram FIFO dispatcher не подменяет input runtime и не
переживает restart. Telegram runtime-control handler использует тот же exact
session/thread resolution и Gateway boundary, но startup reconstruction active
cycle всё равно остаётся IR-8. Специальный IR-3 WAITING continuation больше не
владеет semantic reply path: IR-4 применяет WAITING reply через общий FIFO
`CP-RESUME`.

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
5. считайте IR-1—IR-5 `v0.4-input-runtime` реализованными: каждый committed batch
   admitted, initial batch запускает один cycle, running additions durable queued
   и применяются в том же cycle через protocol-safe bounded FIFO checkpoints,
   initial `R1`/snapshot authoritative, runtime handoff cancellation-safe и
   storage-neutral, а durable control plane управляет pause/continue/reset через
   control watermarks и generation fencing;
6. учитывайте WAITING reply как common FIFO `CP-RESUME` continuation без legacy
   semantic ownership и без подмены initial input identity; `/continue` без
   ответа не является WAITING reply;
7. считайте `/stop` cooperative safe-checkpoint pause, paused additions — durable
   FIFO без auto-resume, `/continue` — same-cycle resume с atomically frozen
   durable coordination target, а `/reset` — durable-generation authority;
8. не приписывайте durable `AgentEmission`, IR-7 finalization barrier, IR-8
   startup reconstruction, IR-9 completion или IR-10 roast текущему baseline;
9. не считайте checkpoint-level input/control suppression закрытием late terminal
   race: durable barrier остаётся IR-7;
10. не приписывайте startup reconciliation ambiguous handoff/claim/control к
    IR-5 — это ответственность IR-8; `recover_cycle_authority()` corruption
    matrix остаётся IR-8/IR-10;
11. не приписывайте scheduler/parallel branches текущему input runtime;
12. не приписывайте Telegram history rewind текущему baseline;
13. не приписывайте `AgentRuntime`/Dispatcher/Service composition до modularization;
14. не приписывайте scopes, trusted presentation, admission и remote handle
    lifecycle до `v0.4-mcp-registry-foundation`;
15. не называйте текущий self-hosted Service Application Future Local Agent;
16. проверяйте затронутый код и tests для точного implementation status;
17. используйте v0.5–v0.10 только как будущие ограничения;
18. не смешивайте `AgentCycle`, будущий `AgentRun` и `TaskRun`;
19. не смешивайте sandbox execution backend и Future Local Agent Application.
