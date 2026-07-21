"""Per-call context for manager tools that operate on agent runtime state."""

from dataclasses import dataclass
from typing import Any

from ..runtime import ActiveAgentCycle


@dataclass(slots=True)
class ManagerToolContext:
    session_id: str
    cycle_id: str
    active_cycle: ActiveAgentCycle
    session_state: Any
    progress_callback: Any = None
