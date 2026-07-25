---
id: design.v0.4.semantic-interaction
version: v0.4
spec_status: accepted
implementation_status: partial
---
# v0.4 — Semantic Interaction Runtime

> Подраздел обновления
> [`v0.4-file-artifacts-advanced`](README.md).

## Место обновления в последовательности v0.4

`v0.4-file-artifacts-advanced` располагается между
`v0.4-file-artifacts` и `v0.4-input-runtime`.

Обновление расширяет файловый runtime до согласованного
transport-independent контура ввода-вывода, но не реализует заранее
`CycleInbox`, RAG, distributed workers и другие обязанности будущих версий.

---

### AF-1. Назначение обновления

`v0.4-file-artifacts` уже закрепляет:

```text
Client transport event
→ ClientInputEnvelope
→ InputBatchDraft
→ CommittedInputBatch
→ Agent Runtime
→ ArtifactVersion / ArtifactDeliveryRef
```

Реальные workflow-тесты показали, что входная половина этого контура уже может
собирать несколько файлов и отдельную инструкцию в один immutable
`CommittedInputBatch`.

Следующий архитектурный разрыв находится на границе между результатом агента и
пользовательским клиентом:

```text
AgentResult / ProgressEvent / ArtifactDeliveryRef
→ transport-specific сообщения и загрузки
```

Без дополнительного слоя возникают проблемы:

- один логический ответ превращается в набор независимых client messages;
- порядок выбранных deliverables может теряться;
- несколько файлов отправляются по одному, хотя клиент поддерживает grouping;
- `cycle_done` публикуется раньше фактической доставки artifacts;
- response reply может быть привязан к первому attachment, а не к инструкции;
- каждое новое событие открытого InputBatch может создавать отдельное status
  message;
- API и clients используют готовые локализованные строки вместо typed semantic
  events;
- transport-specific вложения не имеют общего semantic contract;
- LLM получает opaque artifact IDs без удобного bounded manifest;
- явно запрошенные выходные файлы могут ошибочно создаваться как `working`, а не
  `deliverable`.

Цель `v0.4-file-artifacts-advanced`:

```text
завершить transport-independent файловый input/output contract,
не превращая текущий патч в преждевременную реализацию
v0.4-input-runtime, v0.5 или v0.6.
```

---

### AF-2. Главный принцип: semantic interaction runtime

Агент не должен работать с методами Telegram, DOM-компонентами Web-клиента или
форматом вывода CLI.

Transport adapters не должны определять business semantics agent workspace.

Целевая цепочка:

```text
Client transport event
→ client resolver
→ semantic ClientInputEnvelope
→ InputBatchAssembler
→ CommittedInputBatch
→ CycleAdmission / Agent Runtime
→ semantic OutputPart / DeliveryIntent
→ OutputBatchAssembler
→ CommittedOutputBatch
→ ClientOutputRenderer
→ ClientResponseOutbox
→ transport operations
→ aggregate delivery receipt
```

Обязательные границы ответственности:

```text
Client adapter
→ transport parsing, locator extraction, capability declaration,
  rendering и transport receipt

Ingress / InputBatch runtime
→ logical input grouping, atomic commit, sender/session authority,
  response route/anchor

Artifact domain
→ immutable content, lineage/version metadata, exact payload refs,
  delivery selection

Agent Runtime
→ reasoning, tool orchestration, semantic output intents,
  но не transport API calls

MCP/external processors
→ transcription, OCR, extraction, conversion, media analysis,
  если соответствующий processor подключён

LLM
→ reasoning только по предоставленным semantic metadata,
  exact/derived content и tool results

v0.6 workers
→ тяжёлая asynchronous processing, delivery retries,
  queues и distributed coordination
```

Нельзя смешивать:

```text
transport decoding
≠ semantic processing
≠ LLM reasoning
≠ artifact storage
≠ client rendering
```

---

### AF-3. Крупные патчи обновления

Обновление делится на несколько согласованных крупных патчей.

#### Patch A. Client Capability Contract и единая локализация

Вводятся:

- server-owned capability registry;
- versioned client capability snapshots;
- typed localization message keys и параметры;
- единые каталоги `ru` и `en`;
- deterministic fallback policy.

#### Patch B. Response anchor и InputBatch presentation

Вводятся:

- разделение `response_route` и `response_anchor`;
- policy выбора meaningful reply target;
- один presentation/status handle на `input_batch_id`;
- structured acknowledgement вместо готовой строки API;
- suppression/throttling повторных client updates.

