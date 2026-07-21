from __future__ import annotations

from pathlib import Path

DOC_PATH = Path('docs/design_document.md')


def replace_between(text: str, start: str, end: str, replacement: str) -> str:
    start_index = text.find(start)
    if start_index < 0:
        raise RuntimeError(f'start marker not found: {start}')
    end_index = text.find(end, start_index)
    if end_index < 0:
        raise RuntimeError(f'end marker not found: {end}')
    return text[:start_index] + replacement.rstrip() + '\n\n' + text[end_index:]


def replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'expected one occurrence, found {count}: {old[:120]!r}')
    return text.replace(old, new, 1)


UNIFIED_SECTION = r'''# Часть VIII-E. v0.4 Unified Input and Artifact Architecture

## 89. Назначение и архитектурные инварианты

`v0.4-file-artifacts` и `v0.4-input-runtime` являются двумя последовательными,
но тесно связанными обновлениями. Они используют один transport-independent
контракт входных данных и артефактов, однако реализуются отдельно:

```text
v0.4-file-artifacts
→ initial input, file ingress, artifact workspace, processing и delivery

v0.4-input-runtime
→ дополнительные committed InputBatch во время active cycle,
   CycleInbox, safe checkpoints и session coordination
```

Общая формула:

```text
client transport event
≠ logical user input
≠ committed InputBatch
≠ agent cycle
≠ artifact payload
≠ client delivery
```

Основные инварианты:

1. Клиент не создаёт authoritative runtime IDs, statuses, provenance и refs.
2. Агент запускается только на полностью committed `InputBatch`.
3. Файлы не передаются в JSON как base64, локальные пути или download URL.
4. Канонические байты хранятся в `ContentStore`; artifact domain хранит
   пользовательскую семантику, lineage, immutable versions и `content_id`.
5. `artifact_id` обозначает точную неизменяемую версию, а не mutable latest file.
6. После commit `InputBatch` и `ArtifactVersion` не изменяются.
7. Client-specific grouping заканчивается до `MessageProcessor` и Agent Runtime.
8. `CycleInbox` принимает только ссылки на committed batches, а не Telegram/Web
   transport messages.
9. Agent Runtime не вызывает Telegram/Web API и не владеет bot tokens,
   multipart streams или client download URLs.
10. Выполнение задачи, доставка текста и доставка каждого файла являются
    разными наблюдаемыми lifecycle.
11. Все входные файлы, tool outputs и extracted document text считаются
    недоверенными данными и не становятся инструкциями для агента.
12. Filesystem-реализация `v0.4` рассчитана на один runtime process; межпроцессная
    атомарность и distributed locks относятся к `v0.5/v0.6`.

Архитектура должна поддерживать Telegram, будущий Web, CLI и новые клиенты без
изменения agent loop и artifact manager tools.

---

## 90. Сквозной lifecycle

### 90.1. Входной путь

```text
Client transport
→ durable ClientIngressEvent
→ client grouping policy
→ InputBatchDraft
→ streaming ArtifactIngressService
→ committed Content + input ArtifactVersion
→ atomic CommittedInputBatch
→ CycleAdmissionService
   ├─ session idle    → initial ActiveAgentCycle
   └─ cycle active    → CycleInbox
→ bounded AgentInputBatch / input_batch_update
→ LLM + manager/MCP tools
```

### 90.2. Обработка и создание результата

```text
input artifact
→ artifact manager tool или artifact binding в mcp_call_tool
→ internal text operation / external MCP processor
→ ContentRef или ArtifactCandidate
→ explicit artifact promotion/version creation
→ validation
→ exact artifact version selected for delivery
```

### 90.3. Исходящий путь

```text
AgentResult(final text + ArtifactDeliveryRef[])
→ durable ClientResponseOutbox
→ client-specific DeliveryPlanner
→ Telegram/Web/CLI sink
→ per-item delivery receipt
→ delivered / failed / unknown
```

Agent cycle не запускается повторно из-за ошибки доставки. Delivery retry не
должен повторять LLM-работу или tool side effects.

---

## 91. Общие идентификаторы и входные модели

Opaque IDs:

```text
evt_*   ingress event
ibat_*  input batch
aln_*   artifact lineage
art_*   immutable artifact version
cnt_*   canonical content payload
cand_*  artifact candidate from tool output
dlv_*   client delivery
resp_*  durable client response
ibox_*  CycleInbox item
ctrl_*  session control command
```

### 91.1. `ClientIngressEvent`

Одно событие транспортного клиента:

```python
class ClientIngressEvent(BaseModel):
    event_id: str
    client_type: ClientType
    client_instance_id: str

    conversation: ClientConversationRef
    sender: ClientSenderRef

    source_update_id: str | None
    source_message_id: str
    source_group_id: str | None
    reply_to_message_id: str | None

    occurred_at: datetime
    text_parts: list[IngressTextPart]
    attachment_slots: list[IngressAttachmentSlot]

    locale: str | None
    admission_mode: InputAdmissionMode = InputAdmissionMode.AUTO
    metadata: dict[str, Any]
```

`conversation` содержит нормализованные `conversation_id` и optional
`thread_id`. `sender` содержит stable external principal ID. Session key строит
server-side `SessionKeyPolicy`; transport metadata не используется как
произвольный источник `session_id`.

### 91.2. Text и attachment parts

```python
class IngressTextPart(BaseModel):
    kind: Literal["message_text", "caption"]
    text: str
    attachment_slot_ids: list[str]


class IngressAttachmentSlot(BaseModel):
    slot_id: str
    media_kind: str
    original_filename: str | None
    declared_mime_type: str | None
    declared_size_bytes: int | None
    transport_locator: ClientAttachmentLocator
    upload_field_name: str | None
    metadata: dict[str, Any]
```

Caption сохраняет связь с конкретным attachment/message. Несколько captions не
склеиваются безусловно в одну строку.

### 91.3. Server ownership

Клиент может передать correlation/idempotency key, текст, порядок частей,
filename, declared MIME и bytes/locator. Только runtime создаёт:

```text
event_id, input_batch_id, content_id, lineage_id, artifact_id,
session_id, sequence_number, hashes, detected MIME, statuses,
provenance, cycle association и delivery IDs.
```

Web-клиент может отправить структурированный `ClientInputEnvelope`, но не готовый
authoritative `CommittedInputBatch`.

---

## 92. Durable ingress и `InputBatch`

### 92.1. Сохранение события до acknowledgement

Webhook/HTTP request получает success только после durable persistence события:

```text
validate client authentication
→ normalize event
→ IngressEventStore.save_if_absent(idempotency_key)
→ atomic commit
→ 2xx / InputSubmissionReceipt
→ asynchronous ingress dispatcher
```

Если событие невозможно надёжно сохранить, transport возвращает retryable
ошибку. Нельзя отвечать Telegram `200 OK` после одного `asyncio.create_task`,
когда update ещё существует только в памяти процесса.

`IngressEventRecord`:

```text
received → normalized → dispatched
                    ↘ failed
```

Telegram idempotency key:

```text
telegram + bot_instance_id + update_id
```

Web idempotency key scoped к authenticated principal/session.

### 92.2. `InputBatchDraft`

Mutable server-side draft:

```python
class InputBatchDraft(BaseModel):
    input_batch_id: str
    session_id: str
    client_type: ClientType
    conversation: ClientConversationRef
    sender: ClientSenderRef

    grouping_mode: InputGroupingMode
    grouping_key: str
    state: InputBatchDraftState

    source_events: list[InputEventRef]
    text_parts: list[InputTextPart]
    attachment_parts: list[InputAttachmentPart]

    opened_at: datetime
    last_event_at: datetime
    quiet_deadline: datetime | None
    sealing_deadline: datetime | None
    maximum_deadline: datetime | None

    response_route: ClientResponseRoute
```

State machine:

```text
collecting
→ sealing
→ ingesting
→ ready_to_commit
→ committed

collecting/sealing/ingesting
→ failed | cancelled | abandoned
```

Новый элемент во время `sealing` возвращает draft в `collecting` и обновляет
таймер. `maximum_deadline` не позволяет draft зависнуть навсегда.

### 92.3. `CommittedInputBatch`

Immutable logical user input:

```python
class CommittedInputBatch(BaseModel):
    input_batch_id: str
    session_id: str
    client_type: ClientType
    sequence_number: int

    source_event_ids: list[str]
    text_parts: list[InputTextPart]
    artifact_refs: list[str]
    referenced_artifact_refs: list[str]

    admission_mode: InputAdmissionMode
    response_route: ClientResponseRoute

    continuation_of_batch_id: str | None
    correction_of_batch_id: str | None

    committed_at: datetime
    commit_reason: InputBatchCommitReason
    content_fingerprint: str
```

После commit нельзя добавлять файлы, менять текст, порядок, conversation или
refs. Поздний fragment/edit создаёт continuation/correction batch.

### 92.4. Atomic commit

```text
1. draft lock acquired
2. все expected events зарегистрированы
3. все attachment states == stored
4. content/artifact refs проверены
5. batch/file/count/size limits проверены
6. immutable payload сформирован
7. committed manifest опубликован atomic replace
8. input_batch_committed event записан
9. admission dispatcher получает batch
```

Если один файл не сохранён, агент не запускается с частичным набором. Уже
созданные orphan contents остаются недоступны runtime и очищаются sweeper после
grace period.

### 92.5. `InputBatchStore`

```text
storage/input_batches/ibat_*/
  draft.json
  committed.json
  events/evt_*.json
```

`committed.json` authoritative. Commit идемпотентен. После crash committed, но
ещё не consumed batch повторно передаётся admission dispatcher.

---

## 93. Практическая группировка по клиентам

`InputBatchAssembler` реализует общую state machine. Client adapter предоставляет
`InputGroupingPolicy`:

```python
class InputGroupingPolicy(Protocol):
    def resolve_grouping(
        self,
        event: ClientIngressEvent,
        open_drafts: Sequence[InputBatchDraftRef],
    ) -> GroupingDecision: ...
```

### 93.1. Web

Web отправляет atomic multipart request:

```text
manifest JSON + file parts
→ один draft
→ stream всех частей
→ immediate commit после полной валидации
```

При disconnect до commit draft становится failed/abandoned, временные payloads
не публикуются, agent cycle не запускается. Retry с тем же `Idempotency-Key`
возвращает прежний result/receipt и не повторяет side effects.

Большой Web upload в будущем может использовать resumable upload session, но
итоговый `CommittedInputBatch` остаётся тем же.

### 93.2. Telegram: text и command

- text без открытого attachment draft → immediate batch;
- command → отдельный control/input event и не объединяется с файлами;
- text при ровно одном открытом standalone attachment draft → instruction к
  draft и explicit/fast commit;
- text после commit → новый batch.

### 93.3. Telegram media group

Telegram присылает элементы альбома отдельными messages с общим
`media_group_id`. Grouping key:

```text
bot instance + conversation + thread + sender + media_group_id
```

Порядок определяется `source_message_id`, а не временем обработки webhook.
Окончание группы определяется:

```text
quiet timeout
→ sealing grace
→ commit
```

Отдельного `album_finished` event нет. Новая часть в sealing возвращает draft в
collecting. Часть, пришедшая после commit, создаёт continuation batch с причиной
`late_media_group_fragment`; committed batch не открывается повторно.

### 93.4. Telegram standalone files

Без `media_group_id` намерение неоднозначно. Поэтому у пользователя в одной
conversation/thread допускается максимум один standalone attachment draft.
После первого файла бот показывает один status с действиями:

```text
Начать обработку | Добавить ещё | Отменить
```

Последующие файлы и текст добавляются в draft. Explicit commit предпочтителен;
maximum timeout выполняет bounded auto-commit. Filename не используется для
автоматического определения версии.

### 93.5. Telegram file provider

Webhook не держит соединение во время скачивания файла. После durable event
`ArtifactIngressService` получает bytes через закрытый `ClientFileProvider`:

```text
Gateway/Ingress
→ authenticated TelegramFileProvider
→ getFile(file_id)
→ stream в ContentStore
```

Bot token и временный Telegram download URL не покидают Telegram service и не
сохраняются в event metadata. Исходные filename/MIME сохраняются из Message,
поскольку transport download metadata может быть неполной.

### 93.6. Duplicate, ordering, edit и reply

- duplicate update отбрасывается по idempotency key;
- per-conversation/sender lock защищает порядок и draft mutation;
- edited message до commit обновляет draft;
- edit после commit создаёт correction batch;
- reply разрешается через `ClientMessageBindingStore`;
- reply может явно повторно ввести ранее доставленный/input artifact в текущий
  или новый cycle, но только после ownership checks.

### 93.7. Session scope

Recommended defaults:

```text
Telegram private chat     → conversation
Telegram group            → conversation + sender
Telegram topic/thread     → conversation + thread + sender
Web                       → authenticated principal + explicit session
```

Collaborative group session включается явно. События разных senders не
объединяются в один draft автоматически.

---

## 94. Streaming, лимиты и безопасность ingress

`ContentStore` расширяется:

```python
async def save_stream(
    chunks: AsyncIterator[bytes],
    *,
    source_type: str,
    source_name: str | None,
    mime_type: str | None,
    metadata: dict[str, Any],
    max_size_bytes: int,
) -> ContentRef: ...

async def iter_content(
    content_id: str,
    *,
    chunk_size: int,
) -> AsyncIterator[bytes]: ...
```

`save_stream` пишет temporary payload, одновременно считает size/hash, проверяет
лимит и публикует content atomic replace. Gateway/API не загружают крупный файл
полностью в RAM.

Лимиты многоуровневые:

```text
per-file bytes
per-batch total bytes
attachments/events/text per batch
open drafts per principal
queued batches/bytes per session
global concurrent downloads
temporary storage budget
client transport limit
```

Filename sanitization удаляет path components/control chars и не влияет на
storage path. Declared MIME недоверенный. Сохраняются `declared_mime_type`,
`detected_mime_type`, `format_id`; для известных форматов выполняется bounded
signature/container validation. Core не исполняет scripts/macros и не делает
автоматическую распаковку архивов или Office containers.

`photo` и `document` считаются разными transport media kinds. Telegram photo
может быть преобразовано платформой; provenance отмечает
`transport_transformation_possible=true`. Для точного возврата файл по умолчанию
отправляется как document.

---

## 95. Admission и session coordination

```python
class CycleAdmissionService(Protocol):
    async def submit(
        self,
        batch: CommittedInputBatch,
    ) -> InputSubmissionResult: ...
```

Реализации:

```text
DirectCycleAdmissionService (v0.4-file-artifacts)
  idle session  → start cycle
  active cycle  → session_busy

InboxCycleAdmissionService (v0.4-input-runtime)
  idle session  → start cycle
  active cycle  → enqueue CycleInbox
```

`SessionRuntimeState`:

```text
session_id
generation
active_cycle_id
resumable_cycle_id
next_input_sequence
state: idle | running | waiting_user | finalizing
```

Один per-session lock защищает start/enqueue, active cycle pointer,
`WAITING_USER`, finalization и terminal commit. Перед terminal commit runtime под
тем же lock повторно проверяет inbox; принятый batch не может потеряться в гонке
между последней проверкой и `DONE`.

`InputAdmissionMode`:

```text
auto
continue_cycle
new_cycle
```

Web может указывать mode явно. Telegram reply на active-cycle message означает
`continue_cycle`, `/new` — `new_cycle`, обычный input — `auto`. Параллельные
независимые cycles одной session в `v0.4` не поддерживаются.

---

# Часть VIII-F. v0.4-file-artifacts

## 96. Artifact domain и storage

### 96.1. Разделение сущностей

```text
Content       — immutable canonical bytes/text
StoredResult  — сохранённый tool result
Artifact      — пользовательский/рабочий/выходной именованный файл
```

Artifact payload хранится только в `ContentStore`. `ArtifactStore` хранит
lineage/version metadata и `content_id`, но не вторую копию `file.bin`.

### 96.2. IDs и linear version history

```text
aln_* — logical artifact lineage
art_* — exact immutable version
cnt_* — exact payload
```

```text
aln_report
 ├─ art_v1 → cnt_a
 ├─ art_v2 → cnt_b
 └─ art_v3 → cnt_c (current head)
```

В `v0.4` история линейная. Mutation принимает `current_artifact_id` как optimistic
concurrency token. Если head уже изменился, возвращается
`artifact_version_conflict`; silent merge и branching запрещены.

Конвертация формата обычно создаёт новый lineage. Например DOCX → PDF не является
новой версией DOCX.

### 96.3. Models

`ArtifactLineage`:

```text
artifact_lineage_id
session_id
created_cycle_id
current_artifact_id
current_version
purpose: input | working | deliverable
status: active | archived
title
created_at / updated_at
metadata
```

`ArtifactVersion`:

```text
artifact_id
artifact_lineage_id
version
parent_artifact_id
content_id
filename
format_id
encoding
declared_mime_type
detected_mime_type
size_bytes
content_hash
created_cycle_id
created_at
provenance
metadata
```

Provenance runtime-owned и включает origin, source content/result/artifact refs,
tool call, plan/node association и creator. LLM не задаёт provenance напрямую.

### 96.4. Filesystem layout и atomic head

```text
storage/
  contents/cnt_*/content.bin + metadata.json
  artifacts/lineages/aln_*/metadata.json
  artifacts/versions/art_*/metadata.json
  deliveries/cycles/<cycle_id>.json
```

Новая version metadata публикуется раньше atomic update lineage head. Orphan
version, не указанная committed manifest, не доступна через public service и
очищается позднее.

---

## 97. Масштабируемый format registry

`format_id` не является закрытым Enum. Форматы регистрируются через
`ArtifactFormatSpec`:

```python
ArtifactFormatSpec(
    format_id="docx",
    canonical_mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    extensions=(".docx",),
    content_kind="binary_container",
    capabilities={...},
)
```

Capabilities:

```text
can_create_inline
can_read_text
can_search_text
can_replace_text
can_patch_text
can_deliver
can_bind_to_tool
requires_external_processor
```

Уровни `v0.4`:

1. Native text: TXT, Markdown, JSON, CSV, source/config text — create/read/search/
   replace/patch/version/deliver.
2. Managed binary: PDF, DOCX, XLSX, PPTX, PNG, JPEG, WebP — store/version/bind/
   deliver; semantic processing через MCP.
3. Opaque allowed binary — store/version/deliver при policy.

JSON и другие structured text проходят syntax validation после create/edit.
Password-protected/unsupported container может храниться, но получает ограниченные
capabilities.

---

## 98. Artifact manager functions и runtime state

Command-oriented tools:

```text
artifact_list
artifact_get
artifact_read_text
artifact_search_text
artifact_create_text
artifact_create_from_content
artifact_replace_text
artifact_patch_text
artifact_create_version_from_content
artifact_set_delivery
```

Правила:

- tools не принимают session/cycle/client type/local path;
- runtime проверяет current-cycle access set;
- read/search bounded и возвращают offsets/excerpts;
- exact patch использует `old_text`, `new_text`, `expected_occurrences`;
- fuzzy/line-number patch отсутствует;
- mutation создаёт новую immutable version;
- unknown refs и stale head отклоняются;
- `purpose=deliverable` может автоматически выбрать current version для delivery;
- обновление deliverable переносит selection на новый committed head.

`runtime_iteration_state` получает bounded `artifact_state`:

```json
{
  "available_count": 3,
  "items": [
    {
      "artifact_id": "art_...",
      "artifact_lineage_id": "aln_...",
      "version": 2,
      "filename": "report.md",
      "purpose": "deliverable",
      "format_id": "markdown",
      "size_bytes": 12400,
      "is_current": true,
      "selected_for_delivery": true
    }
  ],
  "items_truncated": false
}
```

Content, full provenance и version history автоматически в LLM context не
попадают. Compactor сохраняет только runtime-owned artifact refs; authoritative
state восстанавливается из store после resume/compaction.

---

## 99. Работа с внутренними и внешними tools

### 99.1. Artifact binding в `mcp_call_tool`

```json
{
  "artifact_inputs": [
    {
      "artifact_id": "art_...",
      "argument_name": "input_file",
      "transfer_mode": "auto"
    }
  ]
}
```

Modes:

```text
auto | local_path | inline_text | base64
```

Runtime проверяет schema, ownership, format capability и размер. `local_path`
доступен только доверенному local executable/stdio server. Remote HTTP MCP не
получает путь локальной машины. LLM не получает materialized path.

### 99.2. Per-call workspace

```text
workspace/<tool_call_id>/
  inputs/   read-only copies
  outputs/  allowed output root
  temp/
```

Canonical content immutable. In-place processor работает с копией. Output path
принимается только внутри выделенного workspace и импортируется в `ContentStore`.
Произвольный путь из tool output отклоняется. Workspace имеет lease и cleanup.

### 99.3. MCP output normalization

Text остаётся tool result. Embedded image/audio/resource/blob сохраняется как
content candidate. Binary/base64 не попадает в messages_for_llm.

```json
{
  "type": "artifact_candidate",
  "candidate_id": "cand_...",
  "content_id": "cnt_...",
  "filename": "result.docx",
  "mime_type": "...",
  "size_bytes": 18422,
  "source_tool_call_id": "...",
  "needs_promotion": true
}
```

Агент явно создаёт новый artifact либо version. Каждый технический output не
становится пользовательским artifact автоматически. Partial tool success сохраняет
полезные candidates и отдельно фиксирует limitations.

Предпочтительный контракт будущих Word/PDF/PowerPoint/Excel MCP servers —
embedded resource или structured output manifest, а не произвольный local path.

---

## 100. Delivery, outbox и client bindings

Delivery selection является отдельной сущностью, а не mutable полем immutable
ArtifactVersion:

```text
selected → delivering → delivered | failed | unknown
```

`AgentResult` содержит `ArtifactDeliveryRef[]`, но не bytes. Перед `DONE` runtime
проверяет, что selected versions committed, доступны session и проходят integrity
check.

`ClientResponseOutbox` сохраняется до terminal publication:

```text
pending → claimed → partially_delivered → delivered | failed
```

Crash после LLM final answer не теряет result; delivery продолжается после
restart без повторного agent cycle.

Telegram/Web transport получает `delivery_id`, потоково читает content и пишет
receipt. Execution success, text delivery и каждый artifact delivery наблюдаются
раздельно. Неопределённый сетевой timeout после начала Telegram upload получает
`unknown`; бесконечный автоматический retry запрещён, поскольку может создать
дубликат.

`ClientMessageBindingStore` связывает incoming/outgoing client message IDs с
batch/cycle/artifact/delivery/waiting request. Reply позволяет безопасно сослаться
на ранее доставленный artifact после ownership check.

Client DeliveryPlanner учитывает capabilities/limits. Telegram по умолчанию
использует document, а media group только для совместимых типов/количества. Web
выдаёт authenticated streaming download endpoint; опасные HTML/SVG/script formats
не открываются inline по умолчанию.

Artifact progress/trace:

```text
artifact_ingested
artifact_created
artifact_version_created
artifact_version_conflict
artifact_validation_failed
artifact_candidate_saved
artifact_tool_input_materialized
artifact_tool_input_released
artifact_delivery_selected
artifact_delivery_started
artifact_delivery_done
artifact_delivery_failed
```

Events содержат IDs/version/filename/format/size и plan provenance, но не content,
base64, patch text или temporary paths.

---

# Часть VIII-F2. v0.4-input-runtime

## 101. `CycleInbox`

`CycleInbox` хранит только committed batch refs:

```python
class CycleInboxItem(BaseModel):
    inbox_item_id: str
    cycle_id: str
    input_batch_id: str
    sequence_number: int

    status: Literal[
        "queued", "claimed", "applied", "failed", "cancelled"
    ]

    claim_token: str | None
    claim_expires_at: datetime | None
    enqueued_at: datetime
    claimed_at: datetime | None
    applied_at: datetime | None
```

At-least-once delivery компенсируется idempotent consumption по
`input_batch_id`. Просроченный claim после restart возвращается в queued.

`ActiveAgentCycle` хранит bounded replay state:

```text
applied_input_batch_ids
last_applied_inbox_sequence
```

Unread batches остаются authoritative в `CycleInboxStore` и не включаются в
cycle compaction summary.

---

## 102. Safe checkpoints и control plane

Обычный batch применяется только:

1. перед следующим main LLM request;
2. после полного assistant tool-call block и всех role=tool results;
3. перед `WAITING_USER`;
4. перед final processing/terminal commit;
5. после resume перед первой новой iteration.

Новый input не вставляется между assistant tool call и соответствующим tool
result. Если batch пришёл во время LLM call, уже выбранный tool action не
игнорируется: tool block завершается, затем выполняется checkpoint.

Перед `DONE`/`WAITING_USER` inbox проверяется повторно. Если batch уже принят,
terminal action подавляется, batch применяется, cycle продолжается.

Несколько batches представляются одной runtime-generated user message, но границы
сохраняются:

```json
{
  "type": "input_batch_update",
  "batches": [
    {
      "input_batch_id": "ibat_...",
      "text_parts": [],
      "artifacts": [],
      "continuation_of_batch_id": null,
      "correction_of_batch_id": null
    }
  ],
  "runtime_generated": true
}
```

Управляющие команды отделены от обычного FIFO:

```text
cancel_cycle
cancel_draft
reset_session
start_new_cycle
commit_draft
```

`SessionControlInbox` проверяется до side-effect tool, между последовательными
tool calls, перед finalization и delivery. Если `/cancel` пришёл после первого из
нескольких native tool calls, оставшиеся calls получают корректные cancelled
`role=tool` results, чтобы не нарушить OpenAI-compatible sequence.

---

## 103. Resume, correction и finalization races

Применение inbox replay-safe:

```text
claim item
→ проверить applied_input_batch_ids
→ добавить one input_batch_update
→ добавить runtime-owned artifact refs
→ persist cycle replay state
→ mark item applied
```

После crash повтор не добавляет batch второй раз.

`WAITING_USER`:

```text
inbox уже содержит ответ
→ не переходить в waiting
→ применить batch

waiting уже committed
→ новый AUTO batch enqueue
→ resume того же cycle
```

Edited message после commit создаёт correction batch. Late album fragment создаёт
continuation batch. Они не изменяют исторический committed input.

Finalization race закрывается одним session lock:

```text
begin finalization
→ lock
→ final inbox/control recheck
→ если input есть: cancel finalization и continue
→ иначе: durable final result + outbox + terminal state
```

Нельзя доставить ответ, который заведомо игнорирует уже accepted batch.

---

## 104. Recovery, configuration, scope и acceptance

После restart восстанавливаются:

```text
received/dispatched ingress events
open/sealing/ingesting drafts
committed-but-unconsumed batches
expired CycleInbox claims
active/resumable cycle references
pending client response outbox
delivery states/tool workspaces cleanup
```

Basic sweeper удаляет только expired temporary uploads, abandoned drafts, orphan
content после grace period и expired workspaces. Committed artifacts автоматически
не удаляются.

Configuration categories:

```text
artifacts:
  max_artifacts_per_cycle
  max_versions_per_lineage
  max_artifact_size_bytes
  max_inline_text_chars / max_read_chars / max_search_matches
  max_patch_operations
  max_runtime_artifact_summaries
  allow_opaque_binary
  auto_select_deliverables

input_runtime:
  max_events_per_batch
  max_attachments_per_batch
  max_batch_total_bytes
  max_open_drafts_per_principal
  max_queued_batches/bytes_per_session
  claim_lease_seconds

telegram_input:
  album_quiet_timeout_seconds
  album_sealing_grace_seconds
  album_maximum_wait_seconds
  standalone_attachment_maximum_wait_seconds
```

Concrete timing/size values определяются тестами и client capabilities, но сами
лимиты обязательны.

Acceptance `v0.4-file-artifacts`:

- atomic Web text+files ingress;
- Telegram text, single file, media group и standalone draft;
- no cycle start before full batch commit;
- immutable input artifact and versioned edits;
- native text create/read/search/replace/patch;
- binary bind to trusted MCP processor;
- candidate promotion/version creation;
- deliverable selected, persisted in outbox and streamed to Telegram/Web;
- crash/retry does not lose accepted input or duplicate artifact versions.

Acceptance `v0.4-input-runtime`:

- batch during LLM/tool execution is queued, not injected concurrently;
- safe checkpoint preserves assistant/tool protocol;
- accepted input suppresses stale DONE/WAITING_USER;
- multiple queued batches preserve order and boundaries;
- duplicate/replayed batch is applied once;
- `/cancel` control command is observed before next side effect;
- finalization/enqueue race is closed under session lock;
- restart restores inbox claims and resumable cycle state.

Не входят в `v0.4`:

```text
semantic RAG по artifact history
PDF/DOCX/XLSX/PPTX extraction in core
OCR, previews, thumbnails
antivirus/macro sandbox
object storage и presigned URLs
resumable large Web upload
branching/merge artifact versions
cross-cycle automatic artifact search
multi-process/distributed admission
background conversion workers
automatic retry of ambiguous client delivery
```
'''

