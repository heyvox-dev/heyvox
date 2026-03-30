"""
AgentAdapter protocol definition.

Adapters abstract how transcribed text is delivered to different AI coding agents.
Each adapter implements inject_text (how to send the text) and should_auto_send
(whether to press Enter automatically after pasting).
"""

from typing import Protocol


class AgentAdapter(Protocol):
    """Protocol for agent-specific text injection adapters."""

    def inject_text(self, text: str) -> None:
        """Inject transcribed text into the target agent's input field.

        Args:
            text: Transcribed and processed text to inject.
        """
        ...

    def should_auto_send(self) -> bool:
        """Return True if the adapter should auto-press Enter after injection.

        Wake word mode uses auto-send. PTT mode does not (user sends manually).

        Returns:
            True to press Enter after inject_text, False to leave cursor in field.
        """
        ...
