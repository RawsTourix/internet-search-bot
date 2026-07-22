import unittest

from src.artifacts import (
    ArtifactConfigType,
    ArtifactConfigValidationError,
    apply_local_workspace_server_policy,
)
from src.mcp.mcp_client import ServerConfigType, ServerConnectType


class ArtifactServerPolicyTests(unittest.TestCase):
    def test_only_explicit_executable_servers_receive_workspace_transport(self):
        processor = ServerConfigType(
            name="processor",
            connect_type=ServerConnectType.EXECUTABLE,
        )
        search = ServerConfigType(
            name="search",
            connect_type=ServerConnectType.EXECUTABLE,
        )
        config = ArtifactConfigType(
            local_workspace_server_names=[" processor ", "processor"],
        )

        apply_local_workspace_server_policy([processor, search], config)

        self.assertEqual(processor.artifact_transport, "local_workspace")
        self.assertEqual(search.artifact_transport, "none")
        self.assertEqual(config.local_workspace_server_names, ["processor"])

    def test_unknown_server_rejects_without_partial_mutation(self):
        processor = ServerConfigType(
            name="processor",
            connect_type=ServerConnectType.EXECUTABLE,
        )
        object.__setattr__(processor, "artifact_transport", "sentinel")

        with self.assertRaises(ArtifactConfigValidationError):
            apply_local_workspace_server_policy(
                [processor],
                ArtifactConfigType(
                    local_workspace_server_names=["processor", "missing"],
                ),
            )

        self.assertEqual(processor.artifact_transport, "sentinel")

    def test_non_executable_server_rejects_without_partial_mutation(self):
        local = ServerConfigType(
            name="local",
            connect_type=ServerConnectType.EXECUTABLE,
        )
        remote = ServerConfigType(
            name="remote",
            connect_type=ServerConnectType.STREAMABLE_HTTP,
            url="https://example.invalid/mcp/",
        )
        object.__setattr__(local, "artifact_transport", "sentinel-local")
        object.__setattr__(remote, "artifact_transport", "sentinel-remote")

        with self.assertRaises(ArtifactConfigValidationError):
            apply_local_workspace_server_policy(
                [local, remote],
                ArtifactConfigType(
                    local_workspace_server_names=["local", "remote"],
                ),
            )

        self.assertEqual(local.artifact_transport, "sentinel-local")
        self.assertEqual(remote.artifact_transport, "sentinel-remote")

    def test_duplicate_configured_names_are_rejected(self):
        first = ServerConfigType(
            name="processor",
            connect_type=ServerConnectType.EXECUTABLE,
        )
        second = ServerConfigType(
            name="processor",
            connect_type=ServerConnectType.EXECUTABLE,
        )

        with self.assertRaises(ArtifactConfigValidationError):
            apply_local_workspace_server_policy(
                [first, second],
                ArtifactConfigType(
                    local_workspace_server_names=["processor"],
                ),
            )

        self.assertFalse(hasattr(first, "artifact_transport"))
        self.assertFalse(hasattr(second, "artifact_transport"))

    def test_empty_allowlist_marks_every_server_as_none(self):
        server = ServerConfigType(
            name="processor",
            connect_type=ServerConnectType.EXECUTABLE,
        )
        object.__setattr__(server, "artifact_transport", "local_workspace")

        apply_local_workspace_server_policy(
            [server],
            ArtifactConfigType(),
        )

        self.assertEqual(server.artifact_transport, "none")


if __name__ == "__main__":
    unittest.main()