ACCEPTANCE_SECTION = r'''## 107. Acceptance criteria v0.4

### Small/large/oversized result и cycle compaction

Сохраняются критерии `v0.4-result-compaction` и `v0.4-cycle-compaction`:

```text
small result → inline → immediate use
large result → original persisted → compact/store_only → no raw in context
oversized result → needs_retrieval=true → no false complete summary
context trigger → closed atomic segment → one CycleWorkingMemory
repeated compaction → one visible generation → no summary tree
```

### DAG

```text
invalid mutation/revision
→ validation/conflict
→ stored plan remains intact
```

```text
active plan + no in_progress node
→ substantive tool call blocked
```

```text
active unresolved plan + final answer
→ reconciliation or controlled resumable handoff
```

### Atomic client input

```text
Web multipart text + files
→ all streams stored and validated
→ one committed InputBatch
→ one cycle
```

```text
one attachment fails
→ batch failed
→ cycle not started with partial input
```

### Telegram grouping

```text
media_group messages
→ one draft by media_group_id
→ quiet + sealing
→ one committed InputBatch
```

```text
late media-group fragment after commit
→ continuation batch
→ original batch remains immutable
```

```text
standalone files + text
→ one explicit attachment draft
→ commit button/command or bounded timeout
```

### Artifact versioning

```text
agent edits text artifact
→ new art_* version in same aln_*
→ original exact version remains available
→ stale current_artifact_id rejected
```

```text
DOCX processor returns modified bytes
→ content candidate
→ explicit new version
→ no arbitrary local path in LLM context
```

### Active-cycle input

```text
batch during LLM/tool execution
→ durable CycleInbox
→ current atomic action finishes
→ batch applied at safe checkpoint
```

```text
batch accepted before terminal commit
→ stale DONE/WAITING_USER suppressed
→ cycle continues
```

```text
duplicate input/inbox replay
→ applied once by stable IDs
```

### Delivery

```text
final answer + deliverable artifact
→ durable response outbox
→ client-specific streaming delivery
→ independent text/file receipts
```

```text
process restart after final LLM answer
→ outbox replay
→ no repeated agent cycle
```

### Recovery and isolation

```text
restart
→ persisted drafts/batches/inbox/outbox recovered
```

```text
group chat users
→ session/grouping policy prevents accidental cross-user file access
```

---'''

