# Telegram transport runtime

Каноническая точка запуска Telegram-клиента после
`v0.4-file-artifacts-advanced`:

```bash
python -m src.servers.telegram
```

Эквивалентный ASGI entrypoint:

```bash
uvicorn src.servers.telegram.app:app --host 0.0.0.0 --port 8001
```

`src.servers.telegram.app` объединяет в одном process-local runtime:

- Telegram webhook application;
- shared ingress/InputBatch flow;
- instance-scoped Gateway client;
- authoritative `OutputDeliveryPlan` executor;
- bounded polling только безопасных `OutputBatch.READY`;
- exact receipt persistence и conservative recovery.

Прямой запуск
`src.servers.telegram.telegram_server:app` сохранён как низкоуровневый
compatibility entrypoint для webhook-разработки. Package-wide input policy
сохраняет в нём ту же маршрутизацию semantic-only событий: forwarded text,
location, contact и poll не входят в standalone-attachment commit path и могут
безопасно присоединяться к открытому InputBatch. Однако этот entrypoint не
запускает READY-outbox worker и не является полным production composition root.
Для полного агентного runtime используйте только канонический entrypoint выше.

Настройки process-local outbox:

```text
TELEGRAM_READY_OUTBOX_POLL_SECONDS=15
TELEGRAM_READY_OUTBOX_MINIMUM_AGE_SECONDS=30
TELEGRAM_READY_OUTBOX_BATCH_LIMIT=50
```

Outbox автоматически claim-ит только достаточно старые финальные
`OutputBatch.READY` точного `TELEGRAM_BOT_INSTANCE_ID`. Состояния
`DELIVERING`, `UNKNOWN`, `PARTIALLY_DELIVERED`, `FAILED` и `DELIVERED`
автоматически повторно не отправляются.
