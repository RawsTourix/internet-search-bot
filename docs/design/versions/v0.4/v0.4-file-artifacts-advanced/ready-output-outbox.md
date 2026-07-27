---
id: design.v0.4.ready-output-outbox
version: v0.4
spec_status: accepted
implementation_status: implemented
---

# v0.4 — Process-local READY OutputBatch outbox

> Подраздел обновления
> [`v0.4-file-artifacts-advanced`](README.md), дополняющий
> [`output-delivery.md`](output-delivery.md).

## AF-10A. Граница безопасного автоматического recovery

Автоматическая доставка после restart допустима только до начала transport
operation:

```text
OutputBatch.READY
→ immutable logical result уже committed
→ transport attempt ещё не существует
→ bounded automatic claim допустим

OutputBatch.DELIVERING
→ transport мог принять часть данных
→ automatic resend запрещён
→ stale attempt консервативно становится UNKNOWN

UNKNOWN / PARTIALLY_DELIVERED / FAILED / DELIVERED
→ automatic resend запрещён
```

READY worker не повторяет agent cycle, не пересобирает результат из mutable
artifact records и не обращается к LLM. Authority остаётся за уже сохранёнными:

```text
OutputBatch.parts
+ capability_snapshot
+ response_route
+ response_anchor
+ OutputDeliveryPlan
```

## AF-10A.1. Bounded projection

Gateway публикует transport worker-у только bounded refs:

```text
FINAL
+ READY
+ ready_at старше minimum_age
+ exact client_type
+ exact client_instance_id
→ ReadyOutputOutboxRef[]
```

Projection не содержит:

- полные output parts;
- route metadata;
- artifact bytes;
- локальные пути;
- transport URL;
- секреты.

Полный immutable OutputBatch возвращается только после успешного exact claim.
Минимальный возраст отделяет обычную synchronous delivery от restart recovery;
конкуренция всё равно разрешается durable claim authority, а не таймером.

## AF-10A.2. Exact credential authority

Внутренний API проверяет одновременно:

```text
API key
→ разрешённый client_type
→ разрешённый client_instance_id
→ session_id
→ immutable capability snapshot
```

`client_type` и `client_instance_id` из query/body не создают полномочия сами по
себе. Telegram credential связывается с точным `TELEGRAM_BOT_INSTANCE_ID`.
Internal credential может иметь explicit wildcard authority. Это позволяет
масштабировать несколько Telegram/Web/CLI instances без переинтерпретации
одних и тех же параметров.

## AF-10A.3. Idempotent claim

Claim использует отдельный стабильный `oclm_*` request ID:

```text
READY
+ first oclm request
→ DELIVERING + attempt_id
→ claim-request index committed атомарно

тот же oclm после потерянного HTTP-ответа
→ исходный attempt_id

другой oclm при active attempt
→ conflict
```

Worker повторяет сетевой claim только с тем же ID. Claim request index и
OutputBatch state изменяются под одним process-local lock с rollback.

## AF-10A.4. Immutable byte boundary

Shared Telegram control client не хранит mutable
`delivery_id → output_batch_id` mapping. Для каждого claimed batch executor
создаёт отдельный immutable facade:

```text
TelegramClaimedOutputGateway(
    output_batch_id,
    client_instance_id,
)
```

Artifact bytes открываются только через:

```text
claimed OutputBatch.DELIVERING
+ exact delivery_id member
+ exact session/client instance
→ scoped content stream
```

Transport client обязан проверить:

- `X-Output-Batch-ID`;
- `X-Delivery-ID`;
- `X-Content-Hash` с поддерживаемым алгоритмом;
- `Content-Length`;
- фактическую длину и hash;
- безопасное basename filename.

Legacy `/internal/deliveries/{id}/content` не является Telegram authority и
блокируется Gateway middleware. Per-batch facade безопасен для параллельных
чатов и будущих worker replicas, потому что не меняет shared process state.

## AF-10A.5. Durable addressing

Normal и recovered delivery используют только immutable OutputBatch addressing:

```text
response_route
→ conversation/thread

response_anchor
→ reply target
```

Transient Telegram `Update` или первый элемент media group не могут заменить
финальный route/anchor. Если durable route невалиден, attempt завершается exact
preflight-failure receipt до начала transport, а не остаётся бесконечно
`DELIVERING`.

Status presentation ID не считается обязательным restart authority. Recovered
status operation может создать новое сообщение вместо редактирования
неподтверждённого ephemeral handle.

## AF-10A.6. Receipt и crash windows

После claim worker выполняет обычную цепочку:

```text
claim
→ validate committed DeliveryPlan
→ execute groups
→ exact part receipts
→ atomic aggregate completion
```

Потерянный receipt HTTP response повторяется с exact тем же receipt. После
успешной transport operation новый send не запускается.

Если process падает:

```text
до claim
→ batch остаётся READY и может быть поднят снова

после claim, до подтверждённого send
→ v0.4 всё равно использует conservative stale UNKNOWN для non-artifact parts

после начала non-idempotent send
→ UNKNOWN

после durable receipt commit
→ exact replay возвращает terminal state
```

Точная фаза transport attempt и safe per-operation leases относятся к
распределённому outbox v0.6; v0.4 не заявляет distributed exactly-once.

## AF-10A.7. Artifact content evidence

Состояние logical OutputPart и факт доставки artifact bytes независимы:

```text
text fallback delivered
+ artifact_content_state=not_delivered
→ OutputPart может быть DELIVERED
→ ArtifactDelivery становится FAILED

media bytes delivered
+ overflow caption confirmed failed
→ OutputPart PARTIALLY_DELIVERED
→ ArtifactDelivery DELIVERED

media bytes delivered
+ overflow caption outcome unknown
→ OutputPart UNKNOWN
→ OutputBatch UNKNOWN
→ ArtifactDelivery DELIVERED
```

Таким образом, user-facing composition остаётся честной, а exact artifact bytes
не теряют подтверждённый receipt из-за дополнительной presentation operation.

## AF-10A.8. Telegram composition root

Полный runtime запускается через:

```bash
python -m src.servers.telegram
```

или:

```bash
uvicorn src.servers.telegram.app:app --host 0.0.0.0 --port 8001
```

`app.py` владеет ровно одним Telegram Application и одним process-local READY
worker. Низкоуровневый `telegram_server:app` остаётся compatibility webhook
entrypoint без background outbox polling, но package-wide byte policy всё равно
не позволяет ему использовать legacy Telegram content endpoint.

## AF-10A.9. Будущие версии

```text
v0.4-input-runtime
→ не меняет final READY authority;
  добавляет active-cycle input/intermediate interaction

v0.5
→ может хранить OutputBatch/artifact evidence в PostgreSQL,
  не меняя semantic contracts

v0.6
→ distributed workers, leases, queues, retry scheduling,
  reconciliation jobs и exactly-once там, где transport это позволяет
```