TRANSFER_SECTION = r'''## 108. Что переносится

В `v0.5`:

- PostgreSQL implementations для content/artifact/input/inbox/outbox stores;
- transactional session admission и terminal commit;
- persistent artifact lineage/version/delivery relations;
- session/cross-cycle artifact discovery с exact authorization;
- lazy extraction сложных файлов;
- page/sheet/slide/section representations;
- persistent chunk cache и embeddings;
- keyword/semantic/hybrid RAG;
- search over old cycles/results/artifacts;
- provenance-aware retrieval по plan node и artifact version;
- processing oversized sources by parts;
- durable resume после process restart;
- optional object storage backend для payloads.

В `v0.6`:

- Redis/arq и distributed locks;
- durable distributed ingress/admission/CycleInbox;
- background workers для extraction, conversion, scanning и delivery;
- Notification/Delivery Service и durable event bus;
- automatic DAG scheduler и parallel nodes;
- background hierarchical summarization;
- resumable large uploads и remote signed URLs;
- antivirus/sandbox/preview pipelines;
- retention/garbage collection workers;
- multi-service AgentRun lifecycle;
- microservice runtime.

---'''

PG_ENTITIES_SECTION = r'''## 111. Минимальные сущности PostgreSQL

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

---'''

PG_ARTIFACT_BATCH_SECTION = r'''## 114. Artifacts и plans

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

---'''

