"""Live frontmost-app lookup.

`NSWorkspace.frontmostApplication()` is a cached value that AppKit refreshes
through the main run loop. The HeyVox listener never spins one for AppKit, so
inside it the value stays frozen at whatever was frontmost when the process
started: every `[inject] saved pre-inject frontmost pid=` line of one listener
run shows the same pid, and once that app has quit its `bundleIdentifier()` is
None (DEF-260). Anything that asks "which app is frontmost right now" must use
`frontmost_app()` instead. It asks LaunchServices directly through
`lsappinfo` (~16 ms for the two calls) and only falls back to the cached
NSWorkspace value when `lsappinfo` is unavailable.

The returned object is duck-typed like NSRunningApplication for the three
getters the call sites use: processIdentifier(), bundleIdentifier(),
localizedName().
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Optional

_LSAPPINFO = "/usr/bin/lsappinfo"
_TIMEOUT = 1.0


@dataclass(frozen=True)
class FrontmostApp:
    pid: int
    bundle_id: Optional[str]
    name: Optional[str]

    def processIdentifier(self) -> int:
        return self.pid

    def bundleIdentifier(self) -> Optional[str]:
        return self.bundle_id

    def localizedName(self) -> Optional[str]:
        return self.name


def _lsappinfo(*args: str) -> str:
    r = subprocess.run(
        [_LSAPPINFO, *args], capture_output=True, text=True, timeout=_TIMEOUT,
    )
    return r.stdout if r.returncode == 0 else ""


def _lsappinfo_front() -> Optional[FrontmostApp]:
    """Frontmost app straight from LaunchServices, None if unavailable."""
    try:
        asn = _lsappinfo("front").strip()
        if not asn:
            return None
        out = _lsappinfo("info", "-only", "pid", "-only", "bundleid", "-only", "name", asn)
    except (OSError, subprocess.SubprocessError):
        return None
    fields = dict(re.findall(r'"([^"]+)"=("[^"]*"|\d+)', out))
    pid_raw = fields.get("pid", "")
    if not pid_raw.isdigit():
        return None
    bundle = fields.get("CFBundleIdentifier", "").strip('"') or None
    name = fields.get("LSDisplayName", "").strip('"') or None
    return FrontmostApp(pid=int(pid_raw), bundle_id=bundle, name=name)


def frontmost_app():
    """The app that is frontmost right now, or None when it cannot be determined."""
    live = _lsappinfo_front()
    if live is not None:
        return live
    try:
        import AppKit
        return AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    except Exception:
        return None
