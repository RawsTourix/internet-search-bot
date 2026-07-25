---
id: design.v0.3.memory-model
version: v0.3
spec_status: accepted
implementation_status: implemented
---
# Часть I. Базовая модель памяти

## 1. Почему старая модель была проблемной

Старая модель, где `session.messages` одновременно был:

- историей общения;
- рабочим контекстом агента;
- логом инструментов;
- хранилищем tool results;
- трассировкой выполнения;

приводила к загрязнению контекста.

Проблемы:

```text
1. Старые tool results попадали в следующие запросы.
2. Огромные ответы инструментов раздували контекст.
3. LLM могла путаться между текущей задачей и прошлой выдачей.
4. final audit мог видеть лишние данные и галлюцинировать.
5. Невозможно было гибко управлять памятью.
```

Поэтому память разделяется на несколько слоёв.

---

## 2. Основные слои памяти

### 2.1. `messages_for_llm`

Локальный рабочий список сообщений, который отправляется в LLM в текущей итерации.

Это **видимый контекст агента**.

В него могут входить:

- system prompt;
- краткая session memory;
- текущий user request;
- текущие tool calls/tool results;
- текущий `pending_cycle`, если задача не завершена.

Важно:

```text
messages_for_llm не является долговременным хранилищем.
```

---

### 2.2. `session_dialog_memory` / `dialog_turns`

Краткая история завершённых обращений.

Формат:

```text
user_request → final_answer → status → tools_used
```

Туда не должны попадать:

- role=tool;
- assistant tool_calls;
- большие tool results;
- running/continue AgentAction;
- browser snapshots;
- HTML;
- внутренние repair/audit-сообщения.

---

### 2.3. `cycle_trace`

Подробная трассировка текущего агентного цикла. Ранее в коде использовалось имя `task_trace`; после refactor используется `cycle_trace`.

Туда попадают:

- LLM responses;
- tool calls;
- tool results;
- tool errors;
- progress events;
- context warnings;
- compaction events;
- infrastructure errors.

Trace нужен для debug, архивов и будущей памяти, но **не должен автоматически попадать в LLM-контекст**.

---

### 2.4. `archival_logs`

Полное хранилище циклов в JSON-файлах.

Это переходный формат перед PostgreSQL.

Архив нужен для:

- отладки;
- последующего анализа;
- будущей миграции в БД;
- восстановления старых agent cycles;
- будущих RAG-инструментов.

---

### 2.5. `pending_cycle`

Снимок незавершённого агентного цикла.

Используется, когда агент дошёл до `WAITING_USER`.

`WAITING_USER` не завершает цикл. Он только ставит его на паузу.

---