V05_SCOPE_SECTION = r'''## 121. Что не входит в v0.5

- обязательная микросервисная архитектура;
- Redis как обязательная runtime dependency;
- distributed workers как обязательный execution path;
- automatic DAG scheduler;
- automatic parallel plan nodes;
- массовая eager-индексация всех files;
- сложное branching/merge artifact versions;
- полноценный multi-tenant shared workspace;
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

---'''

V06_SERVICES_SECTION = r'''## 124. Возможные сервисы

```text
Gateway / Client API
→ Telegram/Web/CLI ingress authentication
→ durable ClientIngressEvent
→ client file providers and response routes
→ Web AgentRun endpoints

Ingress / Session Coordination Service
→ InputBatchDraft assembly
→ batch commit/idempotency
→ session admission and control commands
→ CycleInbox routing

Agent Runtime Service
→ agent cycle
→ LLM iterations
→ safe checkpoints
→ working memory
→ plan/artifact decisions
→ canonical progress/trace events

MCP Tool Runtime Service
→ MCP servers
→ lifecycle/recovery
→ isolated per-call artifact workspaces
→ tool output normalization

Memory / Workspace Service
→ sessions/cycles/content/results
→ artifact lineages/versions
→ plans/provenance/RAG

Worker Service
→ extraction/chunking/embeddings
→ conversion/rendering/scanning
→ hierarchical summarization
→ retention/cleanup

Notification / Delivery Service
→ durable response outbox
→ progress/final text/artifacts
→ Telegram/Web/CLI-specific planners and sinks
→ delivery attempts/receipts/retry policy
```

Не все services обязаны появиться одновременно. Local development mode может
использовать in-process implementations тех же ports.

---'''

