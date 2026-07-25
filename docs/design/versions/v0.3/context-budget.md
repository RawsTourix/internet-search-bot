---
id: design.v0.3.context-budget
version: v0.3
spec_status: accepted
implementation_status: implemented
---
# Часть VII. Context budget

## 53. Настройки LLM context budget

В `mcp.config`:

```json
{
  "llm": {
    "context_window_tokens": 262144,
    "max_tokens": 4096,
    "reserved_output_tokens": 8192,
    "context_safety_ratio": 0.75,
    "context_compaction_target_ratio": 0.55,
    "enable_context_compaction": true
  }
}
```

### `context_window_tokens`

Полное контекстное окно модели.

### `max_tokens`

Максимальное количество токенов, которое API разрешает модели сгенерировать.

### `reserved_output_tokens`

Запас под будущий ответ модели.

Если `reserved_output_tokens` не задан, можно использовать `max_tokens`.

Рекомендуемая логика:

```python
effective_reserved_output_tokens = max(
    reserved_output_tokens or 0,
    max_tokens,
)
```

### `context_safety_ratio`

Порог, когда нужно начинать беспокоиться о переполнении контекста.

### `context_compaction_target_ratio`

Цель, до которой желательно ужать visible context после compaction.

Важно:

```text
target ratio не означает “удалить ровно X токенов”.
Он означает “попытаться привести контекст к целевому бюджету,
сжимая смысловые блоки”.
```

---

