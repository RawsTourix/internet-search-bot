---
id: design.v0.4.batch-workflows.shutdown-execution
version: v0.4
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-07-30
---

# BW-13 — Shutdown and staged execution boundary

## BW-13.1. Problem

До этого hardening Telegram выполнял один длинный запрос:

```text
POST /input-batches/{id}/commit
run=true
```

Запрос включал две разные операции:

```text
1. durable commit InputBatch
2. in-process AgentCycle с LLM/tools/finalization
```

Если Gateway останавливался после успешного commit, но во время LLM-запроса,
transport видел один HTTP 500 и логировал `commit_failed`. Это не соответствовало
фактическому durable state: пакет уже был committed и повторно отправлять файлы
не требовалось.

На Windows forced Uvicorn shutdown дополнительно выявил lifecycle defect:

```text
lifespan/request cancellation
→ API.stop skipped or interrupted
→ MCP streamable HTTP async generator finalized later
→ AnyIO cancel scope exits in another asyncio task
→ RuntimeError: Attempted to exit cancel scope in a different task
```

`BaseHTTPMiddleware` вокруг всех HTTP requests добавлял вторичный шум из AnyIO
memory streams (`WouldBlock`, `CancelledError`).

## BW-13.2. Durable boundary

Telegram workflow разделяется на два последовательных stage:

```text
POST /input-batches/{id}/commit  run=false
→ durable InputBatch committed

POST /input-batches/{id}/run
→ current in-process AgentCycle
```

Инварианты:

- `/run` не вызывается до подтверждённого commit;
- duplicate commit не запускает второй AgentCycle;
- cancellation/failure `/run` не откатывает committed InputBatch;
- transport не классифицирует agent interruption как commit failure;
- автоматический повтор `/run` после неизвестного результата запрещён;
- полноценный durable `AgentRun`/worker recovery остаётся будущей обязанностью
  runtime/orchestration updates.

Текущий split улучшает correctness и observability, но не делает AgentCycle
фоновым или автоматически resumable после process death.

## BW-13.3. Execution-scoped progress metadata

Durable `CommittedInputBatch.response_route` описывает provenance и reply anchor
исходного пакета. Status, созданный позднее под `/send`, является presentation exact
run invocation и не должен записываться обратно в committed batch.

`/run` принимает optional overlay:

```text
progress_callback_url
progress_callback_token
progress_target
status_message_id
progress_request_id
```

Gateway:

```text
берёт durable batch
→ создаёт in-memory copy response_route.metadata
→ накладывает run progress metadata
→ строит callback по copy
→ запускает исходный exact committed batch
```

Запрещено:

- мутировать durable InputBatch ради UI status;
- использовать redirect registry как основной способ найти run status;
- направлять execution progress в terminal collection snapshot;
- считать reply anchor и progress target одной сущностью.

## BW-13.4. Telegram FIFO admission boundary

`asyncio.Lock` вокруг отдельных handlers не гарантирует порядок: несколько уже
созданных tasks могут захватывать lock не по Telegram admission order.

До durable `CycleInbox` Telegram использует один dispatcher:

```text
Application.process_update(update)
→ synchronous enqueue exact session key
→ one FIFO worker per conversation/thread
```

Internal media-group completion enqueue-ится в ту же lane.

Инварианты:

- одна session не запускает overlapping ingress/run workflow;
- `/collect`, `/send`, `/cancel`, text, attachment и album completion выполняются
  в admission order;
- разные session сохраняют concurrency;
- nested callback той же lane выполняется inline и не deadlock-ится;
- dispatcher shutdown отменяет workers и settle-ит queued waiters;
- очередь in-process и не заменяет restart-safe `CycleInbox`.

## BW-13.5. Structured lifecycle logs

```text
telegram_input_batch_commit_started
telegram_input_batch_commit_finished
telegram_input_batch_commit_failed
telegram_input_batch_commit_cancelled

telegram_agent_run_started
telegram_agent_run_finished
telegram_agent_run_failed
telegram_agent_run_cancelled
```