V06_INBOX_SECTION = r'''## 126. Durable ingress, `CycleInbox` и response outbox

```text
ClientIngressEvent persisted in PostgreSQL
→ draft assembled / payload stored
→ CommittedInputBatch
→ transactional admission
→ Redis event/queue
→ active runtime worker claims inbox item
→ safe checkpoint applies batch
→ application state persisted
```

Требования:

- at-least-once event/queue delivery;
- idempotent processing by event/batch/inbox IDs;
- ordering within session/cycle;
- claim leases and replay after worker restart;
- distributed session lock or transactional fencing token;
- no duplicate addenda, artifact versions or side effects;
- control commands delivered with higher checkpoint priority;
- finalization transaction rechecks input and writes response outbox;
- delivery worker claims `resp_*`/`dlv_*` independently from agent worker;
- execution success and client delivery success remain separate.

Redis является acceleration/event transport, но PostgreSQL остаётся source of
truth для batches, inbox application, terminal result и outbox.

---'''

ROADMAP_V04_OLD = r'''Ключевые результаты:

```text
ContentStore / ArtifactStore
filesystem backend
result/content/artifact refs
relative context budgets
result_handling with runtime override
single-pass result summary
oversized fallback
one CycleWorkingMemory
optional DAG artifact
file versioning and delivery
InputBatch
CycleInbox<InputBatch>
safe checkpoints
per-session lock
```'''