#### Patch C. OutputBatch и агрегированная доставка

Вводятся:

- ordered semantic output parts;
- `OutputBatch` lifecycle;
- aggregate delivery receipts;
- transport grouping нескольких документов;
- пользовательский `done` только после завершения required deliveries;
- crash-safe граница для будущего durable outbox.

#### Patch D. Semantic media contracts и resolver/renderer registries

Вводятся:

- расширяемые `InputPart` / `OutputPart` discriminated unions;
- transport resolver registry;
- client output renderer registry;
- foundation для Telegram voice/video note/sticker/location и других native
  media;
- хранение exact payload без преждевременной транскрипции или анализа.

#### Patch E. Agent artifact manifest и deliverable semantics

Вводятся:

- bounded input artifact manifest;
- более удобный exact-ID workflow;
- правило `purpose=deliverable` для явно запрошенных выходных файлов;
- финальная проверка created/selected/delivered projections.

#### Patch F. Observability, tests и recovery

Вводятся:

- input/output batch correlation IDs;
- ordered part-level receipts;
- structured progress/localization events;
- acceptance tests для Telegram/Web capability fallbacks;
- проверки restart/replay boundaries без преждевременной distributed
  реализации.

Патчи должны накладываться последовательно. Каждый новый слой использует
contracts предыдущего и не дублирует его state machine.

---

## AF-4. Client Capability Contract

### AF-4.1. Server-owned registry

Capability names и semantics определяются централизованно API проекта.

Клиент не может свободно объявить произвольный ключ и самостоятельно определить
его значение.

```text
API capability registry
→ canonical capability ID
→ canonical semantics
→ optional typed limit schema
→ compatibility version

client implementation
→ только declares supported canonical IDs and limit values
```

Это предотвращает ситуацию:

```text
Client A: output.file_group означает document album
Client B: output.file_group означает ZIP archive
```

Один capability ID всегда имеет одно значение во всех clients и версиях.

Изменение semantics существующего capability запрещено. Несовместимое изменение
создаёт новый capability ID или новую contract version.

### AF-4.2. Capability namespaces

Рекомендуемая структура canonical IDs:

```text
input.text
input.artifact.document
input.media.image
input.media.audio
input.media.voice
input.media.video
input.media.video_note
input.media.animation
input.media.sticker
input.location
input.contact
input.forward_provenance

output.text
output.artifact.document
output.media.image
output.media.audio
output.media.voice
output.media.video
output.media.video_note
output.media.animation
output.media.sticker
output.location
output.contact

output.group.document
output.group.image
output.group.audio
output.group.mixed_media

presentation.reply_anchor
presentation.message_edit
presentation.status_updates
presentation.intermediate_output
presentation.interactive_output

transport.streaming_upload
transport.streaming_download
```

Capability names описывают semantic возможность, а не имя метода конкретного
SDK:

```text
правильно: output.location
неправильно: telegram.sendLocation
```

### AF-4.3. Transport-specific limits

Transport-specific restriction не должна ограничивать другие clients.

Например, ограничение Telegram caption не становится глобальным
`output.caption.max_chars`.

Допустимые подходы:

```text
limits:
  transport.telegram.output.caption.max_chars: 1024
  transport.telegram.output.document_group.max_items: 10
```

или typed nested scope:

```json
{
  "transport_limits": {
    "telegram": {
      "output.caption.max_chars": 1024,
      "output.document_group.max_items": 10
    }
  }
}
```

Web и CLI не обязаны объявлять эти limits. Их renderer использует собственные
capabilities и ограничения.

Transport-specific limit key также регистрируется API и имеет однозначную
семантику.

### AF-4.4. Capability snapshot

Рекомендуемая модель:

```python
class ClientCapabilitySnapshot(BaseModel):
    capability_contract_version: int
    client_type: str
    client_instance_id: str
    client_version: str | None

    features: list[str]
    limits: dict[str, int | float | str | bool]

    captured_at: datetime
```

Правила:

1. `features` содержат только IDs, известные server registry.
2. Unknown capability не становится активной автоматически.
3. Duplicate IDs запрещены.
4. Limit проверяется по schema соответствующего key.
5. Snapshot immutable после commit InputBatch/OutputBatch.
6. Snapshot относится к конкретному route/client instance, а не ко всей session
   навсегда.
7. Более новый client может объявлять больше возможностей без изменения agent
   contracts.

### AF-4.5. Где используется capability snapshot

