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

    def _group_envelope(self, suffix: str) -> ClientInputEnvelope:
        envelope = self._envelope()
        return envelope.model_copy(update={
            "idempotency_key": f"telegram:bot-1:update:group-{suffix}",
            "source_update_id": f"group-update-{suffix}",
            "source_message_id": f"group-message-{suffix}",
            "source_group_id": f"album-{suffix}",
            "text_parts": [
                IngressTextPart(
                    part_id=f"group-text-part-{suffix}",
                    kind="caption",
                    text=f"album caption {suffix}",
                )
            ],
        })

    @staticmethod
    def _explicit_submission() -> dict:
        return {
            "status": "collecting",
            "event_id": "evt_" + "1" * 32,
            "input_batch_id": "ibat_" + "2" * 32,
            "duplicate": False,
            "error_code": None,
            "ack_policy": "create",
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

    async def test_first_event_replaces_provisional_collection_command_status(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/internal/input-collections/start":
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
            if request.url.path == "/ingress/events":
                return httpx.Response(
                    202,
                    request=request,
                    json=self._explicit_submission(),
                )
            if request.url.path.endswith("/bind"):
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "input_batch_id": "ibat_" + "2" * 32,
                        "client_message_id": "99",
                        "presentation_generation": 1,
                        "state": "bound",
                    },
                )
            raise AssertionError(f"unexpected HTTP call: {request.url.path}")

        bridge = ExplicitCollectionTelegramGatewayClient(
            gateway_url="http://gateway",
            api_key="telegram-key",
            client_instance_id="bot-1",
            transport=httpx.MockTransport(handler),
        )
        session_id = "telegram:conversation:chat-1"
        await bridge.start_collection(
            session_id=session_id,
            chat_id="chat-1",
            thread_id=None,
            principal_id="user-1",
            idempotency_key="collect-1",
            locale="ru",
            response_route={
                "route_type": "telegram",
                "conversation_id": "chat-1",
                "metadata": {
                    "progress_target": {
                        "chat_id": "chat-1",
                        "message_id": 42,
                    }
                },
            },
        )
        submission = await bridge.submit_envelope(
            self._envelope(),
            progress_locale="ru",
        )
        self.assertEqual(submission["ack_policy"], "create")
        self.assertEqual(
            submission["_telegram_previous_unbound_status_message_id"],
            "42",
        )
        await bridge.bind_input_presentation(
            submission["presentation_ref"],
            session_id=session_id,
            client_message_id="99",
        )
        await bridge.remember_input_presentation_handle(
            submission,
            client_message_id="99",
        )
        pending = await bridge.commit_and_run(
            submission["input_batch_id"],
            session_id=session_id,
            progress_locale="ru",
        )

        self.assertEqual(
            pending["metadata"]["presentation_message_id"],
            "99",
        )
        self.assertEqual(
            pending["metadata"]["input_batch_id"],
            submission["input_batch_id"],
        )

    async def test_out_of_order_submissions_cannot_regress_counts(self):
        counts = iter(((10, 3), (8, 2)))

        async def handler(request: httpx.Request) -> httpx.Response:
            file_count, text_count = next(counts)
            payload = self._explicit_submission()
            payload["presentation_event"]["params"].update({
                "file_count": file_count,
                "text_part_count": text_count,
            })
            return httpx.Response(202, request=request, json=payload)

        bridge = ExplicitCollectionTelegramGatewayClient(
            gateway_url="http://gateway",
            api_key="telegram-key",
            client_instance_id="bot-1",
            transport=httpx.MockTransport(handler),
        )
        first = await bridge.submit_envelope(
            self._group_envelope("newer"),
            progress_locale="ru",
        )
        await bridge.submit_envelope(
            self._group_envelope("older"),
            progress_locale="ru",
        )
        pending = await bridge.commit_and_run(
            first["input_batch_id"],
            session_id="telegram:conversation:chat-1",
            progress_locale="ru",
        )

        self.assertEqual(pending["metadata"]["file_count"], 10)
        self.assertEqual(pending["metadata"]["text_part_count"], 3)

    async def test_collection_failure_notification_is_claimed_once(self):
        bridge = ExplicitCollectionTelegramGatewayClient(
            gateway_url="http://gateway",
            api_key="telegram-key",
            client_instance_id="bot-1",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500, request=request)
            ),
        )
        session_id = "telegram:conversation:chat-1"
        batch_id = "ibat_" + "2" * 32
        bridge._active_collection_sessions.add(session_id)
        bridge._explicit_batches[batch_id] = {
            "session_id": session_id,
            "collection_id": "icol_" + "3" * 32,
            "file_count": 25,
            "text_part_count": 3,
            "presentation_ref": {"client_message_id": "42"},
        }

        first = await bridge.claim_explicit_ingress_failure(session_id)
        second = await bridge.claim_explicit_ingress_failure(session_id)

        self.assertTrue(first)
        self.assertFalse(second)
        async with bridge.explicit_presentation_guard(batch_id) as state:
            self.assertTrue(state["terminal"])
            self.assertEqual(state["action"], "failed")

    async def test_active_collection_does_not_bind_text_to_one_of_many_albums(self):
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            requests.append(payload)
            return httpx.Response(
                202,
                request=request,
                json=self._explicit_submission(),
            )

        bridge = ExplicitCollectionTelegramGatewayClient(
            gateway_url="http://gateway",
            api_key="telegram-key",
            client_instance_id="bot-1",
            transport=httpx.MockTransport(handler),
        )
        bridge._active_collection_sessions.add(
            "telegram:conversation:chat-1"
        )

        await bridge.submit_envelope(
            self._group_envelope("1"),
            progress_locale="ru",
        )
        await bridge.submit_envelope(
            self._group_envelope("2"),
            progress_locale="ru",
        )
        text = await bridge.submit_envelope(
            self._envelope(),
            progress_locale="ru",
        )

        self.assertEqual(text["input_batch_id"], "ibat_" + "2" * 32)
        self.assertEqual(
            [item.get("source_group_id") for item in requests],
            ["album-1", "album-2", None],
        )

    async def test_multiple_groups_release_one_callback_at_a_time(self):
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
            raise AssertionError(f"unexpected HTTP call: {request.url.path}")

        bridge = ExplicitCollectionTelegramGatewayClient(
            gateway_url="http://gateway",
            api_key="telegram-key",
            client_instance_id="bot-1",
            transport=httpx.MockTransport(handler),
        )
        for suffix in ("1", "2", "3"):
            await bridge.submit_envelope(
                self._group_envelope(suffix),
                progress_locale="ru",
            )

        self.assertEqual(
            len(bridge._input_batch_groups[batch_id]),
            3,
        )
        self.assertEqual(len(bridge._input_groups), 3)

        for remaining in (2, 1, 0):
            pending = await bridge.commit_and_run(
                batch_id,
                session_id="telegram:conversation:chat-1",
                progress_locale="ru",
            )
            self.assertEqual(pending["status"], "collecting")
            self.assertEqual(
                len(bridge._input_batch_groups.get(batch_id, set())),
                remaining,
            )
            self.assertEqual(len(bridge._input_groups), remaining)

        self.assertEqual(
            requests,
            ["/ingress/events", "/ingress/events", "/ingress/events"],
        )

    async def test_send_closes_every_group_for_explicit_batch(self):
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
                        "file_count": 21,
                        "text_part_count": 2,
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
        for suffix in ("1", "2", "3"):
            await bridge.submit_envelope(
                self._group_envelope(suffix),
                progress_locale="ru",
            )

        committed = await bridge.send_collection(
            session_id="telegram:conversation:chat-1",
            chat_id="chat-1",
            thread_id=None,
            principal_id="user-1",
            idempotency_key="send-multi-group",
        )

        self.assertEqual(committed["status"], "committed")
        self.assertNotIn(batch_id, bridge._input_batch_groups)
        self.assertEqual(bridge._input_groups, {})
        self.assertEqual(
            requests,
            [
                "/ingress/events",
                "/ingress/events",
                "/ingress/events",
                "/internal/input-collections/send",
            ],
        )

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

    async def test_stale_concurrent_handle_cannot_overwrite_relocation(self):
        bridge = ExplicitCollectionTelegramGatewayClient(
            gateway_url="http://gateway",
            api_key="telegram-key",
            client_instance_id="bot-1",
        )
        batch_id = "ibat_" + "7" * 32
        bridge._explicit_batches[batch_id] = {
            "session_id": "telegram:conversation:chat-1",
            "presentation_ref": {
                "client_message_id": "200",
                "active_client_message_id": "200",
                "presentation_generation": 2,
            },
        }

        await bridge.remember_input_presentation_handle(
            {
                "input_batch_id": batch_id,
                "ack_policy": "update_existing",
                "presentation_ref": {
                    "client_message_id": "100",
                    "active_client_message_id": "100",
                    "presentation_generation": 1,
                },
            },
            client_message_id="100",
        )

        self.assertEqual(
            bridge._explicit_batches[batch_id]["presentation_ref"]
            ["active_client_message_id"],
            "200",
        )

    async def test_successful_relocation_updates_session_status_cache(self):
        batch_id = "ibat_" + "8" * 32
        session_id = "telegram:conversation:chat-1"

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                request=request,
                json={
                    "input_batch_id": batch_id,
                    "client_message_id": "200",
                    "presentation_generation": 2,
                    "state": "bound",
                },
            )

        bridge = ExplicitCollectionTelegramGatewayClient(
            gateway_url="http://gateway",
            api_key="telegram-key",
            client_instance_id="bot-1",
            transport=httpx.MockTransport(handler),
        )
        bridge._explicit_batches[batch_id] = {
            "session_id": session_id,
            "presentation_ref": {
                "client_message_id": "100",
                "active_client_message_id": "100",
                "presentation_generation": 1,
            },
        }
        bridge._active_collection_status_messages[session_id] = "100"

        await bridge.relocate_input_presentation(
            {
                "presentation_id": "iprs_" + "8" * 32,
                "presentation_token": "token",
                "client_message_id": "100",
                "active_client_message_id": "100",
                "presentation_generation": 1,
            },
            session_id=session_id,
            client_message_id="200",
        )

        self.assertEqual(
            bridge._active_collection_status_messages[session_id],
            "200",
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

        self.assertTrue(await bridge.is_explicit_collection_active(
            "telegram:conversation:chat-1"
        ))
        await bridge.clear_session_state("telegram:conversation:chat-1")
        self.assertFalse(await bridge.is_explicit_collection_active(
            "telegram:conversation:chat-1"
        ))

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
