"""Telegram client for explicit InputBatch controls and presentation relocation."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any

from .presentation_relocation import relocate_precreated_input_presentation
from .scoped_artifact_bridge import InstanceScopedTelegramArtifactGatewayClient


class ExplicitCollectionTelegramGatewayClient(
    InstanceScopedTelegramArtifactGatewayClient
):
    """Call shared controls and suppress transport auto-commit for explicit drafts."""

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)
        self._explicit_batch_lock = asyncio.Lock()
        self._explicit_batches: dict[str, dict[str, Any]] = {}
        # A media-group quiet timer may fire after /send or /cancel has already
        # made the explicit collection terminal. Keep a bounded tombstone so
        # that the late transport callback becomes a no-op instead of falling
        # through to the ordinary AUTO commit endpoint and producing HTTP 409.
        self._terminal_explicit_batches: OrderedDict[
            str,
            dict[str, Any],
        ] = OrderedDict()
        self._maximum_terminal_explicit_batches = 512

    @staticmethod
    def _merge_presentation_ref(
        current: dict[str, Any] | None,
        incoming: dict[str, Any] | None,
        *,
        client_message_id: str | None = None,
    ) -> dict[str, Any]:
        merged = dict(current or {})
        for key, value in dict(incoming or {}).items():
            if value is not None:
                merged[key] = value
        if client_message_id is not None:
            merged["client_message_id"] = str(client_message_id)
            merged["active_client_message_id"] = str(client_message_id)
            generation = (
                merged.get("relocation_generation")
                or merged.get("presentation_generation")
                or 1
            )
            merged["presentation_generation"] = max(1, int(generation))
        return merged

    def _remember_terminal_locked(
        self,
        input_batch_id: str,
        *,
        action: str,
        explicit: dict[str, Any] | None,
    ) -> dict[str, Any]:
        state = {
            "action": str(action),
            "collection_id": (explicit or {}).get("collection_id"),
            "file_count": int((explicit or {}).get("file_count") or 0),
            "text_part_count": int(
                (explicit or {}).get("text_part_count") or 0
            ),
            "presentation_ref": dict(
                (explicit or {}).get("presentation_ref") or {}
            ),
        }
        self._terminal_explicit_batches[input_batch_id] = state
        self._terminal_explicit_batches.move_to_end(input_batch_id)
        while (
            len(self._terminal_explicit_batches)
            > self._maximum_terminal_explicit_batches
        ):
            self._terminal_explicit_batches.popitem(last=False)
        return state

    @staticmethod
    def _current_message_id(state: dict[str, Any] | None) -> str | None:
        ref = dict((state or {}).get("presentation_ref") or {})
        value = ref.get("active_client_message_id") or ref.get(
            "client_message_id"
        )
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    async def submit_envelope(
        self,
        envelope,
        *,
        progress_locale: str,
    ) -> dict[str, Any]:
        payload = await super().submit_envelope(
            envelope,
            progress_locale=progress_locale,
        )
        params = dict(
            ((payload.get("presentation_event") or {}).get("params") or {})
        )
        batch_id = str(payload.get("input_batch_id") or "").strip()
        if (
            batch_id
            and params.get("assembly_mode") == "explicit"
            and params.get("auto_commit_allowed") is False
        ):
            async with self._explicit_batch_lock:
                if batch_id not in self._terminal_explicit_batches:
                    current = dict(self._explicit_batches.get(batch_id) or {})
                    current.update({
                        "collection_id": params.get("collection_id"),
                        "file_count": int(params.get("file_count") or 0),
                        "text_part_count": int(
                            params.get("text_part_count") or 0
                        ),
                    })
                    current["presentation_ref"] = self._merge_presentation_ref(
                        current.get("presentation_ref"),
                        payload.get("presentation_ref"),
                    )
                    self._explicit_batches[batch_id] = current
        return payload

    async def bind_input_presentation(
        self,
        presentation_ref: dict[str, Any] | None,
        *,
        session_id: str,
        client_message_id: str,
    ) -> dict[str, Any] | None:
        """Bind an initial handle or execute one reserved relocation generation."""

        ref = dict(presentation_ref or {})
        relocation_reserved = (
            ref.get("relocation_generation") is not None
            or ref.get("previous_client_message_id") is not None
        )
        if not relocation_reserved:
            return await super().bind_input_presentation(
                ref,
                session_id=session_id,
                client_message_id=client_message_id,
            )

        from . import telegram_server as server

        status_message = SimpleNamespace(
            message_id=int(client_message_id),
            chat_id=self._chat_id_from_session(session_id),
        )
        await relocate_precreated_input_presentation(
            server=server,
            gateway=self,
            submission={
                "ack_policy": "relocate",
                "presentation_ref": ref,
            },
            session_id=session_id,
            status_message=status_message,
            chat_id=status_message.chat_id,
            cleanup_unbound=False,
            raise_on_bind_failure=True,
        )
        return {
            "state": "relocated",
            "client_message_id": str(client_message_id),
        }

    async def remember_input_presentation_handle(
        self,
        submission: dict[str, Any],
        *,
        client_message_id: str,
    ) -> None:
        """Remember only a handle confirmed by the durable presentation store."""

        batch_id = str(submission.get("input_batch_id") or "").strip()
        if not batch_id:
            return
        async with self._explicit_batch_lock:
            if batch_id in self._terminal_explicit_batches:
                return
            current = self._explicit_batches.get(batch_id)
            if current is None:
                return
            current = dict(current)
            if str(submission.get("ack_policy") or "") == "relocate":
                current_ref = dict(current.get("presentation_ref") or {})
                active_message_id = (
                    current_ref.get("active_client_message_id")
                    or current_ref.get("client_message_id")
                )
                if str(active_message_id or "") != str(client_message_id):
                    return
                # relocate_input_presentation already persisted and cached the
                # exact new generation. Re-merging the stale reservation ref
                # would reintroduce pending relocation fields.
                self._explicit_batches[batch_id] = current
                return
            current["presentation_ref"] = self._merge_presentation_ref(
                current.get("presentation_ref"),
                submission.get("presentation_ref"),
                client_message_id=str(client_message_id),
            )
            self._explicit_batches[batch_id] = current

    async def commit_and_run(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        progress_locale: str,
    ) -> dict[str, Any]:
        async with self._explicit_batch_lock:
            terminal = dict(
                self._terminal_explicit_batches.get(input_batch_id) or {}
            )
            explicit = dict(self._explicit_batches.get(input_batch_id) or {})
        if terminal:
            await self._close_one_group_for_batch(input_batch_id)
            return {
                "status": "suppressed",
                "input_batch_id": input_batch_id,
                "duplicate": False,
                "run_skipped_duplicate": False,
                "response": "",
                "metadata": {
                    "input_collection_terminal_suppressed": True,
                    "terminal_action": terminal.get("action"),
                    "collection_id": terminal.get("collection_id"),
                    "progress_locale": progress_locale,
                },
            }
        if explicit:
            # One quiet callback owns one exact Telegram album. Releasing all
            # groups here would erase sibling albums that still need their own
            # sequencing callback. /send and /cancel remain the terminal paths
            # that close every group associated with the collection batch.
            await self._close_one_group_for_batch(input_batch_id)
            return {
                "status": "collecting",
                "input_batch_id": input_batch_id,
                "duplicate": False,
                "run_skipped_duplicate": False,
                "response": "",
                "metadata": {
                    "input_collection_pending": True,
                    "collection_id": explicit.get("collection_id"),
                    "file_count": explicit.get("file_count", 0),
                    "text_part_count": explicit.get("text_part_count", 0),
                    "progress_locale": progress_locale,
                },
            }
        return await super().commit_and_run(
            input_batch_id,
            session_id=session_id,
            progress_locale=progress_locale,
        )

    async def start_collection(
        self,
        *,
        session_id: str,
        chat_id: int | str,
        thread_id: int | str | None,
        principal_id: int | str,
        idempotency_key: str,
        locale: str,
        response_route: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._collection_control(
            "start",
            session_id=session_id,
            chat_id=chat_id,
            thread_id=thread_id,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
            extra={
                "locale": locale,
                "response_route": response_route,
            },
        )

    async def inspect_collection(
        self,
        *,
        session_id: str,
        chat_id: int | str,
        thread_id: int | str | None,
        principal_id: int | str,
    ) -> dict[str, Any]:
        return await self._collection_control(
            "inspect",
            session_id=session_id,
            chat_id=chat_id,
            thread_id=thread_id,
            principal_id=principal_id,
        )

    async def send_collection(
        self,
        *,
        session_id: str,
        chat_id: int | str,
        thread_id: int | str | None,
        principal_id: int | str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = await self._collection_control(
            "send",
            session_id=session_id,
            chat_id=chat_id,
            thread_id=thread_id,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
        )
        batch_id = str(payload.get("input_batch_id") or "").strip()
        if payload.get("status") == "committed" and batch_id:
            async with self._explicit_batch_lock:
                explicit = self._explicit_batches.pop(batch_id, None)
                terminal = self._remember_terminal_locked(
                    batch_id,
                    action="committed",
                    explicit=explicit,
                )
            previous_message_id = self._current_message_id(terminal)
            if previous_message_id is not None:
                payload["_telegram_previous_status_message_id"] = (
                    previous_message_id
                )
            await self._close_group_for_batch(batch_id)
        return payload

    async def cancel_collection(
        self,
        *,
        session_id: str,
        chat_id: int | str,
        thread_id: int | str | None,
        principal_id: int | str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = await self._collection_control(
            "cancel",
            session_id=session_id,
            chat_id=chat_id,
            thread_id=thread_id,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
        )
        batch_id = str(payload.get("input_batch_id") or "").strip()
        if batch_id:
            async with self._explicit_batch_lock:
                explicit = self._explicit_batches.pop(batch_id, None)
                terminal = self._remember_terminal_locked(
                    batch_id,
                    action="cancelled",
                    explicit=explicit,
                )
            previous_message_id = self._current_message_id(terminal)
            if previous_message_id is not None:
                payload["_telegram_previous_status_message_id"] = (
                    previous_message_id
                )
            await self._close_group_for_batch(batch_id)
        return payload

    async def relocate_input_presentation(
        self,
        presentation_ref: dict[str, Any],
        *,
        session_id: str,
        client_message_id: str,
    ) -> dict[str, Any]:
        presentation_id = str(presentation_ref.get("presentation_id") or "").strip()
        token = str(presentation_ref.get("presentation_token") or "").strip()
        generation = int(presentation_ref.get("presentation_generation") or 0)
        if not presentation_id or not token or generation < 1:
            raise RuntimeError("Presentation relocation reference is incomplete")
        async with self._client(read_timeout=30.0) as client:
            response = await client.post(
                f"{self.gateway_url}/internal/input-presentations/"
                f"{presentation_id}/relocate",
                json={
                    "session_id": session_id,
                    "presentation_token": token,
                    "client_message_id": str(client_message_id),
                    "expected_generation": generation,
                },
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Gateway presentation relocation response is invalid")

        batch_id = str(payload.get("input_batch_id") or "").strip()
        if batch_id:
            updated_ref = self._merge_presentation_ref(
                presentation_ref,
                {
                    "presentation_id": presentation_id,
                    "presentation_token": token,
                    "client_message_id": payload.get("client_message_id"),
                    "active_client_message_id": payload.get(
                        "client_message_id"
                    ),
                    "presentation_generation": payload.get(
                        "presentation_generation"
                    ),
                    "state": payload.get("state"),
                },
                client_message_id=str(client_message_id),
            )
            updated_ref["relocation_generation"] = None
            updated_ref["previous_client_message_id"] = None
            async with self._explicit_batch_lock:
                current = self._explicit_batches.get(batch_id)
                if current is not None:
                    current = dict(current)
                    current["presentation_ref"] = updated_ref
                    self._explicit_batches[batch_id] = current
        return payload

    async def record_input_presentation_deletion(
        self,
        presentation_ref: dict[str, Any],
        *,
        session_id: str,
        generation: int,
        deletion_state: str,
    ) -> dict[str, Any]:
        presentation_id = str(presentation_ref.get("presentation_id") or "").strip()
        token = str(presentation_ref.get("presentation_token") or "").strip()
        if not presentation_id or not token:
            raise RuntimeError("Presentation deletion receipt reference is incomplete")
        async with self._client(read_timeout=30.0) as client:
            response = await client.post(
                f"{self.gateway_url}/internal/input-presentations/"
                f"{presentation_id}/superseded-deletion",
                json={
                    "session_id": session_id,
                    "presentation_token": token,
                    "generation": int(generation),
                    "deletion_state": str(deletion_state),
                },
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Gateway presentation deletion response is invalid")
        return payload

    async def _collection_control(
        self,
        action: str,
        *,
        session_id: str,
        chat_id: int | str,
        thread_id: int | str | None,
        principal_id: int | str,
        idempotency_key: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "session_id": str(session_id),
            "client_type": "telegram",
            "client_instance_id": self.client_instance_id,
            "conversation_id": str(chat_id),
            "thread_id": str(thread_id) if thread_id is not None else None,
            "principal_id": str(principal_id),
        }
        if idempotency_key is not None:
            body["idempotency_key"] = str(idempotency_key)
        body.update(extra or {})
        async with self._client(read_timeout=30.0) as client:
            response = await client.post(
                f"{self.gateway_url}/internal/input-collections/{action}",
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Gateway input collection response is invalid")
        return payload

    @staticmethod
    def _chat_id_from_session(session_id: str) -> str | None:
        prefix = "telegram:conversation:"
        if not session_id.startswith(prefix):
            return None
        value = session_id[len(prefix):].split(":thread:", 1)[0].strip()
        return value or None
