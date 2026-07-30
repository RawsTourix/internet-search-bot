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

    async def test_telegram_run_request_carries_exact_processing_status(self):
        captured: list[httpx.Request] = []

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

        bridge = RunScopedProgressTelegramGatewayClient(
            gateway_url="http://gateway",
            api_key="telegram-key",
            client_instance_id="bot-1",
            transport=httpx.MockTransport(handler),
        )
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


if __name__ == "__main__":
    unittest.main()
