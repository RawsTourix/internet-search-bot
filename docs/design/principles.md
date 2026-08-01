---
id: design.principles
version: cross-version
spec_status: accepted
implementation_status: mixed
last_reviewed: 2026-08-01
---

# Главные принципы

> Некоторые пункты относятся к будущим версиям v0.4–v0.10 и становятся
> применимыми только в границах соответствующей version specification.
> Текущий baseline определён в [`current.md`](current.md).

1. `messages_for_llm` — рабочий контекст, а не долговременное хранилище.
2. Полные results, files и старые cycle segments сохраняются через storage interfaces.
3. Runtime зависит от `ContentStore` / `ArtifactStore`, а не от файловых путей.
4. Большой result сначала сохраняется и только потом компактизируется.
5. Агент может указать `result_handling`, но runtime имеет последнее слово по безопасности.
6. `prefer_inline` не отключает защиту от переполнения.
7. Размер result оценивается относительно context budget модели.
8. Абсолютные лимиты — только техническая защита процесса.
9. Small result простой задачи может остаться inline.
10. Single-pass summary применяется только к result, помещающемуся в отдельный compact request.
11. Oversized result получает `needs_retrieval=true`, а не ложное полное summary.
12. Canonical original content хранится целиком.
13. Chunks/embeddings — перестраиваемые производные данные.
14. Lazy chunking и semantic RAG относятся к `v0.5`.
15. Cycle compaction работает только с закрытыми атомарными segments.
16. Нельзя разрывать assistant tool calls и corresponding tool messages.
17. В visible context максимум одно актуальное `CycleWorkingMemory`.
18. Старые summary generations не образуют дерево.
19. Исходные cycle events остаются source of truth.
20. Compaction не удаляет segment до успешного replacement.
21. Compaction является trace/progress event.
22. Progress не содержит raw results, secrets и большие payloads.
23. Final grounding использует только фактически доступное evidence.
24. Непрочитанная часть stored content не считается evidence.
25. DAG — отдельный artifact cycle, а не system-prompt text.
26. DAG необязателен для simple tasks.
27. В `v0.4` DAG — карта, а не scheduler.
28. Authoritative current plan читается через точный `PlanStore`, не через RAG.
29. Plan/node IDs принадлежат runtime; LLM использует только `client_key`.
30. `ready` и `stalled` вычисляются и не сохраняются как lifecycle statuses.
31. Каждая plan mutation, кроме create, требует `expected_revision`.
32. При active plan содержательный tool call требует один `in_progress` node.
33. Active plan должен быть completed либо cancelled до final answer.
34. Lifecycle status и `AgentActivity` — разные оси.
35. File представлен `ArtifactRef`, а не arbitrary local path.
36. Edit пользовательского file создаёт новую version.
37. File delivery выполняет adapter layer.
38. Прочитанное file content проходит result-compaction policy.
39. Transport message не равен logical user turn.
40. `InputBatch` объединяет text и attachments.
41. `CycleInbox` принимает sealed `InputBatch`.
42. Initial request и active-cycle addendum используют одну batch model.
43. Addendum вставляется только в safe checkpoint.
44. Полезный tool call не игнорируется ради нового input.
45. Per-session lock защищает active cycle и inbox.
46. `WAITING_USER`/infrastructure interruption сохраняют resumable workspace.
47. v0.4 работает без PostgreSQL, но через PostgreSQL-friendly interfaces.
48. v0.5 добавляет PostgreSQL/pgvector без обязательных microservices.
49. v0.6 вводит Redis/workers/services при реальной необходимости.
50. PostgreSQL — durable source of truth; Redis его не заменяет.
51. Background jobs должны быть idempotent.
52. MCPServerManager остаётся lifecycle coordinator MCP runtime.
53. Agent loop не управляет reconnect/restart transport напрямую.
54. Surface-specific formatting применяется на финальной стадии.
55. Delivery constraints влияют на форму, а не на facts/actions.
56. Новые слои сохраняют local development mode.
57. Длительный AgentRun не зависит от lifetime одного HTTP-соединения.
58. Client disconnect не оставляет run в неопределённом состоянии.
59. Final result сохраняется до terminal status `succeeded`.
60. Повтор Web request с тем же idempotency key не создаёт duplicate run.
61. Per-attempt timeout/retry budget отделён от total run deadline.
62. Execution outcome, delivery outcome и result retrieval наблюдаются раздельно.
63. `v0.6` различает workflow DAG крупных задач и local task DAG одной задачи.
64. Planner/LLM определяет смысл и dependencies; scheduler обеспечивает жёсткое,
    идемпотентное и ресурсно ограниченное исполнение.
65. Agent Executor получает одну ясно описанную ответственность, bounded inputs и
    проверяемый output contract.
66. Результаты между tasks передаются как structured summaries и exact/RAG refs,
    а не как полный producer context.
67. Task lifecycle status, AgentActivity и domain task type являются разными
    осями состояния.
68. Skills загружаются по необходимости; вся библиотека не помещается в system
    prompt или visible context.
69. Skill декларирует required capabilities, но не выдаёт себе разрешения и не
    отменяет runtime/system policy.
70. MCP servers и skills используют совместимые scopes: `builtin`, `instance`,
    `user`, `session`.
71. `user` scope становится полноценно enforced только после Identity и
    Authorization layer `v0.8`.
72. Account, Identity, AuthSession, Conversation, AgentRun, TaskRun и AgentCycle
    не смешиваются в одну сущность `session`.
73. Security audit является release gate/hardening process, а не доказательством
    абсолютной безопасности или обычной product feature.
74. Local и self-hosted deployment остаются first-class после появления accounts
    и потенциального managed service.
75. `AgentRuntime`, а не MCP-specific client, является владельцем agent loop.
76. MCP является одним из tool backends и не владеет application/session state.
77. Новые runtime capabilities подключаются через composition, providers,
    policies и hooks, а не через неограниченную цепочку subclasses/mixins.
78. Concrete infrastructure создаётся в composition root; import модуля не
    запускает application lifecycle.
79. Domain/application code не импортирует FastAPI, SQLAlchemy, Redis, arq,
    Docker или Kubernetes adapters.
80. Process/network boundary вводится после стабилизации in-process contract.
81. Python package или таблица не являются достаточной причиной для выделения
    микросервиса.
82. `AgentRun`, `TaskRun`, `AgentCycle` и `ExecutionAttempt` имеют отдельные
    identity, lifecycle и retry semantics.
83. `TaskRun` получает runtime-owned `TaskContextManifest`, а не произвольную
    копию parent message history.
84. Workflow revision является committed runtime artifact; executor не создаёт
    бесконтрольные дочерние agents напрямую.
85. User input во время active run становится durable intervention и применяется
    только в safe boundary.
86. Control plane и execution plane разделяются начиная с v0.9.
87. Ephemeral sandbox привязывается к TaskRun/execution attempt, а не навсегда к
    user или conversation.
88. Sandbox не получает database, Redis, LLM provider или container-daemon
    credentials.
89. Network access sandbox запрещён по умолчанию и расширяется только policy.
90. Sandbox output импортируется в durable storage до terminal completion и
    teardown.
91. `ExecutionBackend` скрывает local process, container и remote runner details.
92. Distributed execution использует leases и fencing tokens; старый attempt не
    может commit после выдачи нового lease generation.
93. Потерянный runner не является source of truth о состоянии run.
94. Object storage и exact immutable refs заменяют shared-local-filesystem
    assumption в v0.10.
95. Идентификатор принятого или начатого update стабилен и не меняется при
    последующей реорганизации документации.
96. Scope определяет visibility/precedence registry, но не является разрешением.
97. Trusted tool execution, presentation и lifecycle metadata берутся из
    доверенного registry, а не из произвольного tool output.
98. MCP transport lifecycle и lifecycle remote resource являются независимыми.
99. Remote resource handle является opaque; Agent Runtime хранит ownership
    coordinates, но не внутреннее состояние внешнего сервиса.
100. Lifecycle hooks универсальны, а automatic remote-resource integration
     разрешается только trusted policy для конкретного binding.
101. Cleanup со стороны Agent Runtime является bounded best effort; внешний
     сервис остаётся владельцем окончательной expiration/orphan cleanup.
102. Mutating tool call с неопределённым transport outcome получает `unknown` и
     не повторяется автоматически.
103. Canonical progress создаёт Agent Runtime; внешний MCP-сервис не управляет
     локализованным пользовательским UI.
