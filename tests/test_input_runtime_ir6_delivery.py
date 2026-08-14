import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.input_runtime.emissions import AgentEmissionDeliveryReceipt
from src.input_runtime.errors import InputRuntimeConflictError
from src.input_runtime.factory import create_filesystem_input_runtime_repositories
from src.input_runtime.models import (
    AgentEmission,
    CycleStatus,
    EmissionState,
    SessionInputRuntimeState,
    new_context_revision_id,
)
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 8, 13, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def repositories(tmp_path):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )


def setup_ready(tmp_path, *, session="session", cycle="cycle", generation=0, instance="bot-a", conversation="100", thread="7"):
    repos = repositories(tmp_path)
    revision = new_context_revision_id()
    state = SessionInputRuntimeState(
        session_id=session,
        generation=generation,
        active_cycle_id=cycle,
        cycle_status=CycleStatus.RUNNING,
        active_context_revision_id=revision,
        created_at=NOW,
        updated_at=NOW,
    )
    run(repos.sessions.create_if_absent(state))
    emission = AgentEmission(
        session_id=session,
        cycle_id=cycle,
        generation=generation,
        context_revision_id=revision,
        kind="intermediate",
        text="semantic message",
        visibility="user",
        importance="normal",
        response_route={
            "client_type": "telegram",
            "client_instance_id": instance,
            "conversation_id": conversation,
            "thread_id": thread,
            "reply_to_message_id": "55",
            "capability_snapshot_id": "caps-1",
        },
        idempotency_key=f"key-{session}-{cycle}",
        created_at=NOW,
    )
    accepted = run(repos.emissions.accept_intermediate(
        emission,
        max_messages=10,
        min_interval_seconds=0,
    ))
    assert accepted.accepted
    return repos, state, accepted.emission


def receipt(emission, *, token="claim-a", external="900", delivered_at=None):
    return AgentEmissionDeliveryReceipt(
        emission_id=emission.emission_id,
        session_id=emission.session_id,
        cycle_id=emission.cycle_id,
        generation=emission.generation,
        claim_token=token,
        attempt_number=emission.delivery_attempt_count,
        client_type="telegram",
        client_instance_id=str(emission.response_route["client_instance_id"]),
        conversation_id=str(emission.response_route["conversation_id"]),
        thread_id=str(emission.response_route["thread_id"]),
        external_message_id=external,
        delivered_at=delivered_at or NOW,
    )


def claim(repos, emission, token="claim-a", at=NOW):
    return run(repos.emissions.claim_for_client(
        emission.emission_id,
        session_id=emission.session_id,
        client_type="telegram",
        client_instance_id=str(emission.response_route["client_instance_id"]),
        claim_token=token,
        claimed_at=at,
        lease_seconds=30,
    ))


def test_duplicate_same_claim_token_is_idempotent(tmp_path):
    repos, _, ready = setup_ready(tmp_path)
    first = claim(repos, ready)
    second = claim(repositories(tmp_path), ready)
    assert first == second
    assert second.state == EmissionState.DELIVERING
    assert second.delivery_attempt_count == 1


def test_competing_claim_token_is_rejected(tmp_path):
    repos, _, ready = setup_ready(tmp_path)
    claim(repos, ready, "claim-a")
    with pytest.raises(InputRuntimeConflictError):
        claim(repositories(tmp_path), ready, "claim-b")


def test_lost_claim_response_retry_does_not_create_second_attempt(tmp_path):
    repos, _, ready = setup_ready(tmp_path)
    claimed = claim(repos, ready, "stable-token")
    recreated = repositories(tmp_path)
    retried = claim(recreated, ready, "stable-token")
    assert retried.delivery_claim_token == "stable-token"
    assert retried.delivery_attempt_count == claimed.delivery_attempt_count == 1


def test_successful_receipt_is_durable_delivered(tmp_path):
    repos, _, ready = setup_ready(tmp_path)
    claimed = claim(repos, ready)
    delivered = run(repos.emissions.record_delivery_receipt(receipt(claimed)))
    assert delivered.state == EmissionState.DELIVERED
    assert delivered.delivered_at == NOW
    assert delivered.delivery_claim_token is None


def test_duplicate_same_receipt_is_idempotent_after_recreation(tmp_path):
    repos, _, ready = setup_ready(tmp_path)
    claimed = claim(repos, ready)
    evidence = receipt(claimed)
    first = run(repos.emissions.record_delivery_receipt(evidence))
    second = run(repositories(tmp_path).emissions.record_delivery_receipt(evidence))
    assert first == second
    assert second.state == EmissionState.DELIVERED


def test_changed_duplicate_receipt_is_conflict(tmp_path):
    repos, _, ready = setup_ready(tmp_path)
    claimed = claim(repos, ready)
    run(repos.emissions.record_delivery_receipt(receipt(claimed, external="900")))
    with pytest.raises(InputRuntimeConflictError):
        run(repositories(tmp_path).emissions.record_delivery_receipt(receipt(claimed, external="901")))


def test_deterministic_failure_is_failed_not_requeued(tmp_path):
    repos, _, ready = setup_ready(tmp_path)
    claimed = claim(repos, ready)
    failed = run(repos.emissions.fail_for_client(
        claimed.emission_id,
        session_id=claimed.session_id,
        client_type="telegram",
        client_instance_id="bot-a",
        claim_token="claim-a",
        state="failed",
        error_code="telegram_bad_request",
    ))
    assert failed.state == EmissionState.FAILED
    assert run(repositories(tmp_path).emissions.list_pending_delivery()) == ()


