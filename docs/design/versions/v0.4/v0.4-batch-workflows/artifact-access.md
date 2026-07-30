---
id: design.v0.4.batch-workflows.artifact-access
version: v0.4
spec_status: accepted
implementation_status: implemented
last_reviewed: 2026-07-30
---

# BW-9 — Artifact access, visibility and activation

## BW-9.1. Раздельные понятия

Artifact policy разделяет:

```text
authorization/access
≠ automatic prompt visibility
≠ activation in current cycle
≠ delivery selection
```

Исторические артефакты не запрещены. Агент может вернуться к exact immutable
version, доступной текущей session/workspace authority. Ограничивается объём
автоматической проекции в LLM context и неявность выбора.

## BW-9.2. Bounded active manifest

В agent runtime автоматически включается только bounded active manifest:

```text
current InputBatch artifacts
+ explicit current-cycle refs
+ artifacts created/modified in current cycle
+ selected deliverables
+ exact artifacts activated authoritative tools
```

Manifest содержит metadata, но не bytes, local paths, transport locators или
secrets. `ArtifactManifestItem` дополнительно проецирует:

```json
{
  "artifact_id": "art_...",
  "filename": "report.csv",
  "activation_reason": "catalog_result",
  "activation_scope": "session",
  "activation_source_operation_id": "artifact_list:session:0"
}
```

Content исторических artifacts читается только через exact read/search tools.

## BW-9.3. Catalog scopes

Production `artifact_list` принимает:

```python
scope: Literal["current", "session", "workspace"] = "current"
limit: int
cursor: str | None
```

### current

```text
→ exact refs уже active в текущем AgentCycle
→ не расширяет runtime authority
```

### session

```text
→ bounded exact versions, связанные с текущей session history
→ возвращённая page активируется в текущем cycle
```

### workspace

```text
→ bounded workspace-authorized catalog
```

В filesystem v0.4 отдельной multi-user workspace authority ещё нет. Поэтому
`workspace` возвращает явную projection:

```json
{
  "scope": "workspace",
  "effective_scope": "session",
  "workspace_scope_note": "filesystem_v0.4_workspace_equals_session"
}
```

Это не скрытая подмена контракта: v0.8 заменит backend authority, сохранив public
scope.

## BW-9.4. Opaque pagination

Catalog result возвращает `next_cursor`, если page truncated. Cursor:

- opaque для модели и клиента;
- содержит versioned server payload;
- привязан к requested scope;
- не принимается вместе с non-zero legacy offset;
- invalid/cross-scope cursor возвращает structured retryable validation error;
- не предоставляет дополнительную authority.

Полный неограниченный каталог одним tool result запрещён.

## BW-9.5. Activation record

Returned page items получают bounded runtime record:

```python
class ArtifactActivation(BaseModel):
    artifact_id: str
    cycle_id: str
    reason: ArtifactActivationReason
    scope: ArtifactCatalogScope
    source_operation_id: str | None
    activated_at: datetime
```

Причины:

```text
current_input_batch
explicit_reference
created_in_cycle
modified_in_cycle
catalog_result
search_result
client_selection
```

Activation:

- добавляет exact `artifact_id` в current-cycle authority;
- не копирует bytes;
- не создаёт новую version;
- не расширяет session boundary;
- ограничена `max_artifacts_per_cycle`;
- сохраняется в `ActiveAgentCycle.artifact_activations` и survives runtime
  compaction вместе с cycle state.

Если catalog page превысит оставшийся activation budget, операция отклоняется с
предложением уменьшить `limit` или сузить filters.

## BW-9.6. Historical read/search/delivery

После session/workspace catalog activation exact historical version проходит через
тот же existing pipeline:

```text
artifact_list(scope=session|workspace)
→ exact artifact_id activated current cycle
→ artifact_read_text / artifact_search_text
→ artifact_set_delivery(selected=true)
```

Delivery проверяет current runtime authority и immutable ID. Возраст, origin cycle
или знакомое filename сами по себе не блокируют и не разрешают отправку.

До activation historical ID остаётся недоступным existing read/search/delivery
controllers.

## BW-9.7. Session isolation

Catalog enumeration выполняется только через authoritative `context.session_id`.
Модель не передаёт произвольный `session_id`, path или workspace root.

```text
session A artifact
→ visible in session A session/workspace scope
→ absent in session B
```

`workspace` filesystem projection также не пересекает session boundary.

## BW-9.8. Preventing stale accidental selection

Старый проблемный сценарий:

```text
files-only batch without instruction
+ previous deliverables automatically visible
→ LLM selected familiar old IDs
```

Закрыт двумя независимыми мерами:

1. explicit files-only workflow требует `/send`;
2. base manifest не содержит весь history catalog — historical exact versions
   требуют current catalog activation.

Runtime не использует filename similarity и не запрещает историю целиком.

## BW-9.9. Production composition

Scoped behavior подключён отдельным `ArtifactAccessScopeMixin` в production MRO:

```text
FinalizingArtifactDeliveryPlanningMCPClient
→ ArtifactAccessScopeMixin
→ existing ArtifactMCPClient / delivery / planning layers
```

Compatibility-класс `ArtifactMCPClient` не меняет старые direct-construction
contracts. Production `artifact_list` получает scoped schema и
`ScopedArtifactToolController`; остальные manager tools остаются прежними и видят
активированные refs через existing access context.

Progress protocol зарегистрировал internal event:

```text
artifact_catalog_activated
```

Full structured activation evidence хранится в cycle trace/runtime state.

## BW-9.10. Validation

CI head `dac7085cb5cb8b3316b1aba7ded5e2b991a15a92`:

```text
compile: success
artifact suite: 223 tests, OK
storage suite: 41 tests, OK
plans suite: 45 tests, OK
planning suite: 19 tests, OK
API suite: 1 test, OK
```

Regressions проверяют:

- current scope не показывает неактивную history;
- session catalog активирует historical exact version;
- activated history доступна read и delivery;
- activation provenance попадает в bounded manifest;
- opaque cursor продолжает pagination;
- cursor нельзя перенести между scopes;
- filesystem workspace projection объявлена явно;
- другая session не получает metadata или activation.

## BW-9.11. Future scaling

```text
v0.4 filesystem
→ exact list/get/search metadata and bytes

v0.5 PostgreSQL + pgvector
→ semantic retrieval activates exact artifacts/chunks

v0.6 workers
→ background extraction/indexing, same exact refs

v0.8 multi-user
→ real workspace/role authorization around same scopes
```

Базовая модель остаётся:

```text
authorized → discovered → activated → used/selected
```

Новых `.env` или `mcp.config` keys BW-P4 не добавляет.
