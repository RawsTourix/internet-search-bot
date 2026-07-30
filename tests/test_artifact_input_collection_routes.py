import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
from fastapi import FastAPI, Header

from src.api.input_collection_routes import create_input_collection_router
from src.ingress import (
    InputDraftControlAction,
    InputDraftControlResult,
    InputDraftControlStatus,
)


class InputCollectionRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = SimpleNamespace(
            start_collection=AsyncMock(
                return_value=InputDraftControlResult(
                    action=InputDraftControlAction.START,
                    status=InputDraftControlStatus.STARTED,
                )
            ),
            inspect=AsyncMock(
                return_value=InputDraftControlResult(
                    action=InputDraftControlAction.INSPECT,
                    status=InputDraftControlStatus.NOT_FOUND,
                )
            ),
            commit=AsyncMock(
                return_value=InputDraftControlResult(
                    action=InputDraftControlAction.COMMIT,
                    status=InputDraftControlStatus.EMPTY,
                )
            ),
            cancel=AsyncMock(
                return_value=InputDraftControlResult(
                    action=InputDraftControlAction.CANCEL,
                    status=InputDraftControlStatus.NOT_FOUND,
                )
            ),
        )
        self.mcp_client = SimpleNamespace(
            abandon_pending_cycle_for_new_task=Mock(return_value="cycle-old"),
        )
        api = SimpleNamespace(
            ingress_services=SimpleNamespace(
                draft_control_service=self.service,
            ),
            mcp_client=self.mcp_client,
        )

        async def auth(x_api_key: str | None = Header(default=None)) -> str:
            return str(x_api_key or "")

        app = FastAPI()
        app.include_router(
            create_input_collection_router(
                api=api,
                auth_dependency=auth,
                api_key_scopes={
                    "telegram-key": frozenset({"telegram"}),
                    "internal-key": frozenset({"*"}),
                },
                api_key_instance_scopes={
                    "telegram-key": frozenset({("telegram", "bot-1")}),
                    "internal-key": frozenset({("*", "*")}),
                },
            )
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    @staticmethod
    def _scope(*, instance="bot-1"):
        return {
            "session_id": "telegram:conversation:chat-1",
            "client_type": "telegram",
            "client_instance_id": instance,
            "conversation_id": "chat-1",
            "thread_id": None,
            "principal_id": "user-1",
        }

    @classmethod
    def _start_body(cls, *, route_conversation_id="chat-1"):
        return {
            **cls._scope(),
            "idempotency_key": "collect-update-1",
            "locale": "ru",
            "response_route": {
                "route_type": "telegram",
                "conversation_id": route_conversation_id,
                "thread_id": None,
                "reply_to_message_id": "11",
                "metadata": {},
            },
        }

    async def test_authorized_start_uses_exact_scope_and_idempotency(self):
        response = await self.client.post(
            "/internal/input-collections/start",
            headers={"X-API-Key": "telegram-key"},
            json=self._start_body(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "started")
        self.service.start_collection.assert_awaited_once()
        call = self.service.start_collection.await_args
        scope = call.args[0]
        self.assertEqual(scope.client_instance_id, "bot-1")
        self.assertEqual(scope.conversation.conversation_id, "chat-1")
        self.assertEqual(scope.principal_id, "user-1")
        self.assertEqual(call.kwargs["idempotency_key"], "collect-update-1")

    async def test_new_collection_abandons_waiting_cycle_as_fresh_task(self):
        response = await self.client.post(
            "/internal/input-collections/start",
            headers={"X-API-Key": "telegram-key"},
            json=self._start_body(),
        )

        self.assertEqual(response.status_code, 200)
        self.mcp_client.abandon_pending_cycle_for_new_task.assert_called_once_with(
            "telegram:conversation:chat-1",
            reason="explicit_collection_started",
        )

    async def test_already_active_collection_does_not_abandon_waiting_cycle(self):
        self.service.start_collection.return_value = InputDraftControlResult(
            action=InputDraftControlAction.START,
            status=InputDraftControlStatus.ALREADY_ACTIVE,
        )

        response = await self.client.post(
            "/internal/input-collections/start",
            headers={"X-API-Key": "telegram-key"},
            json=self._start_body(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "already_active")
        self.mcp_client.abandon_pending_cycle_for_new_task.assert_not_called()

    async def test_transport_key_cannot_control_another_instance(self):
        response = await self.client.post(
            "/internal/input-collections/inspect",
            headers={"X-API-Key": "telegram-key"},
            json=self._scope(instance="bot-2"),
        )

        self.assertEqual(response.status_code, 403)
        self.service.inspect.assert_not_awaited()

    async def test_start_rejects_response_route_outside_scope(self):
        body = self._start_body(route_conversation_id="another-chat")
        body["idempotency_key"] = "collect-update-2"
        response = await self.client.post(
            "/internal/input-collections/start",
            headers={"X-API-Key": "telegram-key"},
            json=body,
        )

        self.assertEqual(response.status_code, 422)
        self.service.start_collection.assert_not_awaited()
        self.mcp_client.abandon_pending_cycle_for_new_task.assert_not_called()

    async def test_send_commits_without_starting_agent_in_router(self):
        response = await self.client.post(
            "/internal/input-collections/send",
            headers={"X-API-Key": "telegram-key"},
            json={
                **self._scope(),
                "idempotency_key": "send-update-1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "empty")
        self.service.commit.assert_awaited_once()
        call = self.service.commit.await_args
        self.assertEqual(call.kwargs["idempotency_key"], "send-update-1")


if __name__ == "__main__":
    unittest.main()
