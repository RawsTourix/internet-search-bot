from pathlib import Path

path = Path("src/mcp/planning_client.py")
text = path.read_text(encoding="utf-8")

import_anchor = "from ..storage import StorageServices\nfrom .manager_context import ManagerToolContext\n"
import_replacement = (
    "from ..storage import StorageServices\n"
    "from .artifact_client import ArtifactMCPClient\n"
    "from .manager_context import ManagerToolContext\n"
)
class_anchor = "class PlanningMCPClient(MCPClient):"
class_replacement = "class PlanningMCPClient(ArtifactMCPClient):"

if text.count(import_anchor) != 1:
    raise RuntimeError("planning_client import anchor changed")
if text.count(class_anchor) != 1:
    raise RuntimeError("planning_client class anchor changed")

text = text.replace(import_anchor, import_replacement, 1)
text = text.replace(class_anchor, class_replacement, 1)
path.write_text(text, encoding="utf-8")
