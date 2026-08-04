import asyncio
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
from src.servers.telegram.media_group_runner import (
    LifetimeBoundDebouncedBatchRunner,
    LifetimeMediaGroupActivityCoordinator,
)
from src.servers.telegram.run_progress_bridge import (
    RunScopedProgressTelegramGatewayClient,
)


class TelegramExactGroupCleanupTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _envelope(number: int) -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key=f"telegram:bot-1:update:album-{number}",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(conversation_id="chat-1"),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id=f"update-{number}",
            source_message_id=f"message-{number}",
            source_group_id=f"album-{number}",
            occurred_at=datetime.now(timezone.utc),
            text_parts=[
                IngressTextPart(
                    part_id=f"caption-{number}",
                    kind="caption",
                    text=f"album {number}",
                )
            ],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-1",
            ),
        )

    async def test_third_group_callback_releases_third_group_only(self):
        batch_id = "ibat_" + "1" * 32

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/ingress/events":
                return httpx.Response(
                    202,
                    request=request,
                    json={
                        "status": "collecting",
                        "event_id": "evt_" + "2" * 32,
                        "input_batch_id": batch_id,
                        "duplicate": False,
                        "error_code": None,
                        "presentation_ref": None,
                        "presentation_event": {
                            "message_key": "input_batch.collecting",
                            "severity": "info",
                            "params": {
                                "assembly_mode": "explicit",
                                "commit_policy": "explicit",
                                "auto_commit_allowed": False,
                                "collection_id": "icol_" + "3" * 32,
                                "file_count": 3,
                                "text_part_count": 0,
                            },
                            "locale": "ru",
                        },
                    },
                )
            raise AssertionError(f"unexpected HTTP call: {request.url.path}")

        bridge = RunScopedProgressTelegramGatewayClient(
            gateway_url="http://gateway",
            api_key="telegram-key",
            client_instance_id="bot-1",
            transport=httpx.MockTransport(handler),
        )
        for number in (1, 2, 3):
            await bridge.submit_envelope(
                self._envelope(number),
                progress_locale="ru",
            )

        group_keys = sorted(bridge._input_groups)
        self.assertEqual(len(group_keys), 3)
        third_group_key = next(
            key for key in group_keys if key.endswith(":album-3")
        )
        remaining_expected = {
            key for key in group_keys if key != third_group_key
        }

        activity = LifetimeMediaGroupActivityCoordinator()
        runner = LifetimeBoundDebouncedBatchRunner(
            maximum_lifetime_seconds=1.0,
            activity=activity,
        )
        completed = asyncio.Event()

        async def callback() -> None:
            result = await bridge.commit_and_run(
                batch_id,
                session_id="telegram:conversation:chat-1",
                progress_locale="ru",
            )
            self.assertEqual(result["status"], "collecting")
            completed.set()

        scheduled = await runner.schedule(
            third_group_key,
            delay_seconds=0.0,
            callback=callback,
        )
        self.assertTrue(scheduled)
        await asyncio.wait_for(completed.wait(), timeout=1.0)
        await runner.cancel_all()

        self.assertEqual(set(bridge._input_groups), remaining_expected)
        self.assertEqual(
            bridge._input_batch_groups[batch_id],
            remaining_expected,
        )


if __name__ == "__main__":
    unittest.main()
