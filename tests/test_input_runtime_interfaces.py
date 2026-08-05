from dataclasses import FrozenInstanceError
import ast
from pathlib import Path
import pytest
from src.input_runtime import InputRuntimeRepositoryBundle, SessionInputRuntimeRepository


class FakeSessionRepository:
    async def get_state(self, session_id): return None
    async def create_state_if_absent(self, state): return state
    async def compare_and_swap_state(self, state, *, expected_revision): return state
    async def list_recoverable_states(self): return []


def test_runtime_checkable_protocol():
    assert isinstance(FakeSessionRepository(), SessionInputRuntimeRepository)


def test_bundle_accepts_structural_implementations_and_is_frozen():
    fake = object()
    bundle = InputRuntimeRepositoryBundle(fake, fake, fake, fake, fake, fake, fake, fake)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        bundle.sessions = fake


def test_package_import_and_core_modules_have_no_forbidden_imports():
    import src.input_runtime
    root = Path(src.input_runtime.__file__).parent
    forbidden = (
        "telegram", "fastapi", "src.servers", "src.gateway",
        "src.api.api", "src.mcp.mcp_client", "pathlib", "os",
    )
    for filename in ("models.py", "interfaces.py", "errors.py"):
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        assert not any(
            any(item == token or item.startswith(token + ".") for token in forbidden)
            for item in imports
        )
