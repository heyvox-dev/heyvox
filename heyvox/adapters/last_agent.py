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
        self._last_injected_via_socket = False
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
            import sys
            try:
                import AppKit
                workspace = AppKit.NSWorkspace.sharedWorkspace()
            except Exception as e:
                print(f"[last-agent] AppKit unavailable: {e}", file=sys.stderr)
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
                                    print(f"[last-agent] Tracked: {name}", file=sys.stderr)
                                    _first_match = False
                                break
                except Exception as e:
                    print(f"[last-agent] Poll error: {e}", file=sys.stderr)
                time.sleep(1.0)

        t = threading.Thread(target=_poll, daemon=True, name="vox-last-agent-observer")
        t.start()

    def _has_socket_injection(self) -> bool:
        """Return True if the last-seen agent has a direct socket injection path.

        Currently only Conductor has a sidecar socket (though it's disabled).
        This check uses the app profile system -- no hardcoded app names.
        """
        if self._last_agent_name is None or self._config is None:
            return False
        # Future: app profiles could declare a socket_injection_module field.
        # For now, this always returns False since socket injection is disabled.
        return False

    def _try_socket_injection(self, text: str) -> bool:
        """Try to inject text via a direct socket (no focus needed).

        Returns True if successful, False to fall back to clipboard + paste.
        Currently disabled -- all apps use the clipboard + paste path.
        """
        # NOTE: Direct socket injection is disabled. The Conductor sidecar
        # registers methods on an internal tunnel, not the external Unix socket.
        # Kept for future use if an app exposes a public injection API.
        return False

    def inject_text(self, text: str) -> None:
        """Focus the last-seen agent app (if known) and paste text.

        Tries direct socket injection first for apps that support it (bypasses
        focus switching entirely). Falls back to clipboard + Cmd-V on failure.
        """
        from heyvox.input.injection import focus_app, type_text
        self._last_injected_via_socket = False
        print(f"[last-agent] inject_text: _last_agent_name={self._last_agent_name!r}", file=sys.stderr)

        # Try direct socket injection first (no focus switch needed)
        if self._has_socket_injection():
            if self._try_socket_injection(text):
                self._last_injected_via_socket = True
                print("[last-agent] Injected via socket (no focus switch)", file=sys.stderr)
                return
            print("[last-agent] Socket injection failed, falling back to paste", file=sys.stderr)

        # Fallback: focus app + clipboard + Cmd-V
        if self._last_agent_name:
            focus_app(self._last_agent_name)
            import time as _time
            _time.sleep(0.5)
        type_text(text, app_name=self._last_agent_name)

    def should_auto_send(self) -> bool:
        """Last-agent mode always auto-sends after pasting.

        When injected via socket, auto-send is not needed -- the message
        is delivered directly to the session.
        """
        if self._last_injected_via_socket:
            print(f"[last-agent] should_auto_send=False (socket injection)", file=sys.stderr, flush=True)
            return False
        print(f"[last-agent] should_auto_send=True, last_agent={self._last_agent_name}", file=sys.stderr, flush=True)
        return True
