from __future__ import annotations

import ast
from pathlib import Path


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