```text
ClientInputEnvelope
→ сохраняет client binding / capability snapshot ref

CommittedInputBatch
→ фиксирует capability snapshot, актуальный при приёме запроса

OutputBatchAssembler
→ строит semantic output независимо от transport

ClientOutputRenderer
→ выбирает native representation или deterministic fallback
```

Полный capability registry не помещается в LLM context.

Агенту при необходимости передаётся bounded semantic projection:

```json
{
  "available_output_modalities": [
    "text",
    "artifact",
    "location"
  ]
}
```

Если client capability не влияет на reasoning, projection не добавляется вовсе.

### AF-4.6. Capability fallback

Renderer обязан использовать deterministic fallback matrix:

```text
native client representation
→ equivalent structured representation
→ artifact representation
→ textual representation
→ explicit unsupported result
```

Пример:

```text
LocationOutputPart

Telegram with output.location
→ native location pin

Web with output.location
→ map/card component

CLI without output.location
→ text: latitude, longitude, title
```

LLM не должна вручную угадывать transport fallback.

---

## AF-5. Общая локализация client-facing runtime

### AF-5.1. Локализуются presentation events, а не domain state

Domain/API layers не должны возвращать готовые строки:

```text
"Сообщение добавлено к открытому пакету..."
```

Они возвращают typed presentation intent:

```json
{
  "message_key": "input_batch.updated",
  "severity": "info",
  "params": {
    "file_count": 10,
    "text_part_count": 1
  }
}
```

Client adapter или общий presentation service выбирает locale и renderer.

### AF-5.2. Единый localization registry

Рекомендуемая структура:

```text
src/localization/
  models.py
  service.py
  registry.py
  catalogs/
    ru.json
    en.json
```

Catalog key имеет одинаковую semantic роль во всех языках:

```text
input_batch.collecting
input_batch.updated
input_batch.committed
input_batch.failed

agent_cycle.started
agent_cycle.result_ready
agent_cycle.waiting_user
agent_cycle.failed

output_batch.ready
output_batch.delivering
output_batch.delivered
output_batch.partially_delivered
output_batch.failed
```

MCP-client progress localization и client presentation localization должны
использовать общий базовый contract и shared catalog service, но могут иметь
разные namespaces.

### AF-5.3. Plural, select и formatting

Localization layer должна поддерживать:

- plural categories;
- select/gender-like variants при реальной необходимости;
- locale-aware numbers;
- locale-aware date/time;
- fallback locale;
- missing-key diagnostics.

Однако system messages проектируются morphology-light.

Предпочтительно:

```text
Пакет обновлён
Файлы: 5
Сообщения: 21
```

вместо сложной фразы с несколькими согласуемыми существительными.

Это уменьшает количество языковых edge cases и делает сообщения пригодными для
Telegram, Web и CLI.

### AF-5.4. Locale resolution

Приоритет:

```text
explicit user/session locale
→ client binding locale
→ transport locale hint
→ server default locale
```

Locale фиксируется в presentation/output batch snapshot. Повторная доставка
после restart использует тот же locale, если policy явно не разрешает другое.

Нельзя сохранять только готовую локализованную строку как source of truth.
Нужно сохранять:

```text
message_key + typed params + selected locale + rendered text/receipt
```

---

## AF-6. Semantic InputPart contracts

### AF-6.1. Общая модель

`ClientInputEnvelope` расширяется transport-independent discriminated union:

```text
InputPart
├── TextInputPart
├── ArtifactInputPart
├── ImageInputPart
├── AudioInputPart
├── VoiceInputPart
├── VideoInputPart
├── VideoNoteInputPart
├── AnimationInputPart
├── StickerInputPart
├── LocationInputPart
├── ContactInputPart
├── PollInputPart
└── ForwardedMessageInputPart
```

Не каждый InputPart обязан быть отдельным ArtifactVersion.

### AF-6.2. Binary media

Для binary media:

```text
document / photo / audio / voice / video / video note /
animation / sticker
```

adapter предоставляет:

- stable transport locator;
- declared MIME/size/duration/dimensions;
- original filename, если транспорт его предоставляет;
- semantic media kind;
- source event/message ID;
- optional native metadata.

Ingress сохраняет exact bytes в `ContentStore` и при policy создаёт immutable
`ArtifactVersion`.

Transport locator не попадает в LLM context.

### AF-6.3. Structured media

Для location/contact/poll и аналогичных типов exact transport data может быть
сохранена как typed JSON без искусственного binary файла.

