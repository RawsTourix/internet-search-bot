---
id: design.v0.4.file-artifacts-advanced
version: v0.4
spec_status: accepted
implementation_status: partial
last_reviewed: 2026-07-28
---

# v0.4-file-artifacts-advanced

Это самостоятельное именованное обновление между
[`v0.4-file-artifacts`](../v0.4-file-artifacts.md) и
[`v0.4-input-runtime`](../v0.4-input-runtime.md).

Обновление завершает transport-independent контур semantic input/output,
client capabilities, локализации, `OutputBatch`, delivery и artifact policy.
Внутри оно разделено на тематические документы, но в списке версий остаётся
одной рабочей единицей с именем `v0.4-file-artifacts-advanced`.

| Разделы обновления | Документ |
|---|---|
| `AF-1`–`AF-8` | [`semantic-interaction.md`](semantic-interaction.md) |
| `AF-9`–`AF-11` | [`output-delivery.md`](output-delivery.md) |
| `AF-10A` | [`ready-output-outbox.md`](ready-output-outbox.md) |
| `AF-12`–`AF-16` | [`artifact-interaction-policy.md`](artifact-interaction-policy.md) |
| `AF-17`–`AF-20` | [`contracts-and-acceptance.md`](contracts-and-acceptance.md) |
| `AF-21`–`AF-23` | [`implementation.md`](implementation.md) |
| `AF-24` | [`ingress-reservation-hardening.md`](ingress-reservation-hardening.md) |
| `AF-25` | [`ingress-failure-recovery.md`](ingress-failure-recovery.md) |

## Порядок чтения

1. [`semantic-interaction.md`](semantic-interaction.md)
2. [`output-delivery.md`](output-delivery.md)
3. [`ready-output-outbox.md`](ready-output-outbox.md)
4. [`artifact-interaction-policy.md`](artifact-interaction-policy.md)
5. [`contracts-and-acceptance.md`](contracts-and-acceptance.md)
6. [`implementation.md`](implementation.md)
7. [`ingress-reservation-hardening.md`](ingress-reservation-hardening.md)
8. [`ingress-failure-recovery.md`](ingress-failure-recovery.md)

Общий реестр обновлений версии находится в
[`../README.md`](../README.md).

## Статус реализации

Основной контур реализован в filesystem runtime:

- server-owned capability registry и immutable snapshots;
- общая ru/en локализация;
- deterministic response anchor и один presentation handle на InputBatch;
- независимый presentation transport lifecycle с late bind и terminal intent;
- discriminated semantic `InputPart`/`OutputPart`;
- bounded semantic input и отдельный incoming reply provenance;
- bounded input artifact manifest и явный `ArtifactPurpose`;
- стабильный `selection_index` для deliverables;
- commit-once `OutputBatch`, capability renderer и агрегированные receipts;
- authoritative `OutputDeliveryPlan` executor для native Telegram operations;
- atomic aggregate completion delivery records, attempt receipt и OutputBatch;
- отдельное terminal-состояние `unknown` с explicit reconciliation;
- process-restart recovery без автоматического повтора non-idempotent delivery;
- bounded final-only `READY` outbox без повторного agent cycle;
- idempotent claim requests для безопасного повтора потерянного HTTP-ответа;
- exact API-key + `client_instance_id` authority для claim, receipt и bytes;
- immutable per-OutputBatch byte facade без shared mutable claim mapping;
- durable response route/anchor для normal и recovered delivery;
- strict transport и artifact-content evidence перед aggregate completion;
- canonical Telegram composition root `python -m src.servers.telegram`;
- `AF-24` durable ingress reservation: grouping и create/join
  `InputBatchDraft` выполняются в короткой scoped critical section до attachment
  streaming;
- `AF-25` failure recovery: transient Windows metadata publish получает bounded
  retry, permanent post-reservation failure переводит draft в terminal state,
  поздний member exact failed media group получает terminal tombstone;
- `/reset` отменяет open drafts exact session и затем очищает LLM memory;
- shared `API.start` выполняет automatic ingress reconciliation: готовые drafts
  commit-ятся без agent run, остальные open drafts становятся `ABANDONED`, их
  group indexes освобождаются до приёма новых запросов.

`AF-24` подтверждён автоматическими regression tests и live Telegram workflow
2026-07-28: media group из 10 файлов и отдельная поздняя инструкция сформировали
один committed batch с `artifact_count=10`, `text_part_count=1`; был запущен один
agent cycle.

Robustness tests №2–4 затем выявили `AF-25`: transient `WinError 5` при
публикации artifact metadata оставлял zombie draft, который загрязнял следующие
пакеты и создавал ложную grouping ambiguity. Кодовый patch завершён; artifact
suite содержит 156 успешных тестов. Startup tests пересоздают все filesystem
services из того же storage root и подтверждают, что новый альбом с отдельной
инструкцией корректно объединяется после automatic zombie cleanup.

Статус update временно `partial` до полного локального suite и повторного live
Windows-прогона robustness теста №2 без ручного `/reset` и без удаления
`storage`.

Runtime-конфигурация находится в корне
[`src/api/mcp.config.example`](../../../../../src/api/mcp.config.example) в
секциях `client_capabilities`, `localization`, `input_presentation`,
`output_runtime` и `telegram_output`. Отсутствующие секции получают безопасные
defaults, поэтому прежний конфигурационный файл остаётся совместимым.

Точные пути модулей, stores, migrations и тестов перечислены в
[`implementation.md`](implementation.md). Порядок ingress hardening и его
verification evidence определены в
[`ingress-reservation-hardening.md`](ingress-reservation-hardening.md) и
[`ingress-failure-recovery.md`](ingress-failure-recovery.md).
