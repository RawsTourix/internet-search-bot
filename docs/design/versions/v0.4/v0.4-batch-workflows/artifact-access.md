---
id: design.v0.4.batch-workflows.artifact-access
version: v0.4
spec_status: accepted
implementation_status: planned
last_reviewed: 2026-07-29
---

# BW-9 — Artifact access, visibility and activation

## BW-9.1. Three separate concepts

Artifact policy разделяет:

```text
authorization/access
≠ automatic prompt visibility
≠ activation in current cycle
≠ delivery selection
```

Жёсткое правило «исторические артефакты запрещены» не принимается. Агент должен
уметь вернуться к любому exact artifact version, доступному текущему
workspace/session/principal.

Ограничивается не доступ как таковой, а объём автоматической проекции в LLM
context и неявность выбора.

## BW-9.2. Authorization scope

Store/API проверяет access policy до возврата metadata/content:

```text
workspace ownership
session visibility
principal/client authorization
artifact/version existence
retention state
```

Filesystem v0.4 использует текущую session/workspace policy. PostgreSQL v0.5 и
multi-user v0.8 заменят backend/authorization implementation, но не tool
contract.

Agent не может расширить scope произвольным `session_id`, local path или guessed
artifact ID.

## BW-9.3. Bounded active manifest

В каждый agent input автоматически включается только bounded active manifest:

```text
current InputBatch artifacts
+ explicit referenced_artifact_refs
+ artifacts created/modified in current cycle
+ currently selected deliverables
+ artifacts explicitly activated current-cycle tools
```

Manifest содержит metadata exact versions:

```json
{
  "artifact_id": "art_...",
  "artifact_lineage_id": "aln_...",
  "filename": "report.csv",
  "format_id": "csv",
  "size_bytes": 1024,
  "purpose": "input",
  "activation_reason": "current_input_batch"
}
```

Manifest:

- bounded по количеству и serialized size;
- не содержит bytes, local paths, transport locators или secrets;
- восстанавливается из runtime/store после compaction;
- не является полным каталогом workspace;
- не меняет authorization.

## BW-9.4. Catalog scopes

`artifact_list` получает server-validated scope:

```python
scope: Literal["current", "session", "workspace"] = "current"
```

Semantics:

```text
current
→ active manifest/current cycle working set

session
→ доступные exact versions, связанные с текущей session history

workspace
→ все авторизованные workspace artifacts в bounded pagination
```

Обязательные параметры для session/workspace listing:

```text
limit
cursor
optional filename/format/purpose filters
```

Нельзя возвращать неограниченный полный каталог в один tool result.

В v0.5 добавляется semantic search/RAG, но `artifact_list` остаётся точным
metadata catalog operation.

## BW-9.5. Activation

Artifact становится active в текущем cycle, если exact version:

1. пришла в current `InputBatch`;
2. передана через `referenced_artifact_refs`;
3. создана/изменена текущим cycle;
4. возвращена текущим вызовом `artifact_list`, `artifact_search`, `artifact_get`
   или другого authoritative manager tool;
5. выбрана пользователем через client UI/reply binding.

Runtime сохраняет bounded activation record:

```python
class ArtifactActivation(BaseModel):
    artifact_id: str
    cycle_id: str
    reason: Literal[
        "current_input_batch",
        "explicit_reference",
        "created_in_cycle",
        "modified_in_cycle",
        "catalog_result",
        "search_result",
        "client_selection",
    ]
    source_operation_id: str | None
    activated_at: datetime
```

Activation не копирует artifact и не создаёт новую version.

## BW-9.6. Historical use and delivery

`artifact_set_delivery` может выбрать historical exact artifact, если:

```text
artifact авторизован
+ exact version существует
+ artifact активирован в текущем cycle
+ purpose/delivery policy разрешает отправку
```

Возраст, другой origin cycle или прежний filename не являются блокировкой.

Tool result для historical artifact явно сообщает provenance:

```json
{
  "artifact_id": "art_...",
  "filename": "report.csv",
  "scope": "session_history",
  "origin_input_batch_id": "ibat_...",
  "origin_cycle_id": "cycle_...",
  "version_number": 3,
  "activation_reason": "catalog_result"
}
```

Prompt/runtime может предупреждать модель:

```text
Выбран артефакт из предыдущего цикла; проверьте, что пользователь действительно
просит повторно использовать эту exact version.
```

Это soft correctness warning, а не запрет.

## BW-9.7. Preventing accidental stale selection

Проблема старого прогона формулируется так:

```text
files-only batch without instruction
+ previous deliverables already visible
→ LLM selected familiar old artifact IDs
```

Исправление состоит из двух независимых мер:

1. AUTO files-only drafts не запускают agent cycle без explicit `/send`.
2. Base manifest не включает всю историю автоматически; historical artifacts
   требуют текущего catalog/search/get activation.

Runtime не определяет «нужность» файла по filename similarity и не запрещает
историю целиком.

## BW-9.8. Exact version semantics

Любая операция использует exact immutable `artifact_id`.

```text
lineage latest
filename
creation time
```

не заменяют exact version reference.

Если пользователь просит «последнюю версию», manager tool сначала разрешает
lineage/latest по authoritative store, возвращает exact artifact ID и только
после этого version активируется.

## BW-9.9. Context compaction

После cycle compaction:

- current activation records сохраняются bounded runtime state;
- content исторических artifacts не удерживается в LLM context;
- повторное чтение идёт через exact artifact tools;
- manifest summary не превращается в source of truth content.

## BW-9.10. Future scaling

```text
v0.4 filesystem
→ exact list/get/search metadata and bytes

v0.5 PostgreSQL + pgvector
→ semantic retrieval activates exact artifacts/chunks

v0.6 workers
→ background extraction/indexing, same exact refs

v0.8 multi-user
→ workspace/role authorization around same scopes
```

Таким образом, backend и discovery становятся богаче, но базовая модель
`authorized → discovered → activated → used/selected` сохраняется.
