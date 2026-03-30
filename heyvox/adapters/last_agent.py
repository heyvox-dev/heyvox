"""
LastAgentAdapter — tracks which AI coding agent was last focused and injects
transcribed text into it on demand.

Polls NSWorkspace.frontmostApplication() every second in a daemon thread. When
inject_text() is called, focuses the last-seen agent app before pasting.

Requirement: INPT-04 (smart target detection), INPT-05 (last-agent mode)
"""

import threading
import time

from heyvox.input.injection import type_text, focus_app, focus_input


class LastAgentAdapter:
    """Track the most recently focused AI agent and inject text into it.

    Args:
        agents: List of application names to monitor (e.g. ["Claude", "Cursor"]).
            Matching is case-insensitive and substring-based.
        enter_count: Number of Enter keypresses after pasting (auto-send).
    """

    def __init__(self, agents: list[str], enter_count: int = 2) -> None:
        self._agents = [a.lower() for a in agents]
        self._enter_count = enter_count
        self._last_agent_name: str | None = None
        self._start_observer()

    @property
    def enter_count(self) -> int:
        return self._enter_count

    def _start_observer(self) -> None:
        """Start a daemon thread that polls NSWorkspace for frontmost app."""

        def _poll() -> None:
            # Lazy import: AppKit is heavy and only available on macOS. Deferring
            # the import avoids load-time failures on non-macOS or in tests.
            import sys
            try:
                import AppKit
                workspace = AppKit.NSWorkspace.sharedWorkspace()
            except Exception as e:
                print(f"[last-agent] AppKit unavailable: {e}", file=sys.stderr)
                return  # AppKit unavailable — last-agent tracking disabled

            _first_match = True
            while True:
                try:
                    app = workspace.frontmostApplication()
                    if app is not None:
                        name = app.localizedName() or ""
                        name_lower = name.lower()
                        for agent in self._agents:
                            if agent in name_lower:
                                if self._last_agent_name != name or _first_match:
                                    print(f"[last-agent] Tracked: {name}", file=sys.stderr)
                                    _first_match = False
                                self._last_agent_name = name
                                break
                except Exception as e:
                    print(f"[last-agent] Poll error: {e}", file=sys.stderr)
                time.sleep(1.0)

        t = threading.Thread(target=_poll, daemon=True, name="vox-last-agent-observer")
        t.start()

    def inject_text(self, text: str) -> None:
        """Focus the last-seen agent app (if known) and paste text.

        Args:
            text: Transcribed text to inject.
        """
        import sys
        print(f"[last-agent] inject_text: _last_agent_name={self._last_agent_name!r}", file=sys.stderr)
        if self._last_agent_name:
            focus_app(self._last_agent_name)
            time.sleep(0.5)  # Electron apps (Conductor, Cursor) need time to activate
            focus_input(self._last_agent_name)
            time.sleep(0.3)  # Wait for input field to be ready
        type_text(text)

    def should_auto_send(self) -> bool:
        """Last-agent mode always auto-sends after pasting.

        Returns:
            Always True.
        """
        return True
