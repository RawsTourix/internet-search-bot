import tempfile
import unittest
from pathlib import Path

from src.artifacts import ArtifactTraceService, FileSystemArtifactTraceStore
from src.storage import StorageConfigType


class ArtifactTraceValueRedactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_credentials_urls_and_absolute_paths_are_redacted_in_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = FileSystemArtifactTraceStore(
                StorageConfigType(
                    root_dir=str(Path(temporary) / "storage")
                )
            )
            service = ArtifactTraceService(store)
            event = await service.record(
                session_id="telegram:conversation:redaction",
                event_type="artifact_ingress_failed",
                stage="ingress",
                status="failed",
                error={
                    "error_type": "TransportError",
                    "message": (
                        "Bearer very-secret-token "
                        "url=https://example.test/download?token=hidden "
                        "windows=C:\\Users\\tester\\private\\file.txt "
                        "unix=/var/private/artifacts/file.txt "
                        "api_key=hidden-key"
                    ),
                },
                data={
                    "presentation_token": "must-be-removed",
                    "safe": "visible",
                },
            )

            self.assertIsNotNone(event)
            self.assertEqual(event.data, {"safe": "visible"})
            self.assertIn("Bearer [REDACTED]", event.error.message)
            self.assertIn("[REDACTED_URL]", event.error.message)
            self.assertIn("[REDACTED_PATH]", event.error.message)
            self.assertIn("api_key=[REDACTED]", event.error.message)

            session_dir = store._session_dir(
                "telegram:conversation:redaction"
            )
            raw = "\n".join(
                path.read_text(encoding="utf-8")
                for path in session_dir.glob("*.jsonl")
            )
            for secret in (
                "very-secret-token",
                "must-be-removed",
                "hidden-key",
                "C:\\Users\\tester",
                "/var/private/artifacts",
                "https://example.test",
            ):
                self.assertNotIn(secret, raw)


if __name__ == "__main__":
    unittest.main()
