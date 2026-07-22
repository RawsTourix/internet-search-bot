import os
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from src.artifacts import (
    ArtifactAccessContext,
    ArtifactConfigType,
    ArtifactLimitError,
    ArtifactProvenance,
    ArtifactPurpose,
    ArtifactValidationError,
    ArtifactWorkspaceError,
    create_artifact_services,
)
from src.artifacts.workspace import (
    ArtifactInputBinding,
    ArtifactOutputSpec,
    ArtifactWorkspaceManager,
)
from src.storage import StorageConfigType, create_storage_services


class ArtifactWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(
            root_dir=str(self.root / "storage")
        )
        self.artifact_config = ArtifactConfigType(
            max_artifact_size_bytes=1024 * 1024,
            max_patchable_text_bytes=1024 * 1024,
            max_workspace_bytes=2 * 1024 * 1024,
        )
        self.storage = create_storage_services(self.storage_config)
        self.services = create_artifact_services(
            storage_config=self.storage_config,
            artifact_config=self.artifact_config,
            content_store=self.storage.content_store,
        )
        self.manager = ArtifactWorkspaceManager(
            storage_config=self.storage_config,
            artifact_config=self.artifact_config,
            artifact_service=self.services.artifact_service,
            content_store=self.storage.content_store,
            candidate_store=self.services.candidate_store,
            format_registry=self.services.format_registry,
        )
        self.provenance = ArtifactProvenance(
            origin="agent_created",
            creator="agent",
            operation="workspace_test",
        )
        self.artifact = await self.services.artifact_service.create_text(
            session_id="session-1",
            cycle_id="cycle-1",
            filename="input.md",
            text="alpha beta",
            format_id="markdown",
            provenance=self.provenance,
            purpose=ArtifactPurpose.WORKING,
        )
        self.access = ArtifactAccessContext(
            session_id="session-1",
            cycle_id="cycle-1",
            allowed_artifact_ids=[self.artifact.artifact_id],
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_prepare_materializes_exact_read_only_input_without_mutating_args(self):
        original = {"options": {"mode": "edit"}}
        workspace = await self.manager.prepare(
            access=self.access,
            tool_call_id="call-1",
            arguments=original,
            bindings=[
                ArtifactInputBinding(
                    artifact_id=self.artifact.artifact_id,
                    argument_pointer="/options/input_file",
                )
            ],
            outputs=[],
        )
        try:
            self.assertEqual(original, {"options": {"mode": "edit"}})
            injected = Path(workspace.arguments["options"]["input_file"])
            self.assertTrue(injected.is_file())
            self.assertEqual(injected.read_text(encoding="utf-8"), "alpha beta")
            self.assertEqual(workspace.source_artifact_ids, (self.artifact.artifact_id,))
            self.assertEqual(workspace.input_bytes, len(b"alpha beta"))
            if os.name != "nt":
                self.assertEqual(injected.stat().st_mode & 0o222, 0)
        finally:
            await self.manager.cleanup(workspace)
        self.assertFalse(workspace.root.exists())

    async def test_binding_cannot_overwrite_arguments_and_failed_prepare_cleans(self):
        with self.assertRaises(ArtifactValidationError) as caught:
            await self.manager.prepare(
                access=self.access,
                tool_call_id="call-2",
                arguments={"input_file": "user-value"},
                bindings=[
                    ArtifactInputBinding(
                        artifact_id=self.artifact.artifact_id,
                        argument_pointer="/input_file",
                    )
                ],
                outputs=[],
            )
        self.assertEqual(caught.exception.code, "artifact_argument_already_set")
        self.assertEqual(list(self.manager.root.iterdir()), [])

    async def test_invalid_output_paths_are_rejected_by_schema(self):
        for value in ("../escape.txt", "/absolute.txt", "outputs/file.txt", "a\\b.txt"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    ArtifactOutputSpec(relative_path=value)

    async def test_collect_outputs_creates_durable_candidate_and_skips_missing(self):
        workspace = await self.manager.prepare(
            access=self.access,
            tool_call_id="call-3",
            arguments={},
            bindings=[],
            outputs=[
                ArtifactOutputSpec(
                    relative_path="result.md",
                    suggested_filename="final.md",
                ),
                ArtifactOutputSpec(relative_path="missing.txt"),
            ],
        )
        try:
            (workspace.outputs_dir / "result.md").write_text(
                "processed",
                encoding="utf-8",
            )
            candidates = await self.manager.collect_outputs(
                workspace,
                session_id="session-1",
                cycle_id="cycle-1",
                tool_call_id="call-3",
                tool_name="document_processor",
            )
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate.suggested_filename, "final.md")
            self.assertEqual(candidate.format_id, "markdown")
            self.assertEqual(candidate.source_artifact_ids, [])
            stored = await self.services.candidate_store.get(candidate.candidate_id)
            self.assertEqual(stored, candidate)
            self.assertEqual(
                await self.storage.content_store.read_content(candidate.content_id),
                b"processed",
            )
            self.assertNotIn(str(workspace.root), str(candidate.model_dump()))
        finally:
            await self.manager.cleanup(workspace)

    async def test_symlink_output_is_rejected(self):
        workspace = await self.manager.prepare(
            access=self.access,
            tool_call_id="call-4",
            arguments={},
            bindings=[],
            outputs=[ArtifactOutputSpec(relative_path="result.txt")],
        )
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = workspace.outputs_dir / "result.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            await self.manager.cleanup(workspace)
            self.skipTest("symlink creation is unavailable")
        try:
            with self.assertRaises(ArtifactWorkspaceError):
                await self.manager.collect_outputs(
                    workspace,
                    session_id="session-1",
                    cycle_id="cycle-1",
                    tool_call_id="call-4",
                    tool_name="processor",
                )
        finally:
            await self.manager.cleanup(workspace)

    async def test_cumulative_workspace_budget_includes_outputs(self):
        small_config = self.artifact_config.model_copy(
            update={"max_workspace_bytes": len(b"alpha beta") + 3}
        )
        manager = ArtifactWorkspaceManager(
            storage_config=self.storage_config,
            artifact_config=small_config,
            artifact_service=self.services.artifact_service,
            content_store=self.storage.content_store,
            candidate_store=self.services.candidate_store,
            format_registry=self.services.format_registry,
        )
        workspace = await manager.prepare(
            access=self.access,
            tool_call_id="call-5",
            arguments={},
            bindings=[
                ArtifactInputBinding(
                    artifact_id=self.artifact.artifact_id,
                    argument_pointer="/input_file",
                )
            ],
            outputs=[ArtifactOutputSpec(relative_path="result.txt")],
        )
        try:
            (workspace.outputs_dir / "result.txt").write_text(
                "four",
                encoding="utf-8",
            )
            with self.assertRaises(ArtifactLimitError):
                await manager.collect_outputs(
                    workspace,
                    session_id="session-1",
                    cycle_id="cycle-1",
                    tool_call_id="call-5",
                    tool_name="processor",
                )
        finally:
            await manager.cleanup(workspace)

    async def test_cleanup_refuses_path_outside_workspace_root(self):
        outside = self.root / "outside-directory"
        outside.mkdir()
        with self.assertRaises(ArtifactWorkspaceError):
            await self.manager.cleanup_path(outside)
        self.assertTrue(outside.exists())


if __name__ == "__main__":
    unittest.main()
