import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from src.input_runtime.emissions import AgentEmissionDeliveryReceipt, ReplyAwareCommittedBatchReader
from src.input_runtime.factory import create_filesystem_input_runtime_repositories
from src.input_runtime.models import AgentEmission, CycleStatus, SessionInputRuntimeState, new_context_revision_id
from src.input_runtime.projection import project_committed_batch
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 8, 15, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


class Reader:
    def __init__(self, batch):
        self.batch = batch

    async def get_committed(self, input_batch_id):
        assert input_batch_id == self.batch.input_batch_id
        return self.batch


def setup(tmp_path):
    repos = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    revision = new_context_revision_id()
    state = SessionInputRuntimeState(
        session_id="telegram:conversation:100:thread:7",
        generation=0,
        active_cycle_id="cycle",
        cycle_status=CycleStatus.RUNNING,
        active_context_revision_id=revision,
        created_at=NOW,
        updated_at=NOW,
    )
    run(repos.sessions.create_if_absent(state))
    emission = AgentEmission(
        session_id=state.session_id,
        cycle_id="cycle",
        generation=0,
        context_revision_id=revision,
        kind="intermediate",
        text="intermediate",
        response_route={
            "client_type": "telegram",
            "client_instance_id": "bot-a",
            "conversation_id": "100",
            "thread_id": "7",
            "reply_to_message_id": "55",
            "capability_snapshot_id": "caps",
        },
        idempotency_key="reply-key",
        created_at=NOW,
    )
    accepted = run(repos.emissions.accept_intermediate(
        emission,
        max_messages=10,
        min_interval_seconds=0,
    )).emission
    claimed = run(repos.emissions.claim_for_client(
        accepted.emission_id,
        session_id=accepted.session_id,
        client_type="telegram",
        client_instance_id="bot-a",
        claim_token="claim",
        claimed_at=NOW,
        lease_seconds=30,
    ))
    run(repos.emissions.record_delivery_receipt(AgentEmissionDeliveryReceipt(
        emission_id=claimed.emission_id,
        session_id=claimed.session_id,
        cycle_id=claimed.cycle_id,
        generation=claimed.generation,
        claim_token="claim",
        attempt_number=1,
        client_type="telegram",
        client_instance_id="bot-a",
        conversation_id="100",
        thread_id="7",
        external_message_id="900",
        delivered_at=NOW,
    )))
    return repos, claimed


def input_batch(*, session="telegram:conversation:100:thread:7", conversation="100", thread="7", replied_to="900"):
    return SimpleNamespace(
        input_batch_id="input-2",
        session_id=session,
        text_parts=(SimpleNamespace(
            part_id="part-1",
            kind="text",
            text="answering the intermediate",
            attachment_slot_ids=(),
        ),),
        artifact_manifest=None,
        artifact_refs=(),
        continuation_of_batch_id=None,
        correction_of_batch_id=None,
        response_route=SimpleNamespace(
            conversation_id=conversation,
            thread_id=thread,
        ),
        capability_snapshot=SimpleNamespace(
            client_type="telegram",
            client_instance_id="bot-a",
        ),
        reply_contexts=(SimpleNamespace(replied_to_message_id=replied_to),),
    )


def test_exact_external_reply_maps_to_internal_emission_relation(tmp_path):
    repos, emission = setup(tmp_path)
    batch = input_batch()
    wrapped = run(ReplyAwareCommittedBatchReader(Reader(batch), repos.emissions).get_committed("input-2"))
    assert wrapped.reply_to_emission == {
        "emission_id": emission.emission_id,
        "kind": "intermediate",
    }


def test_reply_marker_enters_projection_without_changing_fifo_sequence(tmp_path):
    repos, emission = setup(tmp_path)
    batch = input_batch()
    wrapped = run(ReplyAwareCommittedBatchReader(Reader(batch), repos.emissions).get_committed("input-2"))
    projected = project_committed_batch(wrapped, cycle_sequence=4)
    assert projected["cycle_sequence"] == 4
    assert projected["reply_to"] == {
        "emission_id": emission.emission_id,
        "kind": "intermediate",
    }
    assert "branch" not in projected


def test_cross_session_same_numeric_message_id_does_not_bind(tmp_path):
    repos, _ = setup(tmp_path)
    batch = input_batch(session="telegram:conversation:other:thread:7")
    wrapped = run(ReplyAwareCommittedBatchReader(Reader(batch), repos.emissions).get_committed("input-2"))
    assert wrapped.reply_to_emission is None


def test_cross_conversation_same_numeric_message_id_does_not_bind(tmp_path):
    repos, _ = setup(tmp_path)
    batch = input_batch(conversation="101")
    wrapped = run(ReplyAwareCommittedBatchReader(Reader(batch), repos.emissions).get_committed("input-2"))
    assert wrapped.reply_to_emission is None


def test_cross_thread_same_numeric_message_id_does_not_bind(tmp_path):
    repos, _ = setup(tmp_path)
    batch = input_batch(thread="8")
    wrapped = run(ReplyAwareCommittedBatchReader(Reader(batch), repos.emissions).get_committed("input-2"))
    assert wrapped.reply_to_emission is None


def test_missing_reply_binding_leaves_ordinary_input_unchanged(tmp_path):
    repos, _ = setup(tmp_path)
    batch = input_batch(replied_to="901")
    wrapped = run(ReplyAwareCommittedBatchReader(Reader(batch), repos.emissions).get_committed("input-2"))
    projected = project_committed_batch(wrapped, cycle_sequence=5)
    assert "reply_to" not in projected
    assert projected["text_parts"][0]["text"] == "answering the intermediate"
