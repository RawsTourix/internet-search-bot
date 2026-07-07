from openai import OpenAI
import os

client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key="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"
)

tools = [
    {
        "type": "function",
        "function": {
            "name": "test_tool",
            "description": "Тестовый инструмент. Используй его, когда пользователь просит вызвать test_tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Тестовый запрос"
                    }
                },
                "required": ["query"]
            }
        }
    }
]

response = client.chat.completions.create(
    model="moonshotai/kimi-k2-instruct-0905",
    messages=[
        {
            "role": "system",
            "content": (
                "Ты ИИ-агент. Если пользователь просит вызвать инструмент, "
                "обязательно используй tool_call, а не отвечай текстом."
            )
        },
        {
            "role": "user",
            "content": "Вызови test_tool с query='hello'."
        }
    ],
    tools=tools,
    tool_choice="auto",
    max_tokens=1000,
    temperature=0.2
)

message = response.choices[0].message

print(message)
print("content:", message.content)
print("tool_calls:", message.tool_calls)
print("reasoning_content:", getattr(message, "reasoning_content", None))