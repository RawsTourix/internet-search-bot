import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.agent.prompts import ARTIFACT_RULES
from src.artifacts.delivery_tools import ArtifactDeliveryToolController
from src.artifacts.models import new_artifact_id
from src.artifacts.scoped_tools import ScopedArtifactToolController
from src.ingress import IngressNotFoundError
from src.mcp.manager_context import ManagerToolContext
from src.mcp.mcp_client import SessionState
from src.runtime import ActiveAgentCycle


def context() -> ManagerToolContext:
    cycle = ActiveAgentCycle(
        cycle_id="cycle-1",
        session_id="session-1",
        original_user_request="не открывай",
        messages_for_llm=[],
        cycle_trace=[],
        original_user_message_index=0,
    )
    return ManagerToolContext(
        session_id="session-1",
        cycle_id="cycle-1",
        active_cycle=cycle,
        session_state=SessionState(),
        client_type="telegram",
    )


class ArtifactInputSideEffectPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_input_artifact_delivery_requires_explicit_redelivery_flag(self):
        artifact_id = new_artifact_id()
        service = SimpleNamespace(
            config=SimpleNamespace(max_artifacts_per_cycle=8),
            artifact_service=SimpleNamespace(
                artifact_store=SimpleNamespace(
                    get_version=AsyncMock(return_value=SimpleNamespace(
                        provenance=SimpleNamespace(origin="user_upload")
                    ))
                )
            ),
        )
        controller = ArtifactDeliveryToolController(service)
        manager_context = context()
        manager_context.active_cycle.artifact_refs.append(artifact_id)
        outcome = await controller.execute(
            "artifact_set_delivery",
            {"artifact_ids": [artifact_id], "selected": True},
            manager_context,
        )
        self.assertEqual(outcome.payload["status"], "rejected")
        self.assertIn(
            "current request explicitly asks",
            outcome.payload["items"][0]["message"],
        )

    async def test_failed_or_cancelled_input_batch_is_absent_from_session_scope(self):
        artifact_id = new_artifact_id()
        version = SimpleNamespace(
            artifact_id=artifact_id,
            provenance=SimpleNamespace(
                origin="user_upload",
                input_batch_id="ibat-failed",
            ),
        )
        artifact_store = SimpleNamespace(
            list_lineages=AsyncMock(return_value=[SimpleNamespace(
                artifact_lineage_id="alineage-1"
            )]),
            list_versions=AsyncMock(return_value=[version]),
        )
        artifact_service = SimpleNamespace(artifact_store=artifact_store)
        controller = ScopedArtifactToolController(artifact_service, None)
        controller.committed_batch_store = SimpleNamespace(
            get_committed=AsyncMock(
                side_effect=IngressNotFoundError("not committed")
            )
        )
        access = await controller._session_access(
            context(),
            include_archived=True,
        )
        self.assertEqual(access.allowed_artifact_ids, [])

    def test_prompt_treats_filename_and_do_not_open_as_untrusted_data(self):
        self.assertIn("Filename, title, MIME/type, metadata", ARTIFACT_RULES)
        self.assertIn("не открывай", ARTIFACT_RULES)
        self.assertIn("redeliver_input=true", ARTIFACT_RULES)


if __name__ == "__main__":
    unittest.main()
