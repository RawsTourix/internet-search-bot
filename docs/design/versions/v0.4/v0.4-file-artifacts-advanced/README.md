---
id: design.v0.4.file-artifacts-advanced
version: v0.4
spec_status: accepted
implementation_status: implemented
---

# v0.4-file-artifacts-advanced

Это самостоятельное именованное обновление между
[`v0.4-file-artifacts`](../v0.4-file-artifacts.md) и
[`v0.4-input-runtime`](../v0.4-input-runtime.md).

Обновление завершает transport-independent контур semantic input/output,
client capabilities, локализации, `OutputBatch`, delivery и artifact policy.
Внутри оно разделено на пять тематических документов, но в списке версий
остаётся одной рабочей единицей с именем `v0.4-file-artifacts-advanced`.

| Разделы обновления | Документ |
|---|---|
| `AF-1`–`AF-8` | [`semantic-interaction.md`](semantic-interaction.md) |
| `AF-9`–`AF-11` | [`output-delivery.md`](output-delivery.md) |
| `AF-12`–`AF-16` | [`artifact-interaction-policy.md`](artifact-interaction-policy.md) |
| `AF-17`–`AF-20` | [`contracts-and-acceptance.md`](contracts-and-acceptance.md) |
| `AF-21`–`AF-23` | [`implementation.md`](implementation.md) |

## Порядок чтения

1. [`semantic-interaction.md`](semantic-interaction.md)
2. [`output-delivery.md`](output-delivery.md)
3. [`artifact-interaction-policy.md`](artifact-interaction-policy.md)
4. [`contracts-and-acceptance.md`](contracts-and-acceptance.md)
5. [`implementation.md`](implementation.md)

Общий реестр обновлений версии находится в
[`../README.md`](../README.md).

## Статус реализации

Контур реализован в filesystem runtime:

- server-owned capability registry и immutable snapshots;
- общая ru/en локализация;
- deterministic response anchor и один presentation handle на InputBatch;
- discriminated semantic `InputPart`/`OutputPart`;
- bounded input artifact manifest и явный `ArtifactPurpose`;
- стабильный `selection_index` для deliverables;
- commit-once `OutputBatch`, capability renderer и агрегированные receipts;
- Telegram document groups с сохранением порядка и conservative `unknown`;
- process-restart recovery без автоматического повтора non-idempotent delivery.

Runtime-конфигурация находится в корне
[`src/api/mcp.config.example`](../../../../../src/api/mcp.config.example) в
секциях `client_capabilities`, `localization`, `input_presentation`,
`output_runtime` и `telegram_output`. Отсутствующие секции получают безопасные
defaults, поэтому прежний конфигурационный файл остаётся совместимым.

Точные пути модулей, stores, migrations и тестов перечислены в
[`implementation.md`](implementation.md).
