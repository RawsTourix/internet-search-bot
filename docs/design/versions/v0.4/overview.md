---
id: design.v0.4.overview
version: v0.4
spec_status: accepted
implementation_status: partial
---
# Часть VIII. v0.4 — agent workspace, planning & context management foundation

## 54. Главная идея v0.4

`v0.4` превращает текущий agent runtime в рабочее пространство, способное безопасно выполнять длинные, составные и файловые задачи.

Версия объединяет шесть связанных пакетов:

```text
v0.4-storage-foundation
v0.4-result-compaction
v0.4-cycle-compaction
v0.4-dag-planning
v0.4-file-artifacts
v0.4-input-runtime
```

Главная архитектурная формула:

```text
полные данные, файлы и история выполнения
→ внешнее storage/workspace

видимый LLM-контекст
→ только актуальная рабочая информация,
   компактные представления и устойчивые ссылки
```

`v0.4` работает без PostgreSQL, Redis и workers, но новые компоненты проектируются через интерфейсы, совместимые с последующей миграцией.

---

## 55. Граница v0.4

В `v0.4` входят:

- файловая storage foundation;
- LLM-компактизация больших результатов;
- LLM-компактизация старой части agent cycle;
- необязательный DAG-план;
- получение, чтение, изменение, версионирование и отправка файлов;
- `InputBatch`;
- `CycleInbox`, принимающий `InputBatch`;
- safe checkpoints и per-session lock;
- progress/trace events новых процессов.

В `v0.4` не входят:

- PostgreSQL и pgvector;
- embeddings и semantic RAG;
- постоянный chunk index;
- Redis/arq и background workers;
- automatic DAG scheduler;
- распределённый runtime;
- микросервисная архитектура.

Главный инвариант:

```text
Raw content не должен бесконтрольно жить
в messages_for_llm или дублироваться в cycle archive.
```

---

## 56. Разделение ответственности

Рекомендуемая структура:

```text
src/
  storage/
    models.py
    interfaces.py
    file_backend.py
    serializers.py

  memory/
    context_budget.py
    result_compaction.py
    cycle_compaction.py
    service.py

  artifacts/
    models.py
    service.py
    processors.py

  planning/
    models.py
    validation.py
    service.py
    tools.py

  runtime/
    cycle.py
    session_runtime.py
    input_batch.py
    cycle_inbox.py
```

Физически все модули не обязательно создавать одним патчем, но границы ответственности должны быть сохранены.

`MCPClient` остаётся orchestrator agent loop, но не должен знать физические пути хранения, реализовывать DAG-validation, напрямую отправлять Telegram-файлы или принимать параллельные изменения `messages_for_llm`.

---

