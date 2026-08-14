from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_production_src_uses_only_absolute_imports():
    offenders: list[str] = []
    for path in sorted(Path("src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                offenders.append(
                    f"{path.as_posix()}:{node.lineno}: level={node.level} module={node.module!r}"
                )
    assert offenders == [], "relative imports under src:\n" + "\n".join(offenders)


def _smoke_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        AGENT_CONFIG_PATH="src/api/mcp.config.example",
        TELEGRAM_API_KEY="ir10-import-telegram",
        WEB_API_KEY="ir10-import-web",
        INTERNAL_API_KEY="ir10-import-internal",
        WEBHOOK_SECRET="ir10-import-webhook",
        TELEGRAM_FILE_PROVIDER_URL="",
        TELEGRAM_FILE_PROVIDER_TOKEN="",
        HTTP_PROXY="",
        HTTPS_PROXY="",
        ALL_PROXY="",
        NO_PROXY="127.0.0.1,localhost",
    )
    return env


@pytest.mark.parametrize(
    ("name", "script"),
    [
        (
            "message-processor-first",
            "from src.core.message_processor import MessageProcessor; "
            "from src.api import ensure_input_runtime_projection_compatibility; "
            "assert MessageProcessor; assert ensure_input_runtime_projection_compatibility()",
        ),
        (
            "api-first",
            "from src.api.api import API; "
            "from src.core.message_processor import MessageProcessor; "
            "assert API and MessageProcessor; "
            "assert getattr(API, '_ir9_projection_compatibility_installed', False)",
        ),
        (
            "gateway-import",
            "import src.gateway; "
            "from src.api import ensure_input_runtime_projection_compatibility; "
            "assert src.gateway.app is not None; "
            "assert ensure_input_runtime_projection_compatibility()",
        ),
        (
            "uvicorn-importer",
            "from uvicorn.importer import import_from_string; "
            "app = import_from_string('src.gateway:app'); "
            "from src.api import ensure_input_runtime_projection_compatibility; "
            "assert app is not None; assert ensure_input_runtime_projection_compatibility()",
        ),
    ],
)
def test_fresh_process_production_import_graph(name: str, script: str):
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        env=_smoke_env(),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, (
        f"{name} failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
