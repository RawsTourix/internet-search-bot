import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

from src.mcp.mcp_client import LLMConfigType, MCPClient, load_config
from src.storage import StorageConfigType
from src.storage.errors import (
    StorageValidationError,
    UnsupportedStorageBackendError,
)
from src.storage.factory import StorageServices, create_storage_services


class StorageConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _write_config(self, storage_marker=...):
        payload = {
            "servers": [
                {
                    "name": "disabled-is-not-enough",
                    "connect_type": "executable",
                    "executable": "python",
                    "enabled": True,
                }
            ],
            "llm": {"api_url": "https://example.invalid/v1/chat/completions"},
        }
        if storage_marker is not ...:
            payload["storage"] = storage_marker
        path = self.root / f"config-{len(list(self.root.glob('config-*')))}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_config_without_storage_uses_defaults(self):
        _, _, storage = load_config(str(self._write_config()))

        self.assertEqual(storage, StorageConfigType())

    def test_full_storage_config_is_loaded(self):
        _, _, storage = load_config(
            str(
                self._write_config(
                    {
                        "backend": "filesystem",
                        "root_dir": str(self.root / "objects"),
                        "atomic_writes": False,
                        "verify_content_hash": False,
                        "max_in_memory_content_bytes": 1234,
                    }
                )
            )
        )

        self.assertFalse(storage.atomic_writes)
        self.assertFalse(storage.verify_content_hash)
        self.assertEqual(storage.max_in_memory_content_bytes, 1234)

    def test_invalid_storage_config_is_managed(self):
        for data in (
            {"root_dir": ""},
            {"max_in_memory_content_bytes": -1},
            {"backend": "s3"},
            {"extra": True},
        ):
            with self.subTest(data=data):
                with self.assertRaises(StorageValidationError):
                    load_config(str(self._write_config(data)))

    def test_direct_model_validation_rejects_invalid_values(self):
        with self.assertRaises(ValidationError):
            StorageConfigType(root_dir=" ")
        with self.assertRaises(ValidationError):
            StorageConfigType(max_in_memory_content_bytes=0)

    def test_factory_reserves_layout_and_rejects_unknown_backend(self):
        services = create_storage_services(
            StorageConfigType(root_dir=str(self.root / "storage"))
        )
        self.assertIsNotNone(services.content_store)
        self.assertIsNotNone(services.artifact_store)
        expected = {"contents", "artifacts", "cycles", "plans", "input_batches", "indexes"}
        self.assertEqual(
            {path.name for path in (self.root / "storage").iterdir()},
            expected,
        )

        unsupported = StorageConfigType.model_construct(backend="s3")
        with self.assertRaises(UnsupportedStorageBackendError):
            create_storage_services(unsupported)

    async def test_mcp_client_receives_injected_stores(self):
        fake_content_store = SimpleNamespace(name="content")
        fake_artifact_store = SimpleNamespace(name="artifact")
        services = StorageServices(
            content_store=fake_content_store,
            artifact_store=fake_artifact_store,
        )
        client = MCPClient(
            LLMConfigType(api_url="https://example.invalid"),
            storage_services=services,
        )
        try:
            self.assertIs(client.content_store, fake_content_store)
            self.assertIs(client.artifact_store, fake_artifact_store)
            self.assertIs(client.storage_services, services)
        finally:
            await client.http_client.aclose()
