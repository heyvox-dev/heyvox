"""
AgentAdapter protocol definition.

Adapters control auto-send behavior: whether Enter is pressed after pasting
and how many times. Text injection itself is handled by main._send_local
using capture_lock + type_text (not the adapter).
"""

from typing import Protocol


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
