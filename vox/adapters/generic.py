"""
Generic AgentAdapter — pastes into whichever app is currently focused, with
optional app-focusing for pinned-app mode.

This adapter handles two modes:
- No target_app (always-focused): paste into whatever is focused. No auto-send.
- With target_app (pinned-app): focus the given app before pasting. Auto-send.

The user is responsible for having the target agent's input field focused when
target_app is not set.

Requirement: INPT-03
"""

import time

from vox.input.injection import type_text


class GenericAdapter:
    """Paste transcription into the focused or a specified application.

    Args:
        target_app: If set, focus this app before pasting (pinned-app mode).
            Empty string = always paste into currently focused app.
        enter_count: Number of Enter keypresses after pasting (for auto-send).
    """

    def __init__(self, target_app: str = "", enter_count: int = 2) -> None:
        self._target_app = target_app
        self._enter_count = enter_count

    @property
    def enter_count(self) -> int:
        return self._enter_count

    def inject_text(self, text: str) -> None:
        """Paste text into the focused app (or focused target_app) via clipboard + Cmd-V.

        If target_app is set, focuses that application first, then pastes.

        Args:
            text: Transcribed text to inject.
        """
        if self._target_app:
            from vox.input.injection import focus_app
            focus_app(self._target_app)
            time.sleep(0.3)
        type_text(text)

    def should_auto_send(self) -> bool:
        """Return True if a target app is pinned (AI agent use case).

        Returns:
            True when target_app is set (pinned-app mode) — user expects Enter
            to be pressed. False when always-focused — user sends manually.
        """
        return bool(self._target_app)
