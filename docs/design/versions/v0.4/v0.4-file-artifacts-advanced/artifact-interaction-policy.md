---
id: design.v0.4.artifact-interaction-policy
version: v0.4
spec_status: accepted
implementation_status: implemented
---
# v0.4 — Artifact interaction policy и version boundaries

> Подраздел обновления
> [`v0.4-file-artifacts-advanced`](README.md).

## AF-12. Agent input artifact manifest

### AF-12.1. Проблема opaque IDs

Передача только:

```json
{
  "artifact_refs": ["art_...", "art_..."]
}
```

заставляет модель сопоставлять длинные opaque IDs без имён и увеличивает риск
выдуманного или обрезанного ID.

### AF-12.2. Bounded manifest

Initial input и runtime state включают:

```json
{
  "input_artifacts": [
    {
      "artifact_id": "art_...",
      "artifact_lineage_id": "aln_...",
      "filename": "01_project_brief.md",
      "format_id": "markdown",
      "size_bytes": 668,
      "purpose": "input"
    }
  ]
}
```

Manifest:

- строится runtime из authoritative store;
- содержит только доступные exact versions;
- bounded по количеству и metadata size;
- не содержит content, transport locator или local path;
- восстанавливается после compaction из store/runtime state;
- не заменяет `artifact_list`.

### AF-12.3. Delivery manifest

Перед finalization runtime предоставляет bounded projection:

```text
created deliverables
selected deliverables
unselected requested outputs
delivery states
```

Это позволяет модели исправить забытый `artifact_set_delivery` до завершения.

---

## AF-13. Artifact purpose policy

### AF-13.1. `input`, `working`, `deliverable`

```text
input
→ пользовательский/внешний входной artifact

working
→ промежуточный рабочий artifact, не запрошенный как финальный output

deliverable
→ файл, который пользователь явно запросил создать/изменить/отправить
```

Если пользователь перечислил четыре выходных файла, все четыре создаются с
`purpose=deliverable`.

### AF-13.2. Source of truth

Purpose задаётся agent command в рамках разрешённой schema, но runtime может
валидировать несогласованность с explicit requested outputs.

Возможные меры:

- усиленное tool description/system rule;
- finalization warning;
- typed requested-output projection;
- optional safe promotion `working → deliverable` только по exact artifact ID и
  explicit selection.

Runtime не должен автоматически отправлять все `working` artifacts.

### AF-13.3. Version semantics

Новая version наследует purpose lineage, если explicit policy не разрешает
изменение.

Изменение purpose не меняет immutable content и должно быть отдельной
metadata/domain операцией либо новой delivery intent, а не silent mutation
ArtifactVersion.

---

## AF-14. Telegram native media foundation

### AF-14.1. Поддерживаемые semantic categories

Foundation предусматривает:

- documents;
- photos/images;
- audio files;
- voice messages;
- videos;
- video notes/circles;
- animations/GIF;
- stickers;
- locations/venues;
- contacts;
- forwarded messages/provenance.

Точная доступность определяется Telegram adapter capabilities и API version.

### AF-14.2. Exact payload first

В текущем scope:

```text
receive
→ validate metadata/size/policy
→ stream exact payload into ContentStore
→ create semantic InputPart / ArtifactRef
→ expose bounded metadata to agent
```

Не требуется автоматически:

- транскрибировать voice;
- распознавать sticker;
- извлекать frames из video;
- выполнять OCR;
- суммаризировать media.

### AF-14.3. Native response foundation

Если client capability поддерживается, semantic output может быть отрендерен
native:

```text
LocationOutputPart → location marker
VoiceOutputPart → voice message
VideoNoteOutputPart → video note
StickerOutputPart → sticker
AnimationOutputPart → animation
```

Agent не получает transport secrets и не вызывает Telegram SDK напрямую.

### AF-14.4. Web compatibility

Semantic parts не являются Telegram-only types.

Web-клиент может позднее поддержать:

- map/location component;
- audio player;
- image gallery;
- media cards;
- grouped downloads.

Если Web capability отсутствует, используется deterministic fallback.

---

## AF-15. Boundaries с `v0.4-input-runtime`

`v0.4-file-artifacts-advanced` не решает проблему сообщений пользователя во
время активного cycle.

Следующий раздел обязан отдельно спроектировать:

- `CycleInbox<CommittedInputBatch>`;
- active-cycle additions;
- safe checkpoints;
- continuation/new-cycle admission;
- side-query lane;
- interactive agent questions;
- reply/question/cycle bindings;
- provider-compatible context composition;
- control commands и finalization races.

### AF-15.1. Нельзя вставлять user input внутрь tool block

```text
assistant(tool_calls)
→ role=tool results for every call
→ only then user addition/checkpoint
```

Пользовательский batch, пришедший во время LLM/tool execution:

```text
→ durable CycleInbox
→ текущий atomic/tool block завершается
→ batch применяется в safe checkpoint
```

### AF-15.2. Несколько сообщений одной роли

Source of truth будущего runtime — canonical interaction event log, а не сырой
provider-specific список messages.

```text
UserInputCommitted
AssistantToolCallsRequested
ToolCallCompleted
UserAdditionCommitted
AssistantResponseGenerated
```

Будущий `ContextComposer` проецирует эти события в допустимую последовательность
конкретного OpenAI-compatible provider.

Нельзя сейчас закреплять архитектуру простым правилом:

```text
склеить все соседние user messages строкой
```

Потому что нужно сохранить:

- границы InputBatch;
- provenance;
- attachment refs;
- correction/continuation semantics;
- tool protocol blocks;
- provider compatibility.

### AF-15.3. Side-query lane

Краткий read-only вопрос во время работы может позднее выполняться отдельным
запросом по immutable snapshot:

```text
cycle_id
cycle_revision
bounded working state
relevant exact/derived evidence
user question
```

Side query:

- не меняет main context;
- не вызывает mutating tools;
- не изменяет plan;
- не создаёт artifacts;
- отвечает с указанием snapshot revision/limitations.

Классификацию `side question` / `task addition` нельзя полностью доверять одной
LLM.

Приоритет будущей policy:

```text
explicit client action/mode
→ reply/correlation metadata
→ deterministic rules
→ optional LLM classifier
→ при сомнении: task addition
```

Потерянное пользовательское требование опаснее лишнего context item.

---

## AF-16. Boundaries v0.5 и v0.6

### AF-16.1. v0.5

В `v0.5` переносятся:

- PostgreSQL stores для capability/client bindings, InputBatch/OutputBatch,
  outbox и receipts;
- persistent semantic media metadata;
- lazy extraction и chunking exact media/artifacts;
- transcript/OCR/extraction cache;
- embeddings и RAG по derived text;
- cross-cycle semantic retrieval;
- exact authorization по workspace/session;
- provenance-aware indexing source/derived artifacts.

Capability, InputPart, OutputPart и OutputBatch contracts должны сохраняться при
смене filesystem backend на PostgreSQL.

### AF-16.2. v0.6

В `v0.6` переносятся:

- durable distributed outbox;
- delivery workers;
- retries/backoff/deadlines;
- distributed locks;
- background transcription/OCR/conversion;
- media processing queues;
- resumable large upload/download;
- event bus;
- scheduler и long-running interaction orchestration;
- parallel safe processing.

Текущая in-process реализация должна использовать interfaces, которые могут быть
заменены worker-backed services без изменения Agent Runtime contracts.

---

