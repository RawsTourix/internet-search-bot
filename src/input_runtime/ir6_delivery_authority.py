"""Exact worker-authority fence layered over the IR-6 filesystem repository."""

from __future__ import annotations

from .errors import InputRuntimeConflictError
from .ir6_filesystem import FileSystemAgentEmissionRepository as _IR6Repository
from .models import EmissionState


class FileSystemAgentEmissionRepository(_IR6Repository):
    def _validate_exact_delivery_route(
        self,
        emission,
        *,
        session_id: str,
        cycle_id: str,
        generation: int,
        client_type: str,
        client_instance_id: str,
        conversation_id: str,
        thread_id: str | None,
    ) -> None:
        self._validate_worker_authority(
            emission,
            session_id=session_id,
            client_type=client_type,
            client_instance_id=client_instance_id,
        )
        normalized_thread = (thread_id or "").strip() or None
        if emission.cycle_id != cycle_id.strip():
            raise PermissionError("emission belongs to another cycle")
        if emission.generation != generation:
            raise PermissionError("emission belongs to another generation")
        if self._route_value(emission, "conversation_id") != conversation_id.strip():
            raise PermissionError("emission belongs to another conversation")
        if self._route_value(emission, "thread_id") != normalized_thread:
            raise PermissionError("emission belongs to another thread")

    async def fail_for_route(
        self,
        emission_id: str,
        *,
        session_id: str,
        cycle_id: str,
        generation: int,
        client_type: str,
        client_instance_id: str,
        conversation_id: str,
        thread_id: str | None,
        claim_token: str,
        state: str,
        error_code: str,
    ):
        next_state = EmissionState(state)
        if next_state not in {EmissionState.FAILED, EmissionState.UNKNOWN}:
            raise ValueError("delivery failure state must be failed or unknown")
        stale = await self._find(emission_id)
        self._validate_exact_delivery_route(
            stale,
            session_id=session_id,
            cycle_id=cycle_id,
            generation=generation,
            client_type=client_type,
            client_instance_id=client_instance_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
        )
        async with self.locks.hold(self.root, stale.session_id):
            current = await self._find(emission_id)
            self._validate_exact_delivery_route(
                current,
                session_id=session_id,
                cycle_id=cycle_id,
                generation=generation,
                client_type=client_type,
                client_instance_id=client_instance_id,
                conversation_id=conversation_id,
                thread_id=thread_id,
            )
            if current.state == next_state and current.error_code == error_code:
                return current
            if (
                current.state != EmissionState.DELIVERING
                or current.delivery_claim_token != claim_token.strip()
            ):
                raise InputRuntimeConflictError("stale emission delivery claim")
            from ._filesystem_common import validated_copy
            from .serialization import atomic_write_model

            failed = validated_copy(
                current,
                state=next_state,
                error_code=error_code.strip() or "delivery_failed",
                delivery_claim_token=None,
                delivery_claimed_at=None,
                delivery_claim_expires_at=None,
            )
            atomic_write_model(
                self.layout.emission(current.cycle_id, current.emission_id),
                failed,
            )
            return failed

    async def fail_for_client(
        self,
        emission_id: str,
        *,
        session_id: str,
        client_type: str,
        client_instance_id: str,
        claim_token: str,
        state: str,
        error_code: str,
    ):
        emission = await self._find(emission_id)
        conversation_id = self._route_value(emission, "conversation_id") or ""
        if not conversation_id:
            raise InputRuntimeConflictError(
                "IR-6 delivery failure requires stored conversation authority"
            )
        return await self.fail_for_route(
            emission_id,
            session_id=session_id,
            cycle_id=emission.cycle_id,
            generation=emission.generation,
            client_type=client_type,
            client_instance_id=client_instance_id,
            conversation_id=conversation_id,
            thread_id=self._route_value(emission, "thread_id"),
            claim_token=claim_token,
            state=state,
            error_code=error_code,
        )

    async def fail_delivery(
        self,
        emission_id: str,
        *,
        claim_token: str,
        state: str,
        error_code: str,
    ):
        emission = await self._find(emission_id)
        client_type = self._route_value(emission, "client_type") or ""
        client_instance_id = self._route_value(emission, "client_instance_id") or ""
        conversation_id = self._route_value(emission, "conversation_id") or ""
        if not client_type or not client_instance_id or not conversation_id:
            raise InputRuntimeConflictError(
                "IR-6 delivery failure requires stored client authority"
            )
        return await self.fail_for_route(
            emission_id,
            session_id=emission.session_id,
            cycle_id=emission.cycle_id,
            generation=emission.generation,
            client_type=client_type,
            client_instance_id=client_instance_id,
            conversation_id=conversation_id,
            thread_id=self._route_value(emission, "thread_id"),
            claim_token=claim_token,
            state=state,
            error_code=error_code,
        )
