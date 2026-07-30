---
id: design.v0.4.batch-workflows
version: v0.4
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-07-30
---

# v0.4-batch-workflows

`v0.4-batch-workflows` — самостоятельное именованное обновление между
[`v0.4-file-artifacts-advanced`](../v0.4-file-artifacts-advanced/README.md) и
[`v0.4-input-runtime`](../v0.4-input-runtime.md).

Обновление завершает пользовательские и transport-independent workflows вокруг
уже существующих `InputBatch`, `OutputBatch` и artifact workspace:

- строгая AUTO-политика логического ввода без задержки обычного текста;
- явный режим сборки пакета `/collect → /send | /cancel`;
- безопасное перемещение status/progress presentation после новых сообщений
  пользователя;
- корректная multipart-отправка Telegram media groups;
- стабильная совместимая группировка `OutputPart` без изменения порядка;
- bounded current manifest и явный доступ к session/workspace artifact history;
- явная граница durable commit и in-process agent execution;
- cancellation-safe Gateway/MCP shutdown;
- согласованные aggregate progress events для многофайловых операций.

Обновление не реализует `CycleInbox`, safe checkpoints и additions во время
активного agent cycle. Эти обязанности остаются у следующего
[`v0.4-input-runtime`](../v0.4-input-runtime.md).

## Документы обновления

| Раздел | Документ |
|---|---|
| `BW-1`–`BW-4` | [`input-assembly.md`](input-assembly.md) |
| `BW-2A` | [`forwarded-sequencing-hardening.md`](forwarded-sequencing-hardening.md) |
| `BW-5`–`BW-6` | [`presentation-and-controls.md`](presentation-and-controls.md) |
| `BW-7`–`BW-8` | [`output-grouping.md`](output-grouping.md) |
| `BW-9` | [`artifact-access.md`](artifact-access.md) |
| `BW-10`–`BW-12` | [`contracts-and-acceptance.md`](contracts-and-acceptance.md) |
| `BW-13` | [`shutdown-and-execution.md`](shutdown-and-execution.md) |
| `BW-14` | [`progress-events.md`](progress-events.md) |
| `BW-15` | [`draft-control-foundation.md`](draft-control-foundation.md) |

## Порядок чтения

1. [`input-assembly.md`](input-assembly.md)
2. [`draft-control-foundation.md`](draft-control-foundation.md)
3. [`forwarded-sequencing-hardening.md`](forwarded-sequencing-hardening.md)
4. [`presentation-and-controls.md`](presentation-and-controls.md)
5. [`progress-events.md`](progress-events.md)
6. [`output-grouping.md`](output-grouping.md)
7. [`artifact-access.md`](artifact-access.md)
8. [`contracts-and-acceptance.md`](contracts-and-acceptance.md)
9. [`shutdown-and-execution.md`](shutdown-and-execution.md)

## Главные инварианты

1. Обычный text-only input в AUTO-режиме не ожидает возможных будущих файлов.
2. Автоматическое объединение в обратном порядке не выполняется:
   `text → files` означает два логических ввода, если пользователь заранее не
   включил explicit collection mode.
3. Файл/альбом, пришедший первым, открывает один collecting draft exact scope;
   следующие файлы и сообщения могут войти в него до seal/commit.
4. `/send` коммитит любой непустой explicit draft: text-only, files-only или
   mixed. LLM не обязана получать отдельную «инструкцию», если пользователь явно
   завершил пакет.
5. `/cancel` никогда не запускает agent cycle и не удаляет audit evidence.
6. На exact principal scope допускается не более одного active explicit draft.
7. Пустой explicit collection хранится отдельным `InputCollectionRecord` и не
   ослабляет инвариант `InputBatchDraft.source_event_ids != []`.
8. Presentation relocation сначала создаёт и durable-bind новый handle и только
   потом best-effort удаляет старый. Неудалённый старый handle остаётся архивным
   и больше не редактируется.
9. Output grouping сохраняет `OutputPart.index` и группирует только непрерывные
   совместимые участки.
10. Telegram media upload использует корректное `attach://...` multipart mapping,
    формируемое SDK из raw bytes/file handles, а не вручную собранный неполный
    `InputFile`.
11. Все авторизованные historical artifacts остаются доступны. В prompt
    автоматически попадает только bounded active manifest; история открывается
    через явный catalog/search/get workflow.
12. Delivery выбирает exact immutable `artifact_id`; возраст артефакта сам по
    себе не запрещает повторную отправку.
13. Client adapters вызывают общие draft-control и presentation services; Telegram
    команды не становятся domain logic.
14. Durable commit завершается до AgentCycle. Cancellation agent run не откатывает
    `InputBatch` и не классифицируется как commit failure.
15. MCP transport contexts создаются и закрываются одной lifespan task; authority
    middleware не создаёт скрытый cancellation task layer.
