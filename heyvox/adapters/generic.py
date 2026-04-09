"""
Generic AgentAdapter — controls auto-send behavior for generic/pinned-app mode.

Two modes:
- No target_app (always-focused): no auto-send, user presses Enter manually.
- With target_app (pinned-app): auto-send after pasting.

Requirement: INPT-03
"""


class GenericAdapter:
    """Controls auto-send behavior for generic injection.

    Args:
        target_app: If set, enables auto-send (pinned-app mode).
        enter_count: Number of Enter keypresses for auto-send.
    """

    def __init__(self, target_app: str = "", enter_count: int = 2) -> None:
        self._target_app = target_app
        self._enter_count = enter_count

    @property
    def enter_count(self) -> int:
        return self._enter_count

    def should_auto_send(self) -> bool:
        """Return True if a target app is pinned (AI agent use case)."""
        return bool(self._target_app)
