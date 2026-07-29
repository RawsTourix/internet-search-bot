---
id: design.v0.4.batch-workflows.input-assembly
version: v0.4
spec_status: accepted
implementation_status: planned
last_reviewed: 2026-07-29
---

# BW-1–BW-4 — InputBatch assembly

## BW-1. Domain model

### BW-1.1. Assembly mode

`InputBatchDraft` получает server-owned режим сборки:

```python
class InputAssemblyMode(str, Enum):
    AUTO = "auto"
    EXPLICIT = "explicit"


class InputCommitPolicy(str, Enum):
    AUTOMATIC = "automatic"
    EXPLICIT = "explicit"
```

Связь:

```text
AUTO
→ обычный transport workflow
→ text-only без open draft commit-ится немедленно
→ attachment-first draft ждёт текст либо explicit /send

EXPLICIT
→ пользователь заранее включил collection mode
→ никакой quiet timeout не коммитит пакет
→ commit только по /send, кнопке Send или эквивалентному client action
```

Поля являются authoritative domain metadata. Telegram/Web/CLI могут запрашивать
режим, но не создают собственные несогласованные state machines.

### BW-1.2. Exact collection scope

Ключ активного пользовательского draft:

```text
client_instance_id
+ conversation_id
+ optional thread_id
+ principal_id according to SessionKeyPolicy
```

Recommended scope:

```text
Telegram private chat → bot instance + conversation
Telegram group        → bot instance + conversation + sender
Telegram topic        → bot instance + conversation + thread + sender
Web                    → authenticated workspace/session + principal
CLI                    → explicit local session
```

На одном exact scope допускается:

```text
не более одного active explicit draft
не более одного compatible AUTO attachment draft
```

Два independent active drafts не выбираются по времени, filename или «последнему
сообщению». Conflict возвращает explicit user-facing resolution.

### BW-1.3. Draft provenance

Каждая часть сохраняет:

```text
source event ID
source message/update ID
occurred_at
stable part order
text kind: message_text | caption
attachment slot/file provenance
assembly mode at admission
```

Несколько сообщений не склеиваются в одну строку. `CommittedInputBatch`
сохраняет упорядоченный список `text_parts` и `artifact_refs`.

## BW-2. AUTO policy

### BW-2.1. Матрица входа

| Текущее состояние | Новое событие | Результат |
|---|---|---|
| Нет draft | Обычный text | Немедленный atomic batch и agent admission |
| Нет draft | File/media group без caption | Открыть AUTO attachment draft; agent не запускать |
| Нет draft | File/media group с caption | Открыть draft, сохранить caption, дождаться transport completion и logical quiet period |
| AUTO attachment draft | Новый file/album того же scope | Добавить в тот же draft, сбросить logical quiet timer |
| AUTO attachment draft | Text | Добавить отдельный text part, сбросить logical quiet timer |
| AUTO attachment draft с text | Новый text/file до seal | Вернуть в collecting, сохранить порядок, сбросить timer |
| Нет draft | Text, затем позднее files | Text уже отдельный committed batch; files открывают новый draft |
| Любое | Command/control action | Не добавлять как user text; передать в control plane |

AUTO не угадывает обратную связь:

```text
instruction first
→ immediate text batch

files later
→ another logical input
```

Для обратного порядка пользователь заранее включает EXPLICIT mode.

### BW-2.2. Transport quiet и logical quiet — разные таймеры

```text
media_group_quiet_timeout
→ определяет, что Telegram перестал присылать members одного album

logical_input_quiet_timeout
→ определяет, что пользователь закончил добавлять части уже открытого draft
```

Transport quiet не является достаточной причиной commit files-only draft.

AUTO attachment draft коммитится автоматически только когда:

```text
есть хотя бы один stored attachment
+ есть хотя бы один text part/caption
+ нет in-flight uploads
+ transport groups завершены
+ logical quiet timeout истёк
```

Files-only AUTO draft:

```text
→ остаётся collecting/awaiting_user
→ status предлагает отправить задачу или /send
→ maximum deadline завершает его как ABANDONED, а не запускает files-only cycle
```

Пользователь, намеренно желающий обработать только файлы, использует `/send` или
кнопку Send. Это явный commit intent и не требует текстовой инструкции.

### BW-2.3. Несколько инструкций

Сценарий:

```text
files
→ text 1
→ text 2
→ file
→ text 3
→ quiet
→ one committed InputBatch
```

Каждая новая часть до seal:

- обновляет `last_event_at`;
- сохраняет authoritative order;
- сбрасывает logical quiet deadline;
- может инициировать presentation relocation;
- не продлевает `maximum_deadline` бесконечно.

### BW-2.4. Captions

Caption является `text_part(kind="caption")`, связанным с attachment slot.
Caption может быть единственным текстом, достаточным для AUTO commit после
завершения uploads и logical quiet period.

