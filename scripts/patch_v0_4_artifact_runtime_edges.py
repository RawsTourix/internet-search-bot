from pathlib import Path

artifact_path = Path("src/mcp/artifact_client.py")
artifact_text = artifact_path.read_text(encoding="utf-8")
old_block = '''        self.artifact_services = artifact_services
        self.artifact_config = (
            artifact_services.config if artifact_services is not None else None
        )
        self.artifact_tool_controller = (
            ArtifactToolController(artifact_services.artifact_service)
            if artifact_services is not None
            else None
        )
        self.artifact_runtime = (
            ArtifactRuntimeCoordinator(artifact_services.artifact_service)
            if artifact_services is not None
            else None
        )
'''
new_block = '''        self.artifact_services = artifact_services
        self.artifact_config = (
            artifact_services.config if artifact_services is not None else None
        )
        artifacts_enabled = (
            artifact_services is not None
            and artifact_services.config.enabled
        )
        self.artifact_tool_controller = (
            ArtifactToolController(artifact_services.artifact_service)
            if artifacts_enabled
            else None
        )
        self.artifact_runtime = (
            ArtifactRuntimeCoordinator(artifact_services.artifact_service)
            if artifacts_enabled
            else None
        )
'''
if artifact_text.count(old_block) != 1:
    raise RuntimeError("artifact client initialization anchor changed")
artifact_path.write_text(
    artifact_text.replace(old_block, new_block, 1),
    encoding="utf-8",
)

planning_path = Path("src/mcp/planning_client.py")
planning_text = planning_path.read_text(encoding="utf-8")
old_control = '''    CONTROL_PLANE_MANAGER_TOOLS = frozenset(
        set(MCPClient.CONTROL_PLANE_MANAGER_TOOLS) | set(PLAN_TOOL_NAMES)
    )
'''
new_control = '''    CONTROL_PLANE_MANAGER_TOOLS = frozenset(
        set(ArtifactMCPClient.CONTROL_PLANE_MANAGER_TOOLS)
        | set(PLAN_TOOL_NAMES)
    )
'''
if planning_text.count(old_control) != 1:
    raise RuntimeError("planning control-plane anchor changed")
planning_path.write_text(
    planning_text.replace(old_control, new_control, 1),
    encoding="utf-8",
)
