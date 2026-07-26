"""Run the canonical Telegram webhook and READY-outbox composition."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.getenv("TELEGRAM_SERVER_HOST", "0.0.0.0").strip() or "0.0.0.0"
    try:
        port = int(os.getenv("TELEGRAM_SERVER_PORT", "8001"))
    except ValueError as error:
        raise ValueError("TELEGRAM_SERVER_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError("TELEGRAM_SERVER_PORT must be between 1 and 65535")
    uvicorn.run(
        "src.servers.telegram.app:app",
        host=host,
        port=port,
    )


if __name__ == "__main__":
    main()
