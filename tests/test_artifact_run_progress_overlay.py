import json
import unittest

import httpx
from pydantic import BaseModel, Field

from src.api.artifact_routes import _with_run_progress_metadata
from src.servers.telegram.run_progress_bridge import (
    RunScopedProgressTelegramGatewayClient,
)


class _Route(BaseModel):
    route_type: str = "telegram"
    conversation_id: str
    thread_id: str | None = None
    reply_to_message_id: str | None = None
    metadata: dict = Field(default_factory=dict)


class _Batch(BaseModel):
    input_batch_id: str
    session_id: str
    response_route: _Route


class RunProgressOverlayTests(unittest.IsolatedAsyncioTestCase):
    def test_overlay_is_non_persistent_and_overrides_only_run_presentation(self):
        batch = _Batch(
            input_batch_id="ibat_" + "1" * 32,
            session_id="telegram:conversation:chat-1",
            response_route=_Route(
                conversation_id="chat-1",
                reply_to_message_id="original-user-message",
                metadata={
                    "progress_callback_url": "http://telegram/internal/progress",
                    "progress_target": {
                        "chat_id": 1,
                        "message_id": 100,
                    },
                    "durable_marker": "keep",
                },
            ),
        )

        execution = _with_run_progress_metadata(
            batch,
            {
                "progress_target": {
                    "chat_id": 1,
                    "message_id": 200,
                },
                "status_message_id": 200,
                "progress_request_id": "run-1",
            },
        )

        self.assertIsNot(execution, batch)
        self.assertEqual(
            batch.response_route.metadata["progress_target"]["message_id"],
            100,
        )
        self.assertNotIn("status_message_id", batch.response_route.metadata)
        self.assertEqual(
            execution.response_route.metadata["progress_target"]["message_id"],
            200,
        )
        self.assertEqual(
            execution.response_route.metadata["durable_marker"],
            "keep",
        )
        self.assertEqual(
            execution.response_route.reply_to_message_id,
            "original-user-message",
        )

    @staticmethod
    def _bridge(captured: list[httpx.Request], *, maximum: int = 2048):
        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                request=request,
                json={
                    "status": "ok",
                    "response": "done",
                    "metadata": {},
                },
            )

        return RunScopedProgressTelegramGatewayClient(
            gateway_url="http://gateway",
            api_key="telegram-key",
            client_instance_id="bot-1",
            maximum_pending_run_presentations=maximum,
            transport=httpx.MockTransport(handler),
        )

    async def test_telegram_run_request_carries_exact_processing_status(self):
        captured: list[httpx.Request] = []
        bridge = self._bridge(captured)
        progress_metadata = {
            "progress_callback_url": "http://telegram/internal/progress",
            "progress_target": {"chat_id": 1, "message_id": 200},
            "status_message_id": 200,
        }

        result = await bridge.run_committed(
            "ibat_" + "2" * 32,
            session_id="telegram:conversation:chat-1",
            progress_locale="ru",
            progress_metadata=progress_metadata,
        )

        self.assertEqual(result["response"], "done")
        self.assertEqual(len(captured), 1)
        request = captured[0]
        self.assertEqual(
            request.url.path,
            "/input-batches/" + "ibat_" + "2" * 32 + "/run",
        )
        body = json.loads(request.content)
        self.assertEqual(body["session_id"], "telegram:conversation:chat-1")
        self.assertEqual(body["progress_locale"], "ru")
        self.assertEqual(body["progress_metadata"], progress_metadata)

    async def test_commit_and_run_forwards_exact_status_to_virtual_run(self):
        captured: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            if request.url.path.endswith("/commit"):
                payload = {
                    "status": "committed",
                    "input_batch_id": "ibat_" + "8" * 32,
                    "duplicate": False,
                    "metadata": {},
                }
            else:
                payload = {
                    "status": "ok",
                    "response": "done",
                    "metadata": {},
                }
            return httpx.Response(200, request=request, json=payload)

        bridge = RunScopedProgressTelegramGatewayClient(
            gateway_url="http://gateway",
            api_key="telegram-key",
            client_instance_id="bot-1",
            transport=httpx.MockTransport(handler),
        )
        batch_id = "ibat_" + "8" * 32
        progress_metadata = {
            "progress_callback_url": "http://telegram/internal/progress",
            "progress_target": {"chat_id": 1, "message_id": 800},
            "status_message_id": 800,
        }

        result = await bridge.commit_and_run(
            batch_id,
            session_id="telegram:conversation:chat-1",
            progress_locale="ru",
            progress_metadata=progress_metadata,
        )

        self.assertEqual(result["response"], "done")
        self.assertEqual(
            [request.url.path for request in captured],
            [
                f"/input-batches/{batch_id}/commit",
                f"/input-batches/{batch_id}/run",
            ],
        )
        run_body = json.loads(captured[1].content)
        self.assertEqual(run_body["progress_metadata"], progress_metadata)

    async def test_commit_and_run_resolves_status_after_commit_boundary(self):
        captured: list[httpx.Request] = []
        latest_message_id = 900

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal latest_message_id
            captured.append(request)
            if request.url.path.endswith("/commit"):
                latest_message_id = 901
                payload = {
                    "status": "committed",
                    "input_batch_id": "ibat_" + "9" * 32,
                    "duplicate": False,
                    "metadata": {},
                }
            else:
                payload = {
                    "status": "ok",
                    "response": "done",
                    "metadata": {},
                }
            return httpx.Response(200, request=request, json=payload)

        bridge = RunScopedProgressTelegramGatewayClient(
            gateway_url="http://gateway",
            api_key="telegram-key",
            client_instance_id="bot-1",
            transport=httpx.MockTransport(handler),
        )
        batch_id = "ibat_" + "9" * 32

        await bridge.commit_and_run(
            batch_id,
            session_id="telegram:conversation:chat-1",
            progress_locale="ru",
            progress_metadata={
                "progress_target": {"chat_id": 1, "message_id": 900},
            },
            progress_metadata_provider=lambda: {
                "progress_callback_url": "http://telegram/internal/progress",
                "progress_target": {
                    "chat_id": 1,
                    "message_id": latest_message_id,
                },
                "status_message_id": latest_message_id,
            },
        )

        run_body = json.loads(captured[1].content)
        self.assertEqual(
            run_body["progress_metadata"]["progress_target"]["message_id"],
            901,
        )
        self.assertEqual(run_body["progress_metadata"]["status_message_id"], 901)

    async def test_auto_status_is_consumed_once_by_exact_committed_run(self):
        captured: list[httpx.Request] = []
        bridge = self._bridge(captured)
        batch_id = "ibat_" + "3" * 32
        remembered = {
            "progress_callback_url": "http://telegram/internal/progress",
            "progress_target": {"chat_id": 1, "message_id": 300},
            "status_message_id": 300,
        }
        await bridge.remember_run_presentation(
            batch_id,
            progress_metadata=remembered,
        )

        await bridge.run_committed(
            batch_id,
            session_id="telegram:conversation:chat-1",
            progress_locale="ru",
        )
        await bridge.run_committed(
            batch_id,
            session_id="telegram:conversation:chat-1",
            progress_locale="ru",
        )

        first = json.loads(captured[0].content)
        second = json.loads(captured[1].content)
        self.assertEqual(first["progress_metadata"], remembered)
        self.assertEqual(second["progress_metadata"], {})

    async def test_explicit_run_metadata_overrides_and_consumes_remembered_status(self):
        captured: list[httpx.Request] = []
        bridge = self._bridge(captured)
        batch_id = "ibat_" + "4" * 32
        await bridge.remember_run_presentation(
            batch_id,
            progress_metadata={
                "progress_callback_url": "http://telegram/internal/progress",
                "progress_target": {"chat_id": 1, "message_id": 400},
            },
        )
        explicit = {
            "progress_callback_url": "http://telegram/internal/progress",
            "progress_target": {"chat_id": 1, "message_id": 401},
        }

        await bridge.run_committed(
            batch_id,
            session_id="telegram:conversation:chat-1",
            progress_locale="ru",
            progress_metadata=explicit,
        )
        await bridge.run_committed(
            batch_id,
            session_id="telegram:conversation:chat-1",
            progress_locale="ru",
        )

        first = json.loads(captured[0].content)
        second = json.loads(captured[1].content)
        self.assertEqual(first["progress_metadata"], explicit)
        self.assertEqual(second["progress_metadata"], {})

    async def test_pending_auto_bindings_are_bounded(self):
        captured: list[httpx.Request] = []
        bridge = self._bridge(captured, maximum=2)
        metadata = lambda message_id: {
            "progress_callback_url": "http://telegram/internal/progress",
            "progress_target": {"chat_id": 1, "message_id": message_id},
        }
        first = "ibat_" + "5" * 32
        second = "ibat_" + "6" * 32
        third = "ibat_" + "7" * 32
        await bridge.remember_run_presentation(
            first,
            progress_metadata=metadata(500),
        )
        await bridge.remember_run_presentation(
            second,
            progress_metadata=metadata(600),
        )
        await bridge.remember_run_presentation(
            third,
            progress_metadata=metadata(700),
        )

        await bridge.run_committed(
            first,
            session_id="telegram:conversation:chat-1",
            progress_locale="ru",
        )
        await bridge.run_committed(
            second,
            session_id="telegram:conversation:chat-1",
            progress_locale="ru",
        )
        await bridge.run_committed(
            third,
            session_id="telegram:conversation:chat-1",
            progress_locale="ru",
        )

        bodies = [json.loads(request.content) for request in captured]
        self.assertEqual(bodies[0]["progress_metadata"], {})
        self.assertEqual(
            bodies[1]["progress_metadata"]["progress_target"]["message_id"],
            600,
        )
        self.assertEqual(
            bodies[2]["progress_metadata"]["progress_target"]["message_id"],
            700,
        )

    def test_pending_run_presentation_limit_must_be_positive(self):
        with self.assertRaises(ValueError):
            self._bridge([], maximum=0)


if __name__ == "__main__":
    unittest.main()
