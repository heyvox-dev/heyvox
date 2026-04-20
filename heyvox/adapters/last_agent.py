"""
LastAgentAdapter -- tracks which AI coding agent was last focused.

Polls NSWorkspace.frontmostApplication() every second in a daemon thread.
Used by main._send_local to decide whether to auto-send (Enter) and to
report which agent received the text in the HUD.

Requirement: INPT-04 (smart target detection), INPT-05 (last-agent mode)
"""

import sys
import threading
import time


def _safe_stderr(msg: str) -> None:
    try:
        print(msg, file=sys.stderr, flush=True)
    except (BrokenPipeError, OSError):
        pass


class LastAgentAdapter:
    """Track the most recently focused AI agent for auto-send decisions.

    Args:
        agents: List of application names to monitor (e.g. ["Claude", "Cursor"]).
            Matching is case-insensitive and substring-based.
        enter_count: Number of Enter keypresses after pasting (auto-send).
        config: HeyvoxConfig instance for app profile lookup. If None,
            direct socket injection and profile-based enter counts are skipped.
    """

    def __init__(self, agents: list[str], enter_count: int = 2, config=None) -> None:
        self._agents = [a.lower() for a in agents]
        self._enter_count = enter_count
        self._config = config
        self._last_agent_name: str | None = None
        self._lock = threading.Lock()
        self._start_observer()

    @property
    def enter_count(self) -> int:
        return self._enter_count

    @property
    def last_agent_name(self) -> str | None:
        with self._lock:
            return self._last_agent_name

    def _start_observer(self) -> None:
        """Start a daemon thread that polls NSWorkspace for frontmost app."""

        def _poll() -> None:
            try:
                import AppKit
                workspace = AppKit.NSWorkspace.sharedWorkspace()
            except Exception as e:
                _safe_stderr(f"[last-agent] AppKit unavailable: {e}")
                return

            _first_match = True
            while True:
                try:
                    app = workspace.frontmostApplication()
                    if app is not None:
                        name = app.localizedName() or ""
                        name_lower = name.lower()
                        for agent in self._agents:
                            if agent in name_lower:
                                with self._lock:
                                    changed = self._last_agent_name != name
                                    self._last_agent_name = name
                                if changed or _first_match:
                                    _safe_stderr(f"[last-agent] Tracked: {name}")
                                    _first_match = False
                                break
                except Exception as e:
                    _safe_stderr(f"[last-agent] Poll error: {e}")
                time.sleep(1.0)

        t = threading.Thread(target=_poll, daemon=True, name="vox-last-agent-observer")
        t.start()

    def should_auto_send(self) -> bool:
        """Last-agent mode always auto-sends after pasting."""
        _safe_stderr(f"[last-agent] should_auto_send=True, last_agent={self._last_agent_name}")
        return True