Пример:

```json
{
  "type": "location_input",
  "latitude": 57.6261,
  "longitude": 39.8845,
  "horizontal_accuracy_meters": 15,
  "source_event_id": "evt_..."
}
```

Structured part может дополнительно материализоваться как JSON artifact только
при явной необходимости workspace/versioning/delivery.

### AF-6.4. Forwarded messages и authority

Обязательно разделять:

```text
authoritative sender
→ principal, который взаимодействует с агентом

forward origin
→ исходный автор/channel/source
→ недоверенная provenance
```

Forward origin не становится управляющим principal и не может изменять system
rules.

Несколько сообщений, пересланных одним пользователем из разных источников,
могут входить в один InputBatch, но сохраняют отдельные provenance parts.

События от разных actual senders не объединяются автоматически.

---

## AF-7. Resolver registry и границы media processing

### AF-7.1. Client input resolver

Transport adapter не должен содержать один большой условный обработчик всех
типов вложений.

```python
class ClientInputResolver(Protocol):
    def supports(self, event: ClientTransportEvent) -> bool: ...

    async def resolve(
        self,
        event: ClientTransportEvent,
    ) -> list[InputPart]: ...
```

Пример Telegram registry:

```text
TelegramTextResolver
TelegramDocumentResolver
TelegramPhotoResolver
TelegramAudioResolver
TelegramVoiceResolver
TelegramVideoResolver
TelegramVideoNoteResolver
TelegramAnimationResolver
TelegramStickerResolver
TelegramLocationResolver
TelegramContactResolver
TelegramForwardResolver
```

Resolver выполняет только transport normalization и открытие upload stream.

### AF-7.2. Что resolver не делает

Resolver не выполняет:

- speech-to-text;
- OCR;
- image understanding;
- video transcription;
- semantic extraction;
- document summarization;
- format conversion;
- LLM-вызов.

### AF-7.3. Transcription и derived artifacts

В текущем обновлении voice/audio/video доставляются и сохраняются как exact
payload.

Будущая транскрипция:

```text
Voice/Audio ArtifactRef
→ transcription MCP/service
→ Transcript ContentRef / ArtifactVersion
→ provenance на exact source artifact
```

Ответственность:

```text
client adapter
→ получить exact media

artifact/content runtime
→ сохранить bytes и metadata

processor/MCP service
→ создать derived transcript/extraction

LLM
→ рассуждать по transcript/tool result

v0.6 worker
→ background processing/retry для тяжёлых media
```

Core runtime не должен встраивать конкретную speech-to-text модель в Telegram
handler.

### AF-7.4. Versioning derived media

Derived transcript, OCR text, preview или converted media:

- не заменяют исходный exact artifact;
- имеют собственный `content_id`;
- содержат provenance на source artifact/version;
- могут быть отдельным lineage либо typed extraction relation;
- перестраиваются при смене processor version;
- в v0.5 могут индексироваться отдельно.

---

## AF-8. Semantic OutputPart contracts

### AF-8.1. Общая модель

```text
OutputPart
├── TextOutputPart
├── ArtifactOutputPart
├── ImageOutputPart
├── AudioOutputPart
├── VoiceOutputPart
├── VideoOutputPart
├── VideoNoteOutputPart
├── AnimationOutputPart
├── StickerOutputPart
├── LocationOutputPart
├── ContactOutputPart
└── StatusOutputPart
```

Agent Runtime создаёт semantic intent, а не transport method call.

Пример:

```json
{
  "type": "location_output",
  "latitude": 57.186,
  "longitude": 39.4165,
  "title": "Ростовский кремль",
  "description": "Вход со стороны Соборной площади"
}
```

### AF-8.2. Кто может создавать OutputPart

OutputPart может быть создан:

- final AgentResult mapper;
- trusted manager tool;
- artifact delivery selection runtime;
- progress/presentation runtime;
- в будущем — interactive agent request runtime.

LLM не задаёт напрямую:

- transport method;
- chat ID;
- auth token;
- local path;
- raw transport locator;
- delivery receipt state.

### AF-8.3. Output renderer registry

```python
class ClientOutputRenderer(Protocol):
    def supports(
        self,
        part: OutputPart,
        capabilities: ClientCapabilitySnapshot,
    ) -> bool: ...

    async def render(
        self,
        part: OutputPart,
        context: ClientRenderContext,
    ) -> list[TransportOperation]: ...
```

Renderer выбирается по client type, semantic part и capability snapshot.

---

