import asyncio
import tempfile
import unittest
from pathlib import Path

from src.artifacts import (
    ArtifactCandidate,
    ArtifactCandidateError,
    ArtifactCandidateStatus,
    ArtifactNotFoundError,
    ArtifactStorageError,
    FileSystemArtifactCandidateStore,
    new_artifact_candidate_id,
    new_artifact_id,
    utc_now,
)
from src.storage import StorageConfigType
from src.storage.models import new_content_id


_HASH = "sha256:" + "a" * 64


def make_candidate(
    *,
    session_id: str = "session-1",
    cycle_id: str = "cycle-1",
) -> ArtifactCandidate:
    return ArtifactCandidate(
        candidate_id=new_artifact_candidate_id(),
        session_id=session_id,
        cycle_id=cycle_id,
        content_id=new_content_id(),
        suggested_filename="result.docx",
        format_id="docx",
        mime_type=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        size_bytes=123,
        content_hash=_HASH,
        source_tool_call_id="tool-call-1",
        source_tool_name="word_processor",
        source_artifact_ids=[],
        status=ArtifactCandidateStatus.AVAILABLE,
        created_at=utc_now(),
        metadata={"safe": True},
    )


class ArtifactCandidateStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config = StorageConfigType(
            root_dir=str(Path(self.temporary.name) / "storage")
        )
        self.store = FileSystemArtifactCandidateStore(self.config)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_create_get_and_idempotent_duplicate(self):
        candidate = make_candidate()
        created = await self.store.create(candidate)
        duplicate = await self.store.create(candidate)
        loaded = await self.store.get(candidate.candidate_id)

        self.assertEqual(created, candidate)
        self.assertEqual(duplicate, candidate)
        self.assertEqual(loaded, candidate)

        conflicting = candidate.model_copy(update={"size_bytes": 124})
        with self.assertRaises(ArtifactCandidateError):
            await self.store.create(conflicting)

    async def test_cycle_listing_is_scoped_and_deterministic(self):
        first = make_candidate()
        second = make_candidate()
        foreign_cycle = make_candidate(cycle_id="cycle-2")
        foreign_session = make_candidate(session_id="session-2")
        for candidate in (second, foreign_cycle, first, foreign_session):
            await self.store.create(candidate)

        listed = await self.store.list_cycle(
            session_id="session-1",
            cycle_id="cycle-1",
        )
        self.assertEqual(
            {item.candidate_id for item in listed},
            {first.candidate_id, second.candidate_id},
        )
        self.assertEqual(
            listed,
            sorted(listed, key=lambda item: (item.created_at, item.candidate_id)),
        )

    async def test_terminal_transitions_are_idempotent_and_filtered(self):
        promoted = make_candidate()
        discarded = make_candidate()
        await self.store.create(promoted)
        await self.store.create(discarded)

        artifact_id = new_artifact_id()
        first = await self.store.mark_promoted(
            promoted.candidate_id,
            artifact_id=artifact_id,
        )
        second = await self.store.mark_promoted(
            promoted.candidate_id,
            artifact_id=artifact_id,
        )
        self.assertEqual(first.status, ArtifactCandidateStatus.PROMOTED)
        self.assertEqual(second.promoted_artifact_id, artifact_id)

        terminal = await self.store.mark_discarded(discarded.candidate_id)
        repeated = await self.store.mark_discarded(discarded.candidate_id)
        self.assertEqual(terminal.status, ArtifactCandidateStatus.DISCARDED)
        self.assertEqual(repeated, terminal)

        available = await self.store.list_cycle(
            session_id="session-1",
            cycle_id="cycle-1",
        )
        self.assertEqual(available, [])
        all_items = await self.store.list_cycle(
            session_id="session-1",
            cycle_id="cycle-1",
            include_terminal=True,
        )
        self.assertEqual(len(all_items), 2)

    async def test_competing_terminal_transitions_allow_only_one(self):
        candidate = make_candidate()
        await self.store.create(candidate)
        artifact_a = new_artifact_id()
        artifact_b = new_artifact_id()

        results = await asyncio.gather(
            self.store.mark_promoted(
                candidate.candidate_id,
                artifact_id=artifact_a,
            ),
            self.store.mark_promoted(
                candidate.candidate_id,
                artifact_id=artifact_b,
            ),
            return_exceptions=True,
        )

        successes = [item for item in results if isinstance(item, ArtifactCandidate)]
        failures = [item for item in results if isinstance(item, Exception)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ArtifactCandidateError)
        loaded = await self.store.get(candidate.candidate_id)
        self.assertEqual(loaded.status, ArtifactCandidateStatus.PROMOTED)
        self.assertIn(loaded.promoted_artifact_id, {artifact_a, artifact_b})

    async def test_unknown_invalid_and_symlink_paths_are_managed(self):
        with self.assertRaises(ArtifactCandidateError):
            await self.store.get("../candidate")
        with self.assertRaises(ArtifactNotFoundError):
            await self.store.get(new_artifact_candidate_id())

        candidate_id = new_artifact_candidate_id()
        target = Path(self.temporary.name) / "outside"
        target.mkdir()
        link = self.store.root / candidate_id
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        with self.assertRaises(ArtifactStorageError):
            await self.store.get(candidate_id)


if __name__ == "__main__":
    unittest.main()
