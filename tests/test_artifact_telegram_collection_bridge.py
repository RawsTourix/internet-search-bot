import json
import unittest
from datetime import datetime, timezone

import httpx

from src.core.models import ClientType
from src.ingress import (
    ClientConversationRef,
    ClientInputEnvelope,
    ClientResponseRoute,
    ClientSenderRef,
    IngressTextPart,
)
from src.servers.telegram.collection_bridge import (
    ExplicitCollectionTelegramGatewayClient,
)


class TelegramCollectionBridgeTests(unittest.IsolatedAsyncioTestCase):
    def _envelope(self) -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key="telegram:bot-1:update:text-1",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(conversation_id="chat-1"),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id="update-1",
            source_message_id="message-1",
            occurred_at=datetime.now(timezone.utc),
            text_parts=[
                IngressTextPart(
                    part_id="text-part-1",
                    kind="message_text",
                    text="first collected instruction",
                )
            ],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-1",
            ),
        )

    @staticmethod
    def _explicit_submission() -> dict:
        return {
            "status": "collecting",
            "event_id": "evt_" + "1" * 32,
            "input_batch_id": "ibat_" + "2" * 32,
            "duplicate": False,
            "error_code": None,
            "presentation_ref": {
                "presentation_id": "iprs_" + "4" * 32,
                "presentation_token": "plain-token",
                "client_message_id": None,
                "active_client_message_id": None,
                "state": "reserved",
                "presentation_generation": 0,
                "relocation_generation": None,
                "previous_client_message_id": None,
            },
            "presentation_event": {
                "message_key": "input_batch.collecting",
                "severity": "info",
                "params": {
                    "assembly_mode": "explicit",
                    "commit_policy": "explicit",
                    "auto_commit_allowed": False,
                    "collection_id": "icol_" + "3" * 32,
                    "file_count": 0,
                    "text_part_count": 1,
                },
                "locale": "ru",
            },
        }

    async def test_explicit_submission_suppresses_transport_commit_and_run(self):
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/ingress/events":
                return httpx.Response(
                    202,
                    request=request,
                    json=self._explicit_submission(),
                )
            raise AssertionError(f"unexpected HTTP call: {request.url.path}")

        bridge = ExplicitCollectionTelegramGatewayClient(
            gateway_url="http://gateway",
            api_key="telegram-key",
            client_instance_id="bot-1",
            transport=httpx.MockTransport(handler),
        )
        submission = await bridge.submit_envelope(
            self._envelope(),
            progress_locale="ru",
        )
        self.assertEqual(submission["status"], "collecting")
        self.assertEqual(len(requests), 1)

        pending = await bridge.commit_and_run(
            submission["input_batch_id"],
            session_id="telegram:conversation:chat-1",
            progress_locale="ru",
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(pending["status"], "collecting")
        self.assertTrue(pending["metadata"]["input_collection_pending"])
        self.assertEqual(pending["metadata"]["text_part_count"], 1)

    async def test_send_redirects_current_status_and_suppresses_late_group(self):
        requests = []
        submission_payload = self._explicit_submission()
        batch_id = submission_payload["input_batch_id"]

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            if request.url.path == "/ingress/events":
                return httpx.Response(
                    202,
                    request=request,
                    json=submission_payload,
                )
            if request.url.path == "/internal/input-collections/send":
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "action": "send",
                        "status": "committed",
                        "duplicate": False,
                        "collection": None,
                        "input_batch_id": batch_id,
                        "draft_state": "committed",
                        "file_count": 3,
                        "text_part_count": 1,
                        "semantic_part_count": 0,
                        "committed_batch": {"input_batch_id": batch_id},
                        "error_code": None,
                    },
                )
            raise AssertionError(f"unexpected HTTP call: {request.url.path}")

        bridge = ExplicitCollectionTelegramGatewayClient(
            gateway_url="http://gateway",
            api_key="telegram-key",
            client_instance_id="bot-1",
            transport=httpx.MockTransport(handler),
        )
        submission = await bridge.submit_envelope(
            self._envelope(),
            progress_locale="ru",
        )
        await bridge.remember_input_presentation_handle(
            submission,
            client_message_id="42",
        )

        committed = await bridge.send_collection(
            session_id="telegram:conversation:chat-1",
            chat_id="chat-1",
            thread_id=None,
            principal_id="user-1",
            idempotency_key="send-1",
        )
        self.assertEqual(
            committed["_telegram_previous_status_message_id"],
            "42",
        )

        late = await bridge.commit_and_run(
            batch_id,
            session_id="telegram:conversation:chat-1",
            progress_locale="ru",
        )
        self.assertEqual(late["status"], "suppressed")
        self.assertTrue(
            late["metadata"]["input_collection_terminal_suppressed"]
        )
        self.assertEqual(late["metadata"]["terminal_action"], "committed")
        self.assertEqual(
            requests,
            ["/ingress/events", "/internal/input-collections/send"],
        )

    async def test_cancel_suppresses_late_group_without_commit_request(self):
        submission_payload = self._explicit_submission()
        batch_id = submission_payload["input_batch_id"]
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            if request.url.path == "/ingress/events":
                return httpx.Response(202, request=request, json=submission_payload)
            if request.url.path == "/internal/input-collections/cancel":
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "action": "cancel",
                        "status": "cancelled",
                        "duplicate": False,
                        "collection": None,
                        "input_batch_id": batch_id,
                        "draft_state": "cancelled",
                        "file_count": 3,
                        "text_part_count": 0,
                        "semantic_part_count": 0,
                        "committed_batch": None,
                        "error_code": None,
                    },
                )
            raise AssertionError(f"unexpected HTTP call: {request.url.path}")

        bridge = ExplicitCollectionTelegramGatewayClient(
            gateway_url="http://gateway",
            api_key="telegram-key",
            client_instance_id="bot-1",
            transport=httpx.MockTransport(handler),
        )
        await bridge.submit_envelope(self._envelope(), progress_locale="ru")
        await bridge.cancel_collection(
            session_id="telegram:conversation:chat-1",
            chat_id="chat-1",
            thread_id=None,
            principal_id="user-1",
            idempotency_key="cancel-1",
        )
        late = await bridge.commit_and_run(
            batch_id,
            session_id="telegram:conversation:chat-1",
            progress_locale="ru",
        )
        self.assertTrue(
            late["metadata"]["input_collection_terminal_suppressed"]
        )
        self.assertEqual(late["metadata"]["terminal_action"], "cancelled")
        self.assertEqual(
            requests,
            ["/ingress/events", "/internal/input-collections/cancel"],
        )

    async def test_control_request_contains_exact_instance_authority(self):
        captured = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                request=request,
                json={
                    "action": "start",
                    "status": "started",
                    "duplicate": False,
                    "collection": None,
                    "input_batch_id": None,
                    "draft_state": None,
                    "file_count": 0,
                    "text_part_count": 0,
                    "semantic_part_count": 0,
                    "committed_batch": None,
                    "error_code": None,
                },
            )

        bridge = ExplicitCollectionTelegramGatewayClient(
            gateway_url="http://gateway",
            api_key="telegram-key",
            client_instance_id="bot-1",
            transport=httpx.MockTransport(handler),
        )
        await bridge.start_collection(
            session_id="telegram:conversation:chat-1",
            chat_id="chat-1",
            thread_id="topic-1",
            principal_id="user-1",
            idempotency_key="collect-update-1",
            locale="ru",
            response_route={
                "route_type": "telegram",
                "conversation_id": "chat-1",
                "thread_id": "topic-1",
                "reply_to_message_id": "10",
                "metadata": {},
            },
        )

        self.assertEqual(len(captured), 1)
        request = captured[0]
        self.assertEqual(request.url.path, "/internal/input-collections/start")
        self.assertEqual(request.headers["X-API-Key"], "telegram-key")
        body = json.loads(request.content)
        self.assertEqual(body["client_type"], "telegram")
        self.assertEqual(body["client_instance_id"], "bot-1")
        self.assertEqual(body["conversation_id"], "chat-1")
        self.assertEqual(body["thread_id"], "topic-1")
        self.assertEqual(body["principal_id"], "user-1")
        self.assertEqual(body["idempotency_key"], "collect-update-1")


if __name__ == "__main__":
    unittest.main()
