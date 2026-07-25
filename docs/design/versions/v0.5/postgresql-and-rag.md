---
id: design.v0.5.postgresql-and-rag
version: v0.5
spec_status: draft
implementation_status: planned
---
# Часть IX. v0.5 — PostgreSQL, lazy indexing и RAG для agent workspace

## 109. Главная идея v0.5

`v0.5` переносит memory/workspace metadata в PostgreSQL и добавляет retrieval по текущим и предыдущим cycles, stored results, files, trace/events и plan relations.

```text
PostgreSQL + pgvector
для долговременной памяти и agent workspace,
без полной микросервисной перестройки.
```

---

## 110. Совместимость backend

PostgreSQL implementations реализуют те же contracts, что filesystem backend `v0.4`.

Возможна hybrid storage:

```text
PostgreSQL
→ metadata, relations, statuses, indexes

filesystem / object storage
→ large binary/raw payloads
```

Agent loop не меняет business logic при смене backend.

---

## 111. Минимальные сущности PostgreSQL

```text
agent_sessions
session_runtime_states
agent_cycles
cycle_messages
cycle_trace_events
cycle_working_memories

stored_contents
stored_result_refs
result_compactions

artifact_lineages
artifact_versions
artifact_extractions
artifact_candidates
artifact_delivery_selections
artifact_deliveries
artifact_delivery_attempts

agent_plans
agent_plan_nodes
agent_plan_edges
agent_plan_revisions

ingress_events
input_batch_drafts
input_batches
input_batch_items
cycle_inbox_items
session_control_commands
client_message_bindings
client_response_outbox

content_chunks
chunk_embeddings
retrieval_events
```

Физические таблицы могут объединяться, но domain boundaries, immutable IDs,
ownership, status transitions и idempotency relations должны сохраниться.

### 111.1. Ownership-ready metadata и будущие scopes

`v0.5` ещё не вводит полноценные пользовательские аккаунты и multi-tenant
workspace, однако PostgreSQL-модели не должны навсегда закреплять один глобальный
owner для всех данных.

Для сущностей, где это уместно, заранее предусматриваются nullable или
system-owned поля и relations:

```text
owner_user_id
workspace_id
conversation_id
created_by_principal_id
scope
```

В `v0.5` они могут быть пустыми, ссылаться на локального/system principal либо
определяться текущим client/session context. Полноценная проверка account-level
ownership относится к `v0.8`.

`agent_sessions.external_user_id` на этом этапе остаётся идентификатором
transport/client principal и не должен ошибочно считаться глобальным account ID.
Точная авторизация доступа к artifacts, contents и retrieval выполняется через
текущий runtime/session access set.

Такой задел позволяет позднее добавить области видимости `builtin`, `instance`,
`user` и `session` без миграции от неявного глобального namespace.

---

## 112. Основные таблицы cycle

### `agent_sessions`

```text
id
client_type
external_user_id
created_at
updated_at
metadata_json
```

### `agent_cycles`

```text
id
session_id
status
activity
original_user_request
final_answer
error
error_kind
can_resume
active_plan_id
active_plan_revision
active_plan_node_id
working_memory_generation
created_at
updated_at
completed_at
metadata_json
```

### `cycle_messages`

```text
id
cycle_id
role
content_json
tool_call_id
message_index
input_batch_id
created_at
```

### `cycle_trace_events`

```text
id
cycle_id
event_type
payload_json
created_at
```

---

## 113. Stored content/results

### `stored_contents`

```text
id
storage_backend
storage_uri
mime_type
content_hash
size_bytes
size_chars
size_tokens_estimate
source_type
created_at
metadata_json
```

### `stored_result_refs`

```text
id
content_id
cycle_id
tool_call_id
tool_name
summary_status
summary_json
preview
needs_retrieval
created_at
metadata_json
```

---

## 114. Artifacts и plans

Artifacts:

```text
artifact_lineages
  id, session_id, current_artifact_id, current_version, purpose, status

artifact_versions
  id, lineage_id, version, parent_artifact_id, content_id,
  filename, format_id, detected_mime_type, size_bytes, content_hash,
  provenance_json, created_cycle_id

artifact_extractions
  artifact_id, extractor/version, extracted_content_id, status

artifact_candidates
  content_id, source_tool_call_id, status, promotion target

artifact_deliveries / attempts
  exact artifact_id, client target, state, attempt, receipt/error
```

Current artifact version читается точно по lineage head, а не через vector search.
Payload может оставаться на filesystem/object storage; PostgreSQL хранит metadata,
relations и transaction boundary.

Plans:

```text
agent_plans
agent_plan_nodes
agent_plan_edges
agent_plan_revisions
```

Plan revision может храниться snapshot или event log. Plan refs связываются с
exact artifact/result versions.

### 114.1. Структурированные результаты для будущей orchestration

`v0.5` не вводит workflow scheduler, но сохранённые результаты должны быть
пригодны для передачи между будущими задачами без копирования полного LLM-контекста.

