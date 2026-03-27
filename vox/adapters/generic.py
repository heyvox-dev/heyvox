"""
Generic AgentAdapter — pastes into whichever app is currently focused.

This is the simplest adapter: no app switching, just paste the text.
The user is responsible for having the target agent's input field focused
before triggering the wake word or PTT.

Used when target_app is not set or set to a non-specific value.
"""

from vox.input.injection import type_text


class GenericAdapter:
    """Paste transcription into the currently focused application."""

    def inject_text(self, text: str) -> None:
        """Paste text into the focused app via clipboard + Cmd-V.

        Args:
            text: Transcribed text to inject.
        """
        type_text(text)

    def should_auto_send(self) -> bool:
        """Generic adapter does not auto-send — user sends manually.

        Returns:
            Always False.
        """
        return False
