"""IR-6 outbox facade with exact immutable-route failure fencing."""

from __future__ import annotations

from .emissions import AgentEmissionOutboxService as _BaseOutboxService
from .models import AgentEmission, EmissionState


class AgentEmissionOutboxService(_BaseOutboxService):
    async def failed(
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
        error_code: str,
        ambiguous: bool,
    ) -> AgentEmission:
        return await self.repository.fail_for_route(
            emission_id,
            session_id=session_id,
            cycle_id=cycle_id,
            generation=generation,
            client_type=client_type.strip().lower(),
            client_instance_id=client_instance_id.strip(),
            conversation_id=conversation_id.strip(),
            thread_id=(thread_id or "").strip() or None,
            claim_token=claim_token,
            state=(
                EmissionState.UNKNOWN.value
                if ambiguous
                else EmissionState.FAILED.value
            ),
            error_code=error_code,
        )
