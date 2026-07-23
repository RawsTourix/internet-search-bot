"""Manual OpenAI-compatible LLM connectivity probe.

This script is intentionally kept outside ``tests/`` so unittest discovery never
performs a real network request. Run it explicitly after configuring the
required environment variables.
"""

from __future__ import annotations

import os
import sys

from openai import OpenAI


DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "qwen/qwen3-coder-480b-a35b-instruct"


def main() -> int:
    api_key = os.getenv("LLM_API_KEY") or os.getenv("NVIDIA_API_KEY")
    if not api_key:
        print(
            "Set LLM_API_KEY or NVIDIA_API_KEY before running this manual probe.",
            file=sys.stderr,
        )
        return 2

    base_url = os.getenv("LLM_API_URL", DEFAULT_BASE_URL).strip()
    model = os.getenv("LLM_MODEL", DEFAULT_MODEL).strip()
    if not base_url or not model:
        print("LLM_API_URL and LLM_MODEL must not be empty.", file=sys.stderr)
        return 2

    client = OpenAI(base_url=base_url, api_key=api_key)
    print(f"Sending a test request to {base_url} using {model}...\n")

    try:
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": "Привет. Это тестовый запрос к API.",
                }
            ],
            temperature=0.7,
            top_p=0.8,
            max_tokens=4096,
            stream=True,
        )
        for chunk in completion:
            if chunk.choices and chunk.choices[0].delta.content is not None:
                print(chunk.choices[0].delta.content, end="", flush=True)
        print()
        return 0
    except Exception as error:
        print(f"LLM probe failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