def test_ambiguous_failure_is_unknown_not_requeued(tmp_path):
    repos, _, ready = setup_ready(tmp_path)
    claimed = claim(repos, ready)
    unknown = run(repos.emissions.fail_for_client(
        claimed.emission_id,
        session_id=claimed.session_id,
        client_type="telegram",
        client_instance_id="bot-a",
        claim_token="claim-a",
        state="unknown",
        error_code="telegram_timeout",
    ))
    assert unknown.state == EmissionState.UNKNOWN
    assert run(repositories(tmp_path).emissions.list_pending_delivery()) == ()


def test_wrong_client_instance_cannot_claim(tmp_path):
    repos, _, ready = setup_ready(tmp_path)
    with pytest.raises(PermissionError):
        run(repos.emissions.claim_for_client(
            ready.emission_id,
            session_id=ready.session_id,
            client_type="telegram",
            client_instance_id="bot-b",
            claim_token="claim",
            claimed_at=NOW,
            lease_seconds=30,
        ))


def test_wrong_session_cannot_fail_claim(tmp_path):
    repos, _, ready = setup_ready(tmp_path)
    claimed = claim(repos, ready)
    with pytest.raises(PermissionError):
        run(repos.emissions.fail_for_client(
            claimed.emission_id,
            session_id="other-session",
            client_type="telegram",
            client_instance_id="bot-a",
            claim_token="claim-a",
            state="failed",
            error_code="rejected",
        ))


def test_expired_claim_becomes_unknown_not_ready(tmp_path):
    repos, _, ready = setup_ready(tmp_path)
    claim(repos, ready, at=NOW)
    changed = run(repositories(tmp_path).emissions.recover_expired_delivery_claims(
        now=NOW + timedelta(seconds=31)
    ))
    assert len(changed) == 1
    assert changed[0].state == EmissionState.UNKNOWN
    assert run(repositories(tmp_path).emissions.list_pending_delivery()) == ()


def test_same_token_after_expiry_cannot_restart_delivery(tmp_path):
    repos, _, ready = setup_ready(tmp_path)
    claim(repos, ready, at=NOW)
    with pytest.raises(InputRuntimeConflictError):
        claim(repositories(tmp_path), ready, "claim-a", at=NOW + timedelta(seconds=31))
    current = run(repositories(tmp_path).emissions.get_by_idempotency_key(
        ready.cycle_id, ready.idempotency_key
    ))
    assert current.state == EmissionState.UNKNOWN


def test_ready_survives_repository_recreation(tmp_path):
    _, _, ready = setup_ready(tmp_path)
    pending = run(repositories(tmp_path).emissions.list_ready_for_client(
        client_type="telegram",
        client_instance_id="bot-a",
        limit=10,
        now=NOW,
    ))
    assert [item.emission_id for item in pending] == [ready.emission_id]


def test_ready_reset_is_cancelled_and_not_listed(tmp_path):
    repos, _, ready = setup_ready(tmp_path)
    changed = run(repos.emissions.cancel_generation(
        ready.session_id,
        generation=ready.generation,
        reason_code="reset",
    ))
    assert changed[0].state == EmissionState.CANCELLED
    assert run(repositories(tmp_path).emissions.list_ready_for_client(
        client_type="telegram",
        client_instance_id="bot-a",
        limit=10,
        now=NOW,
    )) == ()


def test_reset_during_delivering_is_unknown_and_fences_stale_writer(tmp_path):
    repos, _, ready = setup_ready(tmp_path)
    claimed = claim(repos, ready)
    changed = run(repos.emissions.cancel_generation(
        claimed.session_id,
        generation=claimed.generation,
        reason_code="reset",
    ))
    assert changed[0].state == EmissionState.UNKNOWN
    assert changed[0].error_code == "reset_during_delivery"
    with pytest.raises(InputRuntimeConflictError):
        run(repositories(tmp_path).emissions.record_delivery_receipt(receipt(claimed)))


def test_terminal_before_ready_claim_cancels_without_transport_attempt(tmp_path):
    repos, state, ready = setup_ready(tmp_path)
    terminal = state.model_copy(update={
        "cycle_status": CycleStatus.DONE,
        "revision": state.revision + 1,
        "updated_at": NOW + timedelta(seconds=1),
    })
    run(repos.sessions.compare_and_swap(state.revision, terminal))
    with pytest.raises(InputRuntimeConflictError):
        claim(repositories(tmp_path), ready)
    current = run(repositories(tmp_path).emissions.get_by_idempotency_key(
        ready.cycle_id, ready.idempotency_key
    ))
    assert current.state == EmissionState.CANCELLED
    assert current.delivery_attempt_count == 0


def test_reply_resolution_requires_exact_delivery_scope(tmp_path):
    repos, _, ready = setup_ready(tmp_path)
    claimed = claim(repos, ready)
    run(repos.emissions.record_delivery_receipt(receipt(claimed, external="900")))
    exact = run(repositories(tmp_path).emissions.resolve_delivered_reply(
        session_id="session",
        client_type="telegram",
        client_instance_id="bot-a",
        conversation_id="100",
        thread_id="7",
        external_message_id="900",
    ))
    assert exact is not None and exact.emission_id == ready.emission_id
    assert run(repositories(tmp_path).emissions.resolve_delivered_reply(
        session_id="other-session",
        client_type="telegram",
        client_instance_id="bot-a",
        conversation_id="100",
        thread_id="7",
        external_message_id="900",
    )) is None
    assert run(repositories(tmp_path).emissions.resolve_delivered_reply(
        session_id="session",
        client_type="telegram",
        client_instance_id="bot-a",
        conversation_id="101",
        thread_id="7",
        external_message_id="900",
    )) is None
