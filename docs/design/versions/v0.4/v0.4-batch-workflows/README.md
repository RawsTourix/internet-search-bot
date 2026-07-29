---
id: design.v0.4.batch-workflows
version: v0.4
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-07-29
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
- bounded current manifest и явный доступ к session/workspace artifact history.

Обновление не реализует `CycleInbox`, safe checkpoints и additions во время
активного agent cycle. Эти обязанности остаются у следующего
[`v0.4-input-runtime`](../v0.4-input-runtime.md).

## Документы обновления

| Раздел | Документ |
|---|---|
| `BW-1`–`BW-4` | [`input-assembly.md`](input-assembly.md) |
| `BW-5`–`BW-6` | [`presentation-and-controls.md`](presentation-and-controls.md) |
| `BW-7`–`BW-8` | [`output-grouping.md`](output-grouping.md) |
| `BW-9` | [`artifact-access.md`](artifact-access.md) |
| `BW-10`–`BW-12` | [`contracts-and-acceptance.md`](contracts-and-acceptance.md) |

## Порядок чтения

1. [`input-assembly.md`](input-assembly.md)
2. [`presentation-and-controls.md`](presentation-and-controls.md)
3. [`output-grouping.md`](output-grouping.md)
4. [`artifact-access.md`](artifact-access.md)
5. [`contracts-and-acceptance.md`](contracts-and-acceptance.md)

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
7. Presentation relocation сначала создаёт и durable-bind новый handle и только
   потом best-effort удаляет старый. Неудалённый старый handle остаётся архивным
   и больше не редактируется.
8. Output grouping сохраняет `OutputPart.index` и группирует только непрерывные
   совместимые участки.
9. Telegram media upload использует корректное `attach://...` multipart mapping,
   формируемое SDK из raw bytes/file handles, а не вручную собранный неполный
   `InputFile`.
10. Все авторизованные historical artifacts остаются доступны. В prompt
    автоматически попадает только bounded active manifest; история открывается
    через явный catalog/search/get workflow.
11. Delivery выбирает exact immutable `artifact_id`; возраст артефакта сам по
    себе не запрещает повторную отправку.
12. Client adapters вызывают общие draft-control и presentation services; Telegram
    команды не становятся domain logic.

## Положение в архитектуре

```text
v0.4-file-artifacts-advanced
→ durable InputBatch/OutputBatch, capabilities, delivery, hardening

v0.4-batch-workflows
→ user-controlled draft assembly, presentation relocation,
  output grouping, artifact activation/catalog scopes

v0.4-input-runtime
→ CycleInbox, active-cycle additions, safe checkpoints,
  control inbox and finalization races
```

## Этапы реализации

### BW-P1. Native output correctness

- исправить PTB multipart attachment mapping для document groups;
- добавить request-level и live regression tests;
- сохранить safe individual fallback и exact receipts.

### BW-P2. Shared explicit draft control

- domain enums и persisted draft policy;
- `InputDraftControlService`;
- `/collect`, `/send`, `/cancel` и aliases;
- один active explicit draft на exact scope;
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
- документация и examples синхронизированы с каждым новым параметром.

## Статус

Спецификация принята. Реализация начата с `BW-P1`: устранения текущей ошибки
Telegram `sendMediaGroup` вида `Can't parse inputmedia: media not found`.

Любой новый параметр `.env` или `mcp.config` в рамках этого обновления обязан в
том же patch обновлять соответствующий `.example`; release notes отдельно
перечисляют новые keys и defaults.