16. Forwarding является provenance, а не attachment type. Только forwarded
    text-only update может кратко ждать более ранний forwarded album того же
    exact scope; обычный текст остаётся немедленным, а смысл текста не
    анализируется.
17. Внешний `duplicate` определяется authoritative первой reservation. Повторный
    внутренний store pass не превращает новый logical input в transport retry.
18. Progress `message` обязан соответствовать scope aggregate operation.
    Structured `data` хранит полный перечень, а человекочитаемая строка использует
    cardinality-aware bounded projection.

## Положение в архитектуре

```text
v0.4-file-artifacts-advanced
→ durable InputBatch/OutputBatch, capabilities, delivery, hardening

v0.4-batch-workflows
→ user-controlled draft assembly, presentation relocation,
  output grouping, artifact activation/catalog scopes,
  staged execution, progress projection and shutdown ownership

v0.4-input-runtime
→ CycleInbox, active-cycle additions, safe checkpoints,
  control inbox and finalization races
```

## Этапы реализации

### BW-P1. Native output correctness

- исправить PTB multipart attachment mapping для document groups;
- добавить request-level и live regression tests;
- сохранить safe individual fallback и exact receipts;
- разделить durable commit и long-running agent execution;
- обеспечить cancellation-safe Gateway/MCP shutdown;
- исправить original idempotency semantics reservation;
- закрыть concurrent forwarded text/media sequencing без задержки обычного текста;
- сделать aggregate artifact delivery progress cardinality-aware и bounded.

### BW-P2. Shared explicit draft control

- durable `InputCollectionRecord` для пустого explicit mode;
- exact `InputDraftScope` и persisted explicit commit policy;
- `InputDraftControlService`;
- `/collect`, `/send`, `/cancel` и aliases;
- один active explicit collection на exact scope;
- text-only/files-only/mixed explicit commit.

### BW-P3. Presentation relocation

- presentation generations;
- create → bind → supersede → best-effort delete;
- restart/replay-safe recovery;
- Telegram UX и локализованные сообщения.

### BW-P4. Artifact access scopes

- bounded current activation set;
- `artifact_list(scope=current|session|workspace)`;
- exact activation provenance;
- historical delivery без скрытого запрета.

### BW-P5. Integration and release gate

- recovery/migration;
- full Windows suite;
- Telegram live scenarios;
- multi-client contract tests;
- documentation и examples синхронизированы с каждым новым параметром.

## Статус

`BW-P1` реализован и подтверждён основными live-сценариями:

- Telegram `sendMediaGroup` доставляет три deliverable одним native album;
- `attach://...` mapping формируется корректно;
- durable commit и AgentCycle разделены transport-stage boundary;
- Gateway lifespan и authority middleware hardened для cancellation;
- первый atomic submit сохраняет `duplicate=False`, настоящий retry —
  `duplicate=True`;
- forwarded text маршрутизируется как text с отдельным provenance;
- forwarded text может bounded-wait более ранний album при конкурентной доставке;
- ordinary text не получает wait;
- mixed-forward live workflow сформировал один batch с `artifact_count=3` и
  `text_part_count=1`;
- aggregate delivery progress корректно отражает один или несколько файлов,
  сохраняя полный structured evidence и bounded preview.

`BW-P2A` foundation реализован:

- persisted empty `InputCollectionRecord`;
- exact scope включает client instance, conversation/thread и principal;
- start/inspect/bind/commit/cancel transport-neutral service;
- files-only explicit commit разрешён;
- in-flight `/send` сохраняет `commit_requested`;
- exact cancel не затрагивает соседние scopes;
- action idempotency key нельзя переиспользовать с другим action/scope;
- active collection и cached action result переживают process restart;
- terminal collection освобождает exact scope для следующего пакета.

Validation evidence на 2026-07-30:

```text
full local Windows suite: 622 tests, OK (skipped=4)
thematic artifact suite: 195 tests, OK
storage suite: 41 tests, OK
plans suite: 45 tests, OK
planning suite: 19 tests, OK
API suite: OK
compile: OK
```

Следующий основной slice — dedicated explicit grouping mode, shared HTTP control
routes и Telegram wiring `/collect`, `/send`, `/cancel`, после чего active
explicit collection начнёт принимать новые transport events без transport quiet
или recovery semantics media groups.

Новый параметр forwarded sequencing slice:

```env
TELEGRAM_FORWARDED_TEXT_JOIN_WAIT_SECONDS="1.5"
```

Он добавлен в `.env.example`; в `mcp.config` новых keys нет. BW-P2A и progress
projection не добавляют новых параметров. Любой следующий параметр `.env` или
`mcp.config` обязан в том же patch обновлять соответствующий `.example`; release
notes отдельно перечисляют новые keys и defaults.
