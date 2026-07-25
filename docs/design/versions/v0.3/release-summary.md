---
id: design.v0.3.release-summary
version: v0.3
spec_status: accepted
implementation_status: implemented
---
# Часть VI. Итог v0.3 перед v0.4

## 52. Итог v0.3 перед v0.4

`v0.3` можно считать архитектурно завершённой фундаментальной версией agent runtime.

Эта версия закрепила не одну отдельную функцию, а базовый каркас дальнейшей архитектуры:

```text
LLM agent loop
+ AgentAction JSON
+ dynamic MCP manager tools
+ cycle memory
+ pending / interrupted cycle
+ progress events
+ MCP runtime lifecycle
+ delivery constraints
+ final processing pipeline
+ cycle_trace / archive
```

Главный итог:

```text
v0.3 превратила проект из простого MCP-клиента
в управляемый agent runtime с наблюдаемостью,
сохраняемым состоянием цикла и подготовкой к storage-архитектуре.
```

### Ключевые изменения v0.3

В рамках v0.3 закреплены следующие слои:

```text
1. JSON-протокол агента
   AgentAction заменяет старые текстовые маркеры и делает ответы агента валидируемыми.

2. Dynamic MCP discovery
   Агент больше не должен знать все инструменты из system prompt.
   Доступные серверы, инструменты и схемы раскрываются через manager tools.

3. Лёгкий system prompt
   System prompt отвечает за базовые правила, JSON-протокол и безопасность.
   Tool descriptions и surface-specific formatting вынесены из него.

4. Agent cycle memory
   Введены cycle_id, cycle_trace, pending_cycle, interrupted cycle и last_error_cycle.
   WAITING_USER больше не считается завершением задачи.

5. Resume после WAITING_USER и инфраструктурных ошибок
   Агент может сохранить контекст незавершённого цикла и продолжить работу после ответа пользователя
   или после временного сбоя LLM/transport.

6. Live progress events
   Runtime сообщает UI/Telegram о ключевых этапах выполнения задачи:
   cycle_started, cycle_resumed, tool_start, tool_done, tool_error, llm_retry,
   llm_error, waiting_user, final_processing_started, cycle_done, cycle_error.

7. Telegram progress UX
   Status-message принадлежит progress callbacks.
   Финальный ответ отправляется отдельным сообщением.
   Telegram server не должен затирать runtime-status собственным "готово".

8. LLM retry/error classification
   Retryable HTTP/transport ошибки отделены от configuration errors.
   429/5xx могут сохранять контекст для продолжения.
   400/401/403/404/422 считаются ошибками конфигурации и не получают retry.

9. Lifecycle-aware MCPServerManager
   MCPServerManager отвечает за health/recovery/retry runtime-а MCP-серверов.
   Сбой внешнего MCP-сервера не должен ронять Gateway request.

10. Delivery constraints
    Telegram/Web-ограничения применяются на финальной стадии и влияют только на форму ответа,
    а не на факты, выводы или выбор инструментов.

11. Final processing pipeline
    Финальная обработка разделена на выбор режима, форматирование и проверку по собранным данным.
    В коде закреплены FORMAT_ONLY, GROUNDED, STRICT_GROUNDED и SKIP.

12. Cycle trace / archive
    cycle_trace стал подробным журналом работы agent cycle.
    Это переходный слой перед PostgreSQL-хранением событий, результатов и артефактов.
```

### Почему v0.3 нужно остановить

К концу v0.3 в проекте уже есть основные runtime-механизмы:

```text
агентный цикл работает;
инструменты подключаются динамически;
прогресс виден пользователю;
ошибки LLM и MCP runtime обрабатываются управляемо;
контекст цикла можно сохранять;
финальный ответ проходит отдельную обработку.
```

Дальше добавлять крупные новые сущности прямо в v0.3 уже неразумно.

Причина:

```text
новые механизмы вроде DAG planner, workers, persistent tool-call tracking,
PostgreSQL memory и LargeResultStore должны жить уже на новой storage/runtime архитектуре.
Если добавить их в v0.3, их потом придётся переносить и переписывать при v0.4/v0.5.
```

### Feature freeze для v0.3

После завершения v0.3 допустимы только безопасные изменения:

```text
bugfixes;
tests;
documentation;
небольшие UX-правки;
чистка названий и структуры;
фиксация design_document.md;
подготовка к v0.4.
```

Не стоит добавлять в v0.3:

```text
DAG planner;
background workers;
PostgreSQL tables;
LargeResultStore;
persistent tool-call queue;
Redis/arq;
полноценный task manager;
новую систему long-term memory;
новые крупные runtime-artifacts.
```

### Граница перехода к v0.4

Правило перехода:

```text
v0.3 фиксирует текущий agent runtime.
v0.4 начинает storage/large-context архитектуру.
```

Практически это означает:

```text
1. Новые крупные возможности добавлять после v0.4/v0.5.
2. v0.3 больше не расширять архитектурно.
3. Перед v0.4 стабилизировать документацию, тесты и багфиксы.
4. Использовать v0.3 как baseline для миграции к storage interfaces, compaction и PostgreSQL.
```

Короткая формула:

```text
Не добавлять в старую архитектуру то,
что должно жить в новой.
```

---