ROADMAP_V04_NEW = r'''Ключевые результаты:

```text
ContentStore + streaming payload IO
ArtifactStore with lineage + immutable versions
filesystem backend and atomic manifests
result/content/artifact refs
relative context budgets
result_handling with runtime override
single-pass result summary
oversized fallback
one CycleWorkingMemory
optional DAG artifact
ClientIngressEvent + durable IngressEventStore
InputBatchDraft + immutable CommittedInputBatch
Telegram album/sealing + standalone attachment draft
Web atomic multipart input
artifact format registry and manager tools
MCP artifact binding and candidate promotion
CycleAdmissionService
CycleInbox<CommittedInputBatch>
SessionControlInbox + safe checkpoints
per-session lock and finalization race guard
ClientResponseOutbox + delivery lifecycle
```'''

ROADMAP_V05_OLD = r'''Задачи:

```text
PostgreSQL + migrations
Postgres storage/repositories
hybrid raw content storage
lazy extraction/chunking
pgvector embeddings
keyword/semantic/hybrid retrieval
provenance-aware memory tools
persistent batch/inbox metadata
resume workspace after restart
```'''

ROADMAP_V05_NEW = r'''Задачи:

```text
PostgreSQL + migrations
Postgres implementations of storage/workspace/input contracts
transactional session admission and finalization
hybrid raw content/object storage
artifact lineage/version/delivery persistence
persistent ingress/draft/batch/inbox/control/outbox state
lazy file extraction and structured representations
pgvector embeddings
keyword/semantic/hybrid retrieval
provenance-aware memory/artifact/plan tools
resume full workspace after restart
```'''