`telegram_agent_run_cancelled` обязательно содержит смысл:

```text
committed=true
```

Логи не утверждают, что task потерян или требует повторной отправки файлов.

## BW-13.6. Gateway lifespan ownership

MCP streamable HTTP contexts создаются и закрываются одной lifespan task.

```text
startup
→ enter MCP AsyncExitStack in lifespan task

yield

finally
→ API.stop in the same lifespan task
→ adapter shutdown
→ propagate cancellation after cleanup
```

Запрещено переносить `AsyncExitStack.aclose()` в случайную shield/background task:
AnyIO cancel scopes должны завершаться владельцем, который вошёл в context.

Lifespan cleanup выполняется через `try/finally`, включая cancellation на
`yield`. Повторная forced cancellation может прервать bounded cleanup, но не
должна приводить к поздней неуправляемой финализации transport generator.

Telegram lifespan дополнительно останавливает READY outbox и session dispatcher.

## BW-13.7. Cancellation-transparent middleware

Security/authority middleware, не нуждающийся в request-body abstraction,
реализуется как pure ASGI middleware.

Это исключает лишний `BaseHTTPMiddleware` task/memory-stream layer и сохраняет:

```text
CancelledError from downstream
→ same CancelledError propagated upstream
```

Middleware не преобразует shutdown cancellation в HTTP 500 и не подавляет её.

## BW-13.8. Graceful and forced operator shutdown

Первый `Ctrl+C` Uvicorn означает graceful shutdown: server перестаёт принимать
новые соединения и ждёт активные requests. Пока AgentCycle остаётся in-process
HTTP request, этот этап может ждать долгий LLM retry workflow.

Повторный `Ctrl+C` означает forced shutdown и отменяет active request. В этом
случае допустим верхнеуровневый `KeyboardInterrupt`/cancel signal от Uvicorn, но
недопустимы project-level симптомы:

- skipped MCP cleanup;
- wrong-task cancel-scope RuntimeError;
- `commit_failed` после уже успешного durable commit;
- resurrection/opening нового InputBatch;
- автоматический повтор unknown AgentCycle;
- зависшие waiter futures Telegram dispatcher.

Ограниченный graceful timeout является deployment policy Uvicorn и не
подменяет будущий durable AgentRun contract.

## BW-13.9. Acceptance criteria

1. Commit request передаёт `run=false`.
2. `/run` начинается только после успешного commit response.
3. Commit и run используют разные structured log stages.
4. Duplicate commit не вызывает `/run`.
5. Cancellation во время `/run` пробрасывает `CancelledError`.
6. До cancellation `/run` durable commit уже подтверждён.
7. Run cancellation не логируется как `telegram_input_batch_commit_failed`.
8. Run progress target приходит execution-scoped overlay.
9. Durable response route после overlay не изменяется.
10. Collection snapshot не используется как AgentCycle progress target.
11. Telegram same-session operations сохраняют FIFO order.
12. Разные Telegram sessions могут выполняться параллельно.
13. Lifespan вызывает `API.stop()` из `finally`.
14. MCP runtime закрывается в task-владельце context stack.
15. Dispatcher завершается из Telegram lifespan.
16. Authority guard использует pure ASGI middleware.
17. Downstream cancellation проходит через guard без преобразования.
18. Forced shutdown не меняет committed InputBatch.
19. Full Windows suite проходит после hardening.
20. Live cancellation больше не создаёт wrong-task cancel-scope RuntimeError.

## BW-13.10. Validation и configuration

CI head `5d9edb4b1e3eafb7b3ad58deab920e147d5bb8e0`:

```text
compile: success
artifact suite: 238 tests, OK
storage suite: 41 tests, OK
plans suite: 45 tests, OK
planning suite: 19 tests, OK
API suite: 1 test, OK
```

Этот patch не добавляет параметры `.env` или `mcp.config`.

Если позднее вводится application-owned graceful timeout или durable queue
configuration, соответствующий patch обязан одновременно добавить config model,
validation, tests, documentation и обновление `.example`.
