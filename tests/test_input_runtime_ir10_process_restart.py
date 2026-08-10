from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def run_python(repo_root: Path, code: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_ir10_waiting_survives_two_independent_cold_restarts(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    durable_root = str(tmp_path).replace("\\", "\\\\")

    process_a = f'''
import asyncio, json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from src.input_runtime import CheckpointName, CycleStatus, InputAdmissionService, InputRuntimeConfigType, create_filesystem_input_runtime_repositories
from src.runtime import ActiveAgentCycle, SessionExecutionCoordinator
from src.storage import StorageConfigType
NOW=datetime(2026,8,9,tzinfo=timezone.utc)
@dataclass
class Batch:
    input_batch_id:str='initial'; session_id:str='session'; sequence_number:int=1; payload_size:int=10
    text_parts:list=field(default_factory=list); artifact_refs:list=field(default_factory=list)
    source_event_ids:tuple=('evt_'+'1'*32,); content_fingerprint:str='sha256:'+'2'*64; committed_at:object=NOW
    continuation_of_batch_id:object=None; correction_of_batch_id:object=None
    artifact_manifest:object=field(default_factory=lambda:SimpleNamespace(items=()))
    def model_dump_json(self): return 'x'*self.payload_size
class Reader:
    def __init__(self): self.batch=Batch()
    async def get_committed(self, input_batch_id): return self.batch
    async def list_committed_for_recovery(self): return (self.batch,)
async def main():
    repos=create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=r'{durable_root}'))
    reader=Reader(); coordinator=SessionExecutionCoordinator()
    service=InputAdmissionService(config=InputRuntimeConfigType(), repositories=repos, committed_batches=reader, wake_coordinator=coordinator, cycle_id_factory=lambda:'waiting-process-cycle', clock=lambda:NOW, payload_size_resolver=lambda b:b.payload_size)
    outcome=await service.admit_committed_batch('initial', session_id='session')
    active=ActiveAgentCycle(cycle_id=outcome.target_cycle_id, session_id='session', original_user_request='initial', messages_for_llm=[{{'role':'system','content':'system'}},{{'role':'user','content':'{{"type":"user_request"}}'}}], cycle_trace=[], original_user_message_index=1, original_input_batch_id='initial', input_runtime_generation=0)
    await service.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.RESUME, active_cycle=active, desired_status=CycleStatus.RUNNING)
    snapshot=await repos.snapshots.get(outcome.target_cycle_id)
    waiting=snapshot.model_copy(update={{'status':CycleStatus.WAITING_USER,'waiting_question':'Persistent process question?','safe_checkpoint':CheckpointName.BEFORE_WAITING,'snapshot_revision':snapshot.snapshot_revision+1,'updated_at':NOW}})
    await repos.snapshots.compare_and_swap(snapshot.snapshot_revision, waiting)
    state=await repos.sessions.get('session')
    await repos.sessions.compare_and_swap(state.revision, state.model_copy(update={{'cycle_status':CycleStatus.WAITING_USER,'revision':state.revision+1,'updated_at':NOW}}))
    print(json.dumps({{'cycle':outcome.target_cycle_id,'question':waiting.waiting_question}}))
asyncio.run(main())
'''
    first = run_python(repo_root, process_a)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout.strip().splitlines()[-1]) == {
        "cycle": "waiting-process-cycle",
        "question": "Persistent process question?",
    }

    recovery_script = f'''
import asyncio, json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from src.input_runtime import InputAdmissionService, InputRuntimeConfigType, create_filesystem_input_runtime_repositories
from src.input_runtime.recovery import InputRuntimeReadinessGate
from src.input_runtime.recovery_terminal import InputRuntimeRecoveryCoordinator
from src.runtime import SessionExecutionCoordinator
from src.storage import StorageConfigType
NOW=datetime(2026,8,9,tzinfo=timezone.utc)
@dataclass
class Batch:
    input_batch_id:str='initial'; session_id:str='session'; sequence_number:int=1; payload_size:int=10
    text_parts:list=field(default_factory=list); artifact_refs:list=field(default_factory=list)
    source_event_ids:tuple=('evt_'+'1'*32,); content_fingerprint:str='sha256:'+'2'*64; committed_at:object=NOW
    continuation_of_batch_id:object=None; correction_of_batch_id:object=None
    artifact_manifest:object=field(default_factory=lambda:SimpleNamespace(items=()))
    def model_dump_json(self): return 'x'*self.payload_size
class Reader:
    def __init__(self): self.batch=Batch()
    async def get_committed(self, input_batch_id): return self.batch
    async def list_committed_for_recovery(self): return (self.batch,)
async def main():
    repos=create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=r'{durable_root}'))
    reader=Reader(); coordinator=SessionExecutionCoordinator()
    service=InputAdmissionService(config=InputRuntimeConfigType(), repositories=repos, committed_batches=reader, wake_coordinator=coordinator, cycle_id_factory=lambda:'must-not-create', clock=lambda:NOW, payload_size_resolver=lambda b:b.payload_size)
    gate=InputRuntimeReadinessGate()
    recovery=InputRuntimeRecoveryCoordinator(repositories=repos, admission_service=service, committed_batches=reader, readiness_gate=gate, generation_coordinator=coordinator, clock=lambda:NOW)
    plan=await recovery.recover(); item=plan.sessions[0]
    admissions=await repos.admissions.list_for_session('session'); revisions=await repos.context_revisions.list_for_cycle(item.cycle_id)
    state=await repos.sessions.get('session')
    print(json.dumps({{'disposition':item.disposition.value,'cycle':item.cycle_id,'question':item.snapshot.waiting_question,'status':state.cycle_status.value,'admissions':len(admissions),'revisions':len(revisions)}}))
asyncio.run(main())
'''

    expected = {
        "disposition": "waiting",
        "cycle": "waiting-process-cycle",
        "question": "Persistent process question?",
        "status": "waiting_user",
        "admissions": 1,
        "revisions": 1,
    }
    second = run_python(repo_root, recovery_script)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout.strip().splitlines()[-1]) == expected

    # A third fresh interpreter proves startup reconciliation itself is
    # idempotent and does not rely on any cache/task/ContextVar from process B.
    third = run_python(repo_root, recovery_script)
    assert third.returncode == 0, third.stderr
    assert json.loads(third.stdout.strip().splitlines()[-1]) == expected