def main() -> None:
    text = DOC_PATH.read_text(encoding='utf-8')

    text = replace_between(
        text,
        '# Часть VIII-E. v0.4-file-artifacts',
        '# Часть VIII-G. Реализация v0.4',
        UNIFIED_SECTION,
    )

    text = replace_between(
        text,
        '## 107. Acceptance criteria v0.4',
        '## 108. Что переносится',
        ACCEPTANCE_SECTION,
    )
    text = replace_between(
        text,
        '## 108. Что переносится',
        '# Часть IX. v0.5 — PostgreSQL, lazy indexing и RAG для agent workspace',
        TRANSFER_SECTION,
    )
    text = replace_between(
        text,
        '## 111. Минимальные сущности PostgreSQL',
        '## 112. Основные таблицы cycle',
        PG_ENTITIES_SECTION,
    )
    text = replace_between(
        text,
        '## 114. Artifacts и plans',
        '## 116. Lazy extraction и chunking',
        PG_ARTIFACT_BATCH_SECTION,
    )
    text = replace_between(
        text,
        '## 121. Что не входит в v0.5',
        '# Часть X. v0.6 — microservices, workers и distributed runtime',
        V05_SCOPE_SECTION,
    )
    text = replace_between(
        text,
        '## 124. Возможные сервисы',
        '## 124.1. Разделение Agent Runtime и client delivery',
        V06_SERVICES_SECTION,
    )
    text = replace_between(
        text,
        '## 126. Durable `CycleInbox`',
        '## 127. Automatic DAG scheduler',
        V06_INBOX_SECTION,
    )

    text = replace_once(text, ROADMAP_V04_OLD, ROADMAP_V04_NEW)
    text = replace_once(text, ROADMAP_V05_OLD, ROADMAP_V05_NEW)

    text = replace_once(
        text,
        '''### `v0.4-file-artifacts`\n\n- attachments in `UnifiedMessage`;\n- ingress;\n- read/create/version;\n- artifacts in `AgentResult`;\n- Telegram delivery.''',
        '''### `v0.4-file-artifacts`\n\n- durable `ClientIngressEvent` and initial `InputBatchDraft`;\n- streaming `ContentStore` ingress/egress;\n- artifact lineage and immutable versions;\n- format registry and bounded text operations;\n- artifact manager tools and MCP bindings;\n- candidate promotion and provenance;\n- `AgentResult.artifacts`;\n- durable response outbox and Telegram/Web delivery.''',
    )
    text = replace_once(
        text,
        '''### `v0.4-input-runtime`\n\n- `InputBatch`;\n- `CycleInbox<InputBatch>`;\n- safe checkpoints;\n- per-session lock;\n- addenda during active cycle.''',
        '''### `v0.4-input-runtime`\n\n- immutable `CommittedInputBatch`;\n- `CycleInbox<CommittedInputBatch>` with leases/idempotency;\n- session admission and runtime manifest;\n- safe checkpoints and batch update messages;\n- `SessionControlInbox`;\n- per-session lock and finalization race guard;\n- resume/replay after restart.''',
    )

    DOC_PATH.write_text(text, encoding='utf-8')


if __name__ == '__main__':
    main()
