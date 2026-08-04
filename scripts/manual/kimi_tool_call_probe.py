"""Manual NVIDIA/Kimi tool-call probe; never imported by the test suite."""

from __future__ import annotations

import os


def main() -> int:
    try:
        from openai import OpenAI
    except ImportError as error:
        raise SystemExit(
            "Install the optional 'openai' package to run this probe."
        ) from error

    api_key = os.getenv("NVIDIA_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("NVIDIA_API_KEY is required.")

    client = OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=api_key,
    )
    tools = [{
        "type": "function",
        "function": {
            "name": "test_tool",
            "description": "Call this test tool when the user requests it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Test query",
                    }
                },
                "required": ["query"],
            },
        },
    }]
    response = client.chat.completions.create(
        model="moonshotai/kimi-k2-instruct-0905",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an agent. Use a tool call when the user asks "
                    "you to invoke a tool."
                ),
            },
            {
                "role": "user",
                "content": "Call test_tool with query='hello'.",
            },
        ],
        tools=tools,
        tool_choice="auto",
        max_tokens=1000,
        temperature=0.2,
    )
    message = response.choices[0].message
    print(message)
    print("content:", message.content)
    print("tool_calls:", message.tool_calls)
    print("reasoning_content:", getattr(message, "reasoning_content", None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