Концептуальный task output содержит:

```text
result/artifact type
producer cycle/plan node
compact summary
exact content/result/artifact refs
provenance and limitations
created_at / schema version
```

Полный payload остаётся в workspace и доступен через exact read или RAG. В
контекст следующего этапа передаются bounded summary, typed fields и refs. Эта
модель подготавливает `v0.6` к task-to-task handoff, но не добавляет task
scheduler в `v0.5`.

---

## 115. Ingress, batches, inbox и outbox

```text
ingress_events
input_batch_drafts
input_batches
input_batch_items
cycle_inbox_items
session_control_commands
session_runtime_states
client_message_bindings
client_response_outbox
```

PostgreSQL transaction закрывает критические переходы:

```text
persist/commit batch + admission decision
claim/apply inbox item + cycle replay state
final inbox recheck + terminal cycle state + response outbox
artifact version commit + lineage head update
delivery claim + attempt state
```

`CycleInbox` по-прежнему оперирует committed `InputBatch`, а не transport
messages. Persisted draft/event metadata позволяет восстановить pending input
после рестарта. Redis в `v0.5` не обязателен: database polling/notifications или
in-process dispatcher могут использовать те же contracts.

---

## 116. Lazy extraction и chunking

Первый retrieval:

```text
ContentRef / ArtifactRef
→ processor
→ text extraction при необходимости
→ chunker
→ chunks
→ embeddings
→ cached index
```

Chunking зависит от типа:

```text
plain text → paragraphs/semantic boundaries
PDF → pages/sections
spreadsheet → sheets/ranges
source code → modules/classes/functions
HTML → headings/content blocks
```

Chunks и embeddings можно перестроить при смене extractor/chunker version.

---

## 117. `content_chunks` и embeddings

```text
content_chunks:
  id
  content_id
  chunk_index
  text
  char_start
  char_end
  page_number
  section_path
  token_estimate
  chunker_name
  chunker_version
  metadata_json
```

```text
chunk_embeddings:
  id
  chunk_id
  embedding_model
  embedding
  created_at
```

`embedding` хранится через pgvector.

---

## 118. Retrieval tools

```text
agent_memory_get_cycle
agent_memory_search_cycles
content_get_metadata
content_read_range
content_list_chunks
content_get_chunk
content_search
artifact_get_metadata
artifact_search_content
agent_plan_get
agent_plan_search_results
```

```json
{
  "content_id": "cnt_...",
  "query": "ошибка подключения",
  "limit": 5,
  "search_type": "keyword | semantic | hybrid"
}
```

---

## 119. Retrieved context временный

```text
retrieve chunks
→ LLM uses data
→ extracted notes/facts persisted
→ raw chunks removed at later compaction
→ chunk IDs + content ID + facts remain
```

```json
{
  "type": "retrieved_context_summary",
  "content_id": "cnt_...",
  "used_chunk_ids": ["ch_1", "ch_8"],
  "extracted_facts": ["...", "..."],
  "retrieval_event_id": "ret_..."
}
```

---

## 120. RAG, activity и grounding

Retrieval выполняется при:

```text
status = RUNNING
activity = PROCESSING
```

Agent может чередовать processing, planning, executing, validating и collecting.

Для evidence сохраняется provenance:

```text
content_id
chunk_id/range
retrieval_event_id
source result/artifact
```

Final grounding использует только фактически retrieved или inline evidence.

---

## 121. Что не входит в v0.5

- обязательная микросервисная архитектура;
- Redis как обязательная runtime dependency;
- distributed workers как обязательный execution path;
- automatic DAG scheduler;
- automatic parallel plan nodes;
- массовая eager-индексация всех files;
- сложное branching/merge artifact versions;
- полноценный multi-tenant shared workspace;
- user accounts, account sessions и linked identities;
- обязательный antivirus/macro sandbox;
- полная замена local development mode.

---

## 122. Acceptance criteria v0.5

```text
filesystem metadata backend заменяется PostgreSQL-compatible implementations
без переписывания agent loop, manager tools и client adapters.
```

```text
batch/admission/finalization critical transitions
→ one database transaction
→ no lost input or duplicate terminal response.
```

```text
first RAG read of PDF/DOCX/XLSX/PPTX
→ lazy extraction/chunking
→ provenance-aware index persisted
→ repeated retrieval uses cache.
```

```text
semantic/hybrid search returns exact refs + retrieved ranges/chunks,
not uncontrolled raw payload.
```

```text
cycle resume after process restart restores messages, working memory,
current plan, artifact access set, pending inbox and response outbox.
```

```text
artifact lineage/current head/version history and delivery state remain exact,
while extracted chunks/embeddings are rebuildable derived data.
```

```text
storage models preserve explicit ownership/scope extension points,
while v0.5 remains usable in single-user local mode without account authorization.
```

```text
structured result/artifact refs can be consumed by a later task through
bounded summary + exact/RAG retrieval without replaying the producer context.
```

---

