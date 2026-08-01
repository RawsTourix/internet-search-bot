import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


class TelegramCollectionRelocationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _envelope(suffix: str) -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key=f"telegram:bot-1:update:{suffix}",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(conversation_id="12345"),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id=f"update-{suffix}",
            source_message_id=f"message-{suffix}",
            occurred_at=datetime.now(timezone.utc),
            text_parts=[
                IngressTextPart(
                    part_id=f"text-{suffix}",
                    kind="message_text",
                    text=f"instruction {suffix}",
                )
            ],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="12345",
            ),
        )

    @staticmethod
    def _submission(*, policy: str, message_id: str | None) -> dict:
        ref = {
            "presentation_id": "iprs_" + "1" * 32,
            "presentation_token": "presentation-token",
            "client_message_id": message_id,
            "active_client_message_id": message_id,
            "state": "bound" if message_id else "reserved",
            "presentation_generation": 1 if message_id else 0,
            "relocation_generation": None,
            "previous_client_message_id": None,
        }
        if policy == "relocate":
            ref.update({
                "client_message_id": "100",
                "active_client_message_id": "100",
                "state": "bound",
                "presentation_generation": 1,
                "relocation_generation": 2,
                "previous_client_message_id": "100",
            })
        return {
            "status": "collecting",
            "event_id": "evt_" + "2" * 32,
            "input_batch_id": "ibat_" + "3" * 32,
            "duplicate": False,
            "error_code": None,
            "ack_policy": policy,
            "presentation_ref": ref,
            "presentation_event": {
                "message_key": "input_batch.collecting",
                "severity": "info",
                "params": {
                    "assembly_mode": "explicit",
                    "commit_policy": "explicit",
                    "auto_commit_allowed": False,
                    "collection_id": "icol_" + "4" * 32,
                    "file_count": 1 if policy == "create" else 2,
                    "text_part_count": 0,
                },
                "locale": "ru",
            },
        }

    async def test_relocation_confirms_new_generation_and_clears_pending_cache(self):
        requests: list[str] = []
        submissions = [
            self._submission(policy="create", message_id=None),
            self._submission(policy="relocate", message_id="100"),
        ]

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            if request.url.path == "/ingress/events":
                return httpx.Response(
                    202,
                    request=request,
                    json=submissions.pop(0),
                )
            if request.url.path.endswith("/bind"):
                return httpx.Response(
                    200,
                    request=request,
                    json={"state": "bound", "client_message_id": "100"},
                )
            if request.url.path.endswith("/relocate"):
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "input_batch_id": "ibat_" + "3" * 32,
                        "state": "bound",
                        "client_message_id": "200",
                        "presentation_generation": 2,
                    },
                )
            if request.url.path.endswith("/superseded-deletion"):
                return httpx.Response(
                    200,
                    request=request,
                    json={"state": "bound", "deletion_state": "deleted"},
                )
            raise AssertionError(f"unexpected request: {request.url.path}")

        bridge = ExplicitCollectionTelegramGatewayClient(
            gateway_url="http://gateway",
            api_key="telegram-key",
            client_instance_id="bot-1",
            transport=httpx.MockTransport(handler),
        )
        first = await bridge.submit_envelope(
            self._envelope("first"),
            progress_locale="ru",
        )
        await bridge.bind_input_presentation(
            first["presentation_ref"],
            session_id="telegram:conversation:12345",
            client_message_id="100",
        )
        await bridge.remember_input_presentation_handle(
            first,
            client_message_id="100",
        )

        second = await bridge.submit_envelope(
            self._envelope("second"),
            progress_locale="ru",
        )
        delete_message = AsyncMock()
        stop_progress = AsyncMock()
        fake_application = SimpleNamespace(
            bot=SimpleNamespace(delete_message=delete_message)
        )
        with (
            patch(
                "src.servers.telegram.telegram_server.application",
                fake_application,
            ),
            patch(
                "src.servers.telegram.telegram_server.stop_progress_edits",
                stop_progress,
            ),
        ):
            await bridge.bind_input_presentation(
                second["presentation_ref"],
                session_id="telegram:conversation:12345",
                client_message_id="200",
            )
        await bridge.remember_input_presentation_handle(
            second,
            client_message_id="200",
        )

        state = bridge._explicit_batches[second["input_batch_id"]]
        ref = state["presentation_ref"]
        self.assertEqual(ref["active_client_message_id"], "200")
        self.assertEqual(ref["presentation_generation"], 2)
        self.assertIsNone(ref["relocation_generation"])
        self.assertIsNone(ref["previous_client_message_id"])
        delete_message.assert_awaited_once_with(chat_id="12345", message_id=100)
        stop_progress.assert_awaited_once_with(chat_id="12345", message_id=100)
        self.assertEqual(
            requests,
            [
                "/ingress/events",
                "/internal/input-presentations/iprs_"
                + "1" * 32
                + "/bind",
                "/ingress/events",
                "/internal/input-presentations/iprs_"
                + "1" * 32
                + "/relocate",
                "/internal/input-presentations/iprs_"
                + "1" * 32
                + "/superseded-deletion",
            ],
        )


if __name__ == "__main__":
    unittest.main()
