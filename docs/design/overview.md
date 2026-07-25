---
id: design.overview
version: cross-version
spec_status: accepted
implementation_status: mixed
---
# Дизайн-документ: архитектура ИИ-агента v0.3 → v0.8

## 0. Назначение документа

Этот документ фиксирует развитие архитектуры памяти, рабочего пространства и runtime ИИ-агента после перехода на JSON-протокол, динамические MCP-инструменты и разделение контекста.

Главная цель:

```text
Агент должен уметь выполнять длинные задачи,
не терять рабочий контекст при WAITING_USER,
не засорять LLM-контекст завершёнными tool results,
и постепенно перейти к долговременной памяти, durable orchestration,
подключаемым skills и многопользовательской среде.
```

Документ описывает:

- текущую логику v0.3;
- промежуточные обновления ветки `feature` перед v0.4:
  - `v0.3-agent-protocol-foundation`;
  - `v0.3-agent-memory-runtime`;
  - `v0.3-cycle-memory`;
  - `v0.3-progress-events`;
  - `v0.3-progress-events refinements`;
  - `v0.3-mcp-server-manager`;
  - `v0.3-prompt-optimization`;
  - `v0.3-final-processing-pipeline`;
  - `v0.3-final-processing-progress`;
- итог v0.3 и границу feature freeze перед v0.4;
- v0.4: agent workspace, storage foundation, LLM-compaction, file artifacts, DAG planning и input runtime;
- v0.5: PostgreSQL, lazy indexing, pgvector и RAG для памяти и workspace;
- v0.6: микросервисную архитектуру, Redis/arq, workers, workflow orchestration и distributed runtime;
- v0.7: предварительную концепцию подключаемой библиотеки skills;
- v0.8: предварительную концепцию Identity & Multi-user Workspace;
- принципы result/cycle compaction;
- работу с файлами и версиями артефактов;
- `InputBatch` и `CycleInbox`;
- будущие RAG-инструменты, scheduler, skills и multi-user boundaries.

Разделы `v0.7` и `v0.8` фиксируют предварительные архитектурные концепции.
Они не являются готовым техническим заданием: точные схемы данных, интерфейсы,
пакеты и промежуточные версии должны уточняться после стабилизации `v0.5` и
`v0.6`.

---

