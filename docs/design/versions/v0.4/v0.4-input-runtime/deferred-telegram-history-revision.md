---
id: design.v0.4.input-runtime.telegram-history-revision
version: v0.4
update: v0.4-input-runtime
spec_status: provisional
implementation_status: deferred
last_reviewed: 2026-08-05
---

# Deferred: Telegram history revision по edited message

## Статус

Функция зафиксирована как потенциально полезное Telegram-specific обновление, но
не входит в initial implementation `v0.4-input-runtime` и не является release
gate текущего PR.

Перед реализацией требуется отдельное подтверждение необходимости, Telegram API
constraints, UX и объёма live acceptance.

## Пользовательский сценарий

Telegram user редактирует старое сообщение, которое входило в ранее committed
логический запрос.

Ожидаемая семантика аналогична созданию новой ветки в first-party LLM chat:

```text
edited source message
→ определить logical message/InputBatch
→ создать новую immutable message/InputBatch revision
→ сохранить весь InputBatch как одну границу запроса
→ сделать более позднюю conversation branch неактивной
→ запустить обработку от обновлённого InputBatch
→ best-effort убрать поздние bot/user-visible messages из Telegram, где возможно
```

## Граница InputBatch

Rewind выполняется не от отдельного transport message, а от целого
`CommittedInputBatch`.

Если batch состоял из десяти сообщений и изменено первое:

- остальные девять частей того же logical request сохраняются;
- изменяется exact part revision;
- весь revised batch становится новой active request boundary;
- неактивной становится история после результата этого batch, а не оставшаяся
  часть самого batch.

Это предотвращает разрушение multipart/forwarded/explicit collection semantics.

## Domain requirements, которые нужно не блокировать сейчас

Current input-runtime design сохраняет возможность будущей функции:

- internal message/part IDs независимы от Telegram message IDs;
- source event/message provenance сохраняется в InputBatch;
- committed batches immutable;
- correction создаёт новую revision/record;
- context revisions имеют parent identity;
- outputs/emissions связаны с cycle/context revision;
- client bindings отделены от domain IDs;
- inactive branch может сохраняться в backend, не входя в active context.

## Потенциальные future entities

Provisional, не являются текущей schema:

```text
LogicalMessage
MessageRevision
ConversationBranch
BranchHead
InputBatchRevision
ConversationRewindRequest
ClientMessageBinding
ClientCleanupOperation
```

## Telegram-specific concerns

Нужно отдельно проверить и протестировать:

- `edited_message`/equivalent update delivery и exact identity;
- private/group/supergroup/topic behavior;
- bot permissions и privacy mode;
- message edit/delete time limits;
- может ли bot убрать собственные поздние messages;
- невозможность надёжно убрать все user messages;
- partial cleanup и divergence между backend active branch и visible Telegram;
- race edited message vs running/finalizing cycle;
- edited message внутри open/committed InputBatch;
- multiple edits/replays одного Telegram message;
- migration после PostgreSQL и multi-client identity v0.8.

## Safety policy

Backend branch switch является authoritative. Telegram cleanup только best-effort
client projection.

Failure удалить/скрыть поздние Telegram messages не должна:

- откатывать branch revision;
- повторять AgentCycle;
- удалять immutable backend evidence;
- считать старую branch активной;
- выполнять blind repeated deletions без receipt/state.

В group chat автоматический rewind особенно рискован. Возможна policy:

```text
private chats: opt-in supported
managed groups/topics: explicit permission + confirmation
other groups: correction-only, no automatic rewind
```

Точная policy откладывается.

## First-party clients

Web/Desktop/Mobile смогут реализовать явное действие:

```text
Edit message and create branch
```

Для них backend branch identity и canonical message IDs являются основным
контрактом; физическое удаление старой branch не требуется.

Telegram feature должна адаптироваться к той же domain branch model, а не создать
отдельную несовместимую историю только для бота.

## Решение для текущего update

- не обрабатывать Telegram edited message как destructive rewind;
- не добавлять client deletion orchestration;
- не вводить полноценную branch model;
- сохранить provenance/IDs/context revision relations;
- вернуться к функции отдельным design/implementation patch после стабилизации
  input runtime и оценки UX/API.
