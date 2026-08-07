"""Exact worker-authority fence layered over the IR-6 filesystem repository."""

from __future__ import annotations

from .errors import InputRuntimeConflictError
from .ir6_filesystem import FileSystemAgentEmissionRepository as _IR6Repository
from .models import EmissionState


class FileSystemAgentEmissionRepository(_IR6Repository):
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
        next_state = EmissionState(state)
        if next_state not in {EmissionState.FAILED, EmissionState.UNKNOWN}:
            raise ValueError("delivery failure state must be failed or unknown")
        stale = await self._find(emission_id)
        self._validate_worker_authority(
            stale,
            session_id=session_id,
            client_type=client_type,
            client_instance_id=client_instance_id,
        )
        async with self.locks.hold(self.root, stale.session_id):
            current = await self._find(emission_id)
            self._validate_worker_authority(
                current,
                session_id=session_id,
                client_type=client_type,
                client_instance_id=client_instance_id,
            )
            if current.state == next_state and current.error_code == error_code:
                return current
            if (
                current.state != EmissionState.DELIVERING
                or current.delivery_claim_token != claim_token.strip()
            ):
                raise InputRuntimeConflictError("stale emission delivery claim")
            # Reuse the parent transition only after exact authority has been
            # revalidated under the same session coordination boundary.
            # Parent would reacquire the same non-reentrant lock, so perform the
            # validated write through its small transition primitives here.
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
        if not client_type or not client_instance_id:
            raise InputRuntimeConflictError(
                "IR-6 delivery failure requires stored client authority"
            )
        return await self.fail_for_client(
            emission_id,
            session_id=emission.session_id,
            client_type=client_type,
            client_instance_id=client_instance_id,
            claim_token=claim_token,
            state=state,
            error_code=error_code,
        )
