"""
AgentAdapter protocol definition.

Adapters control auto-send behavior: whether Enter is pressed after pasting
and how many times. Text injection itself is handled by main._send_local
using capture_lock + type_text (not the adapter).
"""

from dataclasses import dataclass
from typing import Optional, Protocol


class AgentAdapter(Protocol):
    """Protocol for agent-specific injection behavior."""

    @property
    def enter_count(self) -> int:
        """Number of Enter presses for auto-send."""
        ...

    def should_auto_send(self) -> bool:
        """Return True if Enter should be auto-pressed after injection.

        Wake word mode uses auto-send. PTT mode does not (user sends manually).
        """
        ...


# ---------------------------------------------------------------------------
# Workspace identity (generic — no app names here)
# ---------------------------------------------------------------------------
#
# Some target apps manage multiple "workspaces" inside one process (Conductor
# workspaces, and potentially editor window groups or tab sets in other
# agents). Voice targeting needs two app-specific capabilities behind a
# generic interface:
#   1. detect_context(pid): a fast, in-thread probe of what the app is
#      currently SHOWING (e.g. a branch name read from the AX tree).
#   2. resolve(context, profile): map that context to stable IDs (may touch
#      a DB/IPC — callers run it under a timeout).
# An app declares its provider by name in its app profile
# (`workspace_provider`); the registry lives in heyvox.adapters. Apps without
# workspace management simply have no provider and every consumer no-ops.


@dataclass(frozen=True)
class WorkspaceIdentity:
    """Stable workspace/session IDs as resolved by a WorkspaceProvider."""

    workspace_id: str
    session_id: Optional[str] = None


class WorkspaceProvider(Protocol):
    """App-specific workspace detection + resolution behind a generic face."""

    name: str

    def detect_context(self, pid: int) -> str:
        """Fast in-thread probe of the currently visible workspace context
        (e.g. a branch name). "" when undetectable — callers then skip
        resolve() entirely."""
        ...

    def resolve(self, context: str, profile) -> Optional[WorkspaceIdentity]:
        """Map a detected context to stable IDs. May block on DB/IPC —
        callers wrap this in a timeout. None when unresolvable."""
        ...
