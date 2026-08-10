from __future__ import annotations

import math
import random

import pytest

from src.input_runtime import CheckpointName, CycleStatus, InputAdmissionAction, InputRuntimeConfigType
from tests.test_input_runtime_ir10_release import (
    ACTIVE_SEEDS,
    Batch,
    Reader,
    input_updates,
    runtime,
    seed_cycle,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", ACTIVE_SEEDS)
async def test_ir10_seeded_exact_byte_capacity_boundary(seed, tmp_path):
    rng = random.Random(seed ^ 0xB17E)
    byte_limit = rng.choice((17, 31, 47, 64))
    batch_count = rng.randint(2, 5)

    remaining = byte_limit
    sizes: list[int] = []
    for index in range(batch_count - 1):
        minimum_left = batch_count - index - 1
        current = rng.randint(1, remaining - minimum_left)
        sizes.append(current)
        remaining -= current
    sizes.append(remaining)
    assert sum(sizes) == byte_limit

    initial = Batch("initial", "session", 1, payload_size=1)
    exact = [
        Batch(f"exact-{index}", "session", index + 2, payload_size=size)
        for index, size in enumerate(sizes)
    ]
    plus_one = Batch("plus-one", "session", batch_count + 2, payload_size=1)
    reader = Reader(initial, *exact, plus_one)
    checkpoint_batch_limit = rng.randint(1, batch_count)
    repos, _, _, service = runtime(
        tmp_path,
        reader,
        config=InputRuntimeConfigType(
            max_queued_batches_per_session=16,
            max_queued_bytes_per_session=byte_limit,
            max_batches_per_checkpoint=checkpoint_batch_limit,
            max_batch_bytes_per_checkpoint=byte_limit,
            min_intermediate_message_interval_seconds=0,
        ),
        cycle_prefix=f"capacity-bytes-{seed}",
    )
    _, cycle = await seed_cycle(service, initial)

    for batch in exact:
        outcome = await service.admit_committed_batch(
            batch.input_batch_id,
            session_id="session",
        )
        assert outcome.action == InputAdmissionAction.QUEUED_RUNNING

    duplicate = await service.admit_committed_batch(exact[0].input_batch_id, session_id="session")
    assert duplicate.action == InputAdmissionAction.DUPLICATE
    blocked = await service.admit_committed_batch("plus-one", session_id="session")
    assert blocked.action == InputAdmissionAction.CAPACITY_BLOCKED
    assert blocked.reason_code == "max_queued_bytes_per_session"
    assert await repos.admissions.get_by_input_batch_id("plus-one") is None

    applied = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=cycle,
        desired_status=CycleStatus.RUNNING,
    )
    assert applied.applied_input_batch_ids == tuple(batch.input_batch_id for batch in exact)
    assert len(input_updates(cycle)) == math.ceil(batch_count / checkpoint_batch_limit)


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", ACTIVE_SEEDS)
async def test_ir10_seeded_exact_batch_count_capacity_boundary(seed, tmp_path):
    rng = random.Random(seed ^ 0xC0A17)
    batch_limit = rng.randint(2, 6)
    initial = Batch("initial", "session", 1, payload_size=1)
    exact = [
        Batch(
            f"exact-{index}",
            "session",
            index + 2,
            payload_size=rng.randint(1, 4),
        )
        for index in range(batch_limit)
    ]
    plus_one = Batch("plus-one", "session", batch_limit + 2, payload_size=1)
    reader = Reader(initial, *exact, plus_one)
    repos, _, _, service = runtime(
        tmp_path,
        reader,
        config=InputRuntimeConfigType(
            max_queued_batches_per_session=batch_limit,
            max_queued_bytes_per_session=1024,
            max_batches_per_checkpoint=rng.randint(1, batch_limit),
            max_batch_bytes_per_checkpoint=1024,
            min_intermediate_message_interval_seconds=0,
        ),
        cycle_prefix=f"capacity-count-{seed}",
    )
    _, cycle = await seed_cycle(service, initial)

    for batch in exact:
        outcome = await service.admit_committed_batch(
            batch.input_batch_id,
            session_id="session",
        )
        assert outcome.action == InputAdmissionAction.QUEUED_RUNNING

    duplicate = await service.admit_committed_batch(exact[-1].input_batch_id, session_id="session")
    assert duplicate.action == InputAdmissionAction.DUPLICATE
    blocked = await service.admit_committed_batch("plus-one", session_id="session")
    assert blocked.action == InputAdmissionAction.CAPACITY_BLOCKED
    assert blocked.reason_code == "max_queued_batches_per_session"
    assert await repos.admissions.get_by_input_batch_id("plus-one") is None

    applied = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=cycle,
        desired_status=CycleStatus.RUNNING,
    )
    assert applied.applied_input_batch_ids == tuple(batch.input_batch_id for batch in exact)
