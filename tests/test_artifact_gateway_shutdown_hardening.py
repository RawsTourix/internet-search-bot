import asyncio
import json
import unittest

import httpx

from src.api.legacy_delivery_guard import LegacyTelegramDeliveryGuardMiddleware
from src.servers.telegram.scoped_artifact_bridge import (
    InstanceScopedTelegramArtifactGatewayClient,
)


class TelegramStagedCommitRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_commit_finishes_before_agent_run_and_payload_is_merged(self):
        requests: list[tuple[str, dict]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            requests.append((request.url.path, payload))
            if request.url.path.endswith("/commit"):
                return httpx.Response(
                    201,
                    json={
                        "status": "committed",
                        "input_batch_id": "ibat_" + "1" * 32,
                        "duplicate": False,
                        "committed_batch": {
                            "input_batch_id": "ibat_" + "1" * 32,
                        },
                    },
                )
            if request.url.path.endswith("/run"):
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "response": "agent result",
                        "metadata": {"output_batch_id": "obat_test"},
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        client = InstanceScopedTelegramArtifactGatewayClient(
            gateway_url="http://gateway.test",
            api_key="key",
            client_instance_id="default",
            transport=httpx.MockTransport(handler),
        )
        input_batch_id = "ibat_" + "1" * 32

        result = await client.commit_and_run(
            input_batch_id,
            session_id="telegram:conversation:100",
            progress_locale="ru",
        )

        self.assertEqual(
            [path for path, _ in requests],
            [
                f"/input-batches/{input_batch_id}/commit",
                f"/input-batches/{input_batch_id}/run",
            ],
        )
        self.assertFalse(requests[0][1]["run"])
        self.assertNotIn("run", requests[1][1])
        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["response"], "agent result")
        self.assertEqual(result["metadata"]["output_batch_id"], "obat_test")
        self.assertFalse(result["run_skipped_duplicate"])

    async def test_cancellation_during_run_does_not_reclassify_commit(self):
        committed = asyncio.Event()
        run_started = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/commit"):
                committed.set()
                return httpx.Response(
                    201,
                    json={
                        "status": "committed",
                        "input_batch_id": "ibat_" + "2" * 32,
                        "duplicate": False,
                        "committed_batch": {
                            "input_batch_id": "ibat_" + "2" * 32,
                        },
                    },
                )
            if request.url.path.endswith("/run"):
                run_started.set()
                await asyncio.Event().wait()
            raise AssertionError(f"unexpected request: {request.url}")

        client = InstanceScopedTelegramArtifactGatewayClient(
            gateway_url="http://gateway.test",
            api_key="key",
            client_instance_id="default",
            transport=httpx.MockTransport(handler),
        )
        input_batch_id = "ibat_" + "2" * 32

        with self.assertLogs("TelegramServer.ScopedBridge", level="INFO") as logs:
            task = asyncio.create_task(
                client.commit_and_run(
                    input_batch_id,
                    session_id="telegram:conversation:100",
                    progress_locale="ru",
                )
            )
            await committed.wait()
            await run_started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(committed.is_set())
        self.assertTrue(
            any(
                "telegram_input_batch_commit_finished" in line
                for line in logs.output
            )
        )
        self.assertTrue(
            any("telegram_agent_run_cancelled" in line for line in logs.output)
        )
        self.assertFalse(
            any("telegram_input_batch_commit_failed" in line for line in logs.output)
        )

    async def test_duplicate_commit_does_not_start_another_agent_run(self):
        paths: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path.endswith("/commit"):
                return httpx.Response(
                    200,
                    json={
                        "status": "committed",
                        "input_batch_id": "ibat_" + "3" * 32,
                        "duplicate": True,
                        "committed_batch": {
                            "input_batch_id": "ibat_" + "3" * 32,
                        },
                    },
                )
            raise AssertionError("duplicate commit must not call /run")

        client = InstanceScopedTelegramArtifactGatewayClient(
            gateway_url="http://gateway.test",
            api_key="key",
            client_instance_id="default",
            transport=httpx.MockTransport(handler),
        )
        input_batch_id = "ibat_" + "3" * 32

        result = await client.commit_and_run(
            input_batch_id,
            session_id="telegram:conversation:100",
            progress_locale="ru",
        )

        self.assertEqual(paths, [f"/input-batches/{input_batch_id}/commit"])
        self.assertTrue(result["duplicate"])
        self.assertTrue(result["run_skipped_duplicate"])


class LegacyDeliveryGuardCancellationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _scope(path: str, *, query_string: bytes = b"") -> dict:
        return {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query_string,
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
        }

    async def test_guard_rejects_legacy_telegram_content_without_task_wrapper(self):
        downstream_called = False
        sent: list[dict] = []

        async def downstream(scope, receive, send):
            nonlocal downstream_called
            downstream_called = True

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        guard = LegacyTelegramDeliveryGuardMiddleware(
            downstream,
            internal_api_key="internal",
        )
        await guard(
            self._scope(
                "/internal/deliveries/dlv_test/content",
                query_string=b"client_type=telegram",
            ),
            receive,
            send,
        )

        self.assertFalse(downstream_called)
        self.assertEqual(sent[0]["status"], 409)

    async def test_downstream_cancellation_propagates_unchanged(self):
        async def downstream(scope, receive, send):
            raise asyncio.CancelledError()

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            raise AssertionError("no response should be emitted")

        guard = LegacyTelegramDeliveryGuardMiddleware(
            downstream,
            internal_api_key="internal",
        )
        with self.assertRaises(asyncio.CancelledError):
            await guard(self._scope("/health"), receive, send)


if __name__ == "__main__":
    unittest.main()
