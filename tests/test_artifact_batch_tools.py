import asyncio
import tempfile
import unittest
from pathlib import Path

from src.artifacts import (
    ArtifactConfigType,
    ArtifactStorageError,
    create_artifact_services,
)
from src.artifacts.tools import ArtifactToolController
from src.mcp.manager_context import ManagerToolContext
from src.mcp.mcp_client import SessionState
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType, create_storage_services


class ArtifactBatchToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        storage_config = StorageConfigType(root_dir=str(root / "storage"))
        storage = create_storage_services(storage_config)
        self.services = create_artifact_services(
            storage_config=storage_config,
            artifact_config=ArtifactConfigType(
                max_artifacts_per_cycle=16,
                max_concurrent_artifact_reads=2,
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
            ),
            content_store=storage.content_store,
        )
        self.controller = ArtifactToolController(
            self.services.artifact_service,
            self.services.delivery_service,
        )
        self.cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="Read the files",
            messages_for_llm=[],
            cycle_trace=[],
            original_user_message_index=0,
        )
        self.context = ManagerToolContext(
            session_id="session-1",
            cycle_id="cycle-1",
            active_cycle=self.cycle,
            session_state=SessionState(),
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def _create(self, index: int):
        outcome = await self.controller.execute(
            "artifact_create_text",
            {
                "filename": f"file-{index}.md",
                "text": f"alpha {index} omega",
                "format_id": "markdown",
            },
            self.context,
        )
        return outcome.payload["artifact"]

    async def test_ten_files_keep_order_and_item_boundaries(self):
        artifacts = [await self._create(index) for index in range(10)]
        requested = [item["artifact_id"] for item in reversed(artifacts)]

        outcome = await self.controller.execute(
            "artifact_read_text",
            {"artifact_ids": requested},
            self.context,
        )

        self.assertEqual(outcome.payload["status"], "ok")
        self.assertEqual(outcome.payload["requested_count"], 10)
        self.assertEqual(outcome.payload["successful_count"], 10)
        self.assertEqual(
            [
                item["requested_artifact_id"]
                for item in outcome.payload["items"]
            ],
            requested,
        )
        self.assertEqual(
            [item["request_index"] for item in outcome.payload["items"]],
            list(range(10)),
        )
        self.assertTrue(all(
            item["representation"] == "inline"
            and item["exact_content_available"]
            and item["complete"]
            and not item["needs_retrieval"]
            for item in outcome.payload["items"]
        ))

    async def test_partial_read_preserves_valid_results(self):
        first = await self._create(1)
        second = await self._create(2)
        invalid = "not-an-artifact-id"

        outcome = await self.controller.execute(
            "artifact_read_text",
            {
                "artifact_ids": [
                    first["artifact_id"],
                    invalid,
                    second["artifact_id"],
                ]
            },
            self.context,
        )

        self.assertEqual(outcome.payload["status"], "partial")
        self.assertEqual(outcome.payload["successful_count"], 2)
        self.assertEqual(outcome.payload["failed_count"], 1)
        self.assertEqual(outcome.payload["items"][0]["status"], "ok")
        self.assertEqual(
            outcome.payload["items"][1]["code"],
            "invalid_artifact_id",
        )
        self.assertIn(
            "artifact_list",
            outcome.payload["items"][1]["suggested_action"],
        )
        self.assertEqual(outcome.payload["items"][2]["status"], "ok")

    async def test_duplicate_read_executes_basic_operation_once(self):
        artifact = await self._create(1)
        original = self.services.artifact_service.read_text
        calls = 0

        async def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return await original(*args, **kwargs)

        self.services.artifact_service.read_text = counted
        outcome = await self.controller.execute(
            "artifact_read_text",
            {
                "artifact_ids": [
                    artifact["artifact_id"],
                    artifact["artifact_id"],
                ]
            },
            self.context,
        )

        self.assertEqual(calls, 1)
        self.assertEqual(outcome.payload["requested_count"], 2)
        self.assertEqual(len(outcome.payload["items"]), 2)

    async def test_read_concurrency_is_bounded(self):
        artifacts = [await self._create(index) for index in range(6)]
        original = self.services.artifact_service.read_text
        active = 0
        observed_maximum = 0
        guard = asyncio.Lock()

        async def delayed(*args, **kwargs):
            nonlocal active, observed_maximum
            async with guard:
                active += 1
                observed_maximum = max(observed_maximum, active)
            try:
                await asyncio.sleep(0.01)
                return await original(*args, **kwargs)
            finally:
                async with guard:
                    active -= 1

        self.services.artifact_service.read_text = delayed
        outcome = await self.controller.execute(
            "artifact_read_text",
            {"artifact_ids": [item["artifact_id"] for item in artifacts]},
            self.context,
        )

        self.assertEqual(outcome.payload["status"], "ok")
        self.assertLessEqual(
            observed_maximum,
            self.services.config.max_concurrent_artifact_reads,
        )
        self.assertGreater(observed_maximum, 1)

    async def test_storage_error_fails_the_whole_manager_call(self):
        first = await self._create(1)
        second = await self._create(2)
        original = self.services.artifact_service.read_text

        async def failing(artifact_id, **kwargs):
            if artifact_id == second["artifact_id"]:
                raise ArtifactStorageError("storage unavailable")
            return await original(artifact_id, **kwargs)

        self.services.artifact_service.read_text = failing
        with self.assertRaises(ArtifactStorageError):
            await self.controller.execute(
                "artifact_read_text",
                {
                    "artifact_ids": [
                        first["artifact_id"],
                        second["artifact_id"],
                    ]
                },
                self.context,
            )

    async def test_process_hard_limit_returns_bounded_stored_only_items(self):
        artifact = await self._create(1)
        self.services.config.max_composite_result_bytes = 256

        outcome = await self.controller.execute(
            "artifact_read_text",
            {"artifact_ids": [artifact["artifact_id"]]},
            self.context,
        )

        item = outcome.payload["items"][0]
        self.assertEqual(outcome.payload["status"], "ok")
        self.assertEqual(item["representation"], "stored_only")
        self.assertEqual(item["text"], "")
        self.assertFalse(item["exact_content_available"])
        self.assertFalse(item["complete"])
        self.assertTrue(item["needs_retrieval"])
        exact = await self.services.artifact_service.read_text(
            artifact["artifact_id"],
            access=self.controller._access(self.context),
        )
        self.assertEqual(exact.text, "alpha 1 omega")

    async def test_search_uses_order_partial_and_dedup_semantics(self):
        first = await self._create(1)
        second = await self._create(2)
        original = self.services.artifact_service.search_text
        calls: list[str] = []

        async def counted(artifact_id, **kwargs):
            calls.append(artifact_id)
            return await original(artifact_id, **kwargs)

        self.services.artifact_service.search_text = counted
        requested = [
            second["artifact_id"],
            "invalid",
            first["artifact_id"],
            second["artifact_id"],
        ]
        outcome = await self.controller.execute(
            "artifact_search_text",
            {"artifact_ids": requested, "query": "alpha"},
            self.context,
        )

        self.assertEqual(outcome.payload["status"], "partial")
        self.assertEqual(
            [
                item["requested_artifact_id"]
                for item in outcome.payload["items"]
            ],
            requested,
        )
        self.assertEqual(calls.count(second["artifact_id"]), 1)
        self.assertEqual(calls.count(first["artifact_id"]), 1)
        self.assertEqual(
            outcome.payload["items"][1]["code"],
            "invalid_artifact_id",
        )


if __name__ == "__main__":
    unittest.main()