## BW-3. EXPLICIT collection mode

### BW-3.1. Основные actions

Канонические пользовательские команды:

```text
/collect → начать/продолжить явную сборку
/send    → commit непустого draft
/cancel  → cancel active draft
```

Поддерживаемые aliases:

```text
/batch → /collect
/done  → /send
```

В публичном `/help` и command menu показываются только основные команды. Aliases
сохраняются для естественного UX и backward compatibility.

### BW-3.2. `/collect`

Если active draft отсутствует:

```text
→ создать пустой EXPLICIT draft
→ создать collection presentation
```

Если существует compatible AUTO attachment draft:

```text
→ атомарно повысить его до EXPLICIT
→ сохранить все части и deadlines/audit
→ отключить automatic commit policy
```

Если EXPLICIT draft уже существует:

```text
→ idempotent success
→ показать актуальный состав
→ не создавать второй draft
```

Если существует несовместимый/conflicting draft:

```text
→ explicit conflict
→ предложить /send или /cancel
→ ничего не объединять эвристически
```

### BW-3.3. Collection contents

После `/collect` в один draft входят любые обычные user events exact scope:

- text-only сообщения;
- files по одному;
- несколько media groups;
- captions;
- forwarded messages/provenance;
- поддерживаемые semantic client parts.

Control commands не становятся `text_parts`.

### BW-3.4. `/send`

`/send` коммитит любой непустой active draft:

```text
text-only
files-only
mixed
```

Отдельная «инструкция» не обязательна: explicit send является достаточным
пользовательским намерением начать обработку собранного logical input.

Если draft пуст:

```text
→ не commit
→ "Пакет пока пуст. Отправьте сообщение или файл либо используйте /cancel."
```

Если uploads ещё выполняются:

```text
→ persist commit_requested_at
→ дождаться terminal attachment states
→ commit при успехе
→ FAILED при terminal upload error
```

Повторный `/send` идемпотентен по draft ID и control action idempotency key.

### BW-3.5. `/cancel`

```text
active open draft
→ CANCELLED
→ agent cycle не запускается
→ active group/control indexes освобождаются
→ presentation получает terminal cancelled state
→ accepted bytes/metadata остаются audit evidence и очищаются по общей retention policy
```

Если draft отсутствует, команда отвечает idempotent `Нет собираемого пакета`.

### BW-3.6. Другие команды

Во время collection:

```text
/status → показать состав draft и runtime status
/help   → показать помощь, draft не изменять
/reset  → cancel open draft(s), clear dialog context, workspace не удалять
/start  → обычная справочная команда, draft не изменять
```

Неизвестная slash-команда не добавляется молча как user text.

## BW-4. State machine, recovery and ownership

### BW-4.1. State projection

Существующая durable state machine сохраняется:

```text
COLLECTING
→ SEALING
→ INGESTING
→ READY_TO_COMMIT
→ COMMITTED

COLLECTING/SEALING/INGESTING/READY_TO_COMMIT
→ FAILED | CANCELLED | ABANDONED
```

Assembly/commit policy являются отдельными полями и не кодируются дополнительным
набором несовместимых states.

Для UX runtime может вычислять projection:

```text
collecting_files
awaiting_user
collecting_instructions
commit_requested
processing_uploads
```

Projection не является source of truth persistence.

### BW-4.2. Recovery после process restart

AUTO drafts:

- fully stored + text + expired logical quiet → commit without duplicate agent run;
- files-only → `ABANDONED(process_restart_without_commit_intent)`;
- in-flight/unknown attachment → `ABANDONED` или `FAILED` по exact evidence.

EXPLICIT drafts:

- fully durable collecting draft может быть восстановлен как active explicit
  collection;
- presentation создаётся/перепривязывается заново;
- expired explicit idle deadline → `ABANDONED(explicit_collection_timeout)`;
- `commit_requested_at` восстанавливается и завершается идемпотентно;
- unknown in-flight attachment не считается stored.

### BW-4.3. Commit/admission boundary

```text
InputDraftControlService.commit()
→ immutable CommittedInputBatch
→ existing CycleAdmissionService
```

До `v0.4-input-runtime`:

```text
idle session  → start cycle
active session → existing session_busy behavior
```

После `v0.4-input-runtime` тот же committed batch передаётся в `CycleInbox` без
изменения draft-control contract.

### BW-4.4. Service boundary

```python
class InputDraftControlService(Protocol):
    async def start_collection(scope, *, idempotency_key) -> DraftControlResult: ...
    async def inspect(scope) -> DraftControlResult: ...
    async def commit(scope, *, idempotency_key) -> DraftControlResult: ...
    async def cancel(scope, *, idempotency_key) -> DraftControlResult: ...
```

Telegram commands, Web buttons и CLI actions вызывают этот service. Они не
мутируют filesystem draft напрямую.
