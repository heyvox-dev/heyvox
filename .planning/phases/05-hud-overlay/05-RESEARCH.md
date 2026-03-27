# Phase 5: HUD Overlay - Research

**Researched:** 2026-03-27
**Domain:** PyObjC / AppKit / macOS overlay windows / Unix socket IPC
**Confidence:** MEDIUM (AppKit patterns verifiable; PyObjC-specific idioms partially inferred from bridge conventions)

---

## Summary

Phase 5 replaces the current 18px red dot overlay (`vox/hud/overlay.py`) with a full-featured
frosted-glass pill HUD. The HUD process is separate from `vox/main.py` and communicates via a
Unix domain socket (`/tmp/vox-hud.sock`). The IPC stub in `vox/hud/ipc.py` already defines the
message protocol; both sides need to be implemented.

The core AppKit pattern is well-understood and largely carries over from the existing overlay code:
borderless `NSWindow` at `NSStatusWindowLevel + 1`, `setCollectionBehavior_` with
`NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary`, and
`setIgnoresMouseEvents_(True)`. The main new surface is `NSVisualEffectView` (frosted glass),
layer-backed custom drawing for waveform bars and pill shape, and a second content path for TTS
controls that must receive mouse events while the rest of the window stays click-through.

The IPC design is the most consequential architectural decision: AppKit's `NSApplication.run()`
owns the main thread. A Unix socket server must live on a **background thread** (asyncio loop in a
daemon thread, or raw `socket` + `threading.Thread`). State changes received over the socket must
be dispatched back onto the main run loop via `performSelectorOnMainThread_withObject_waitUntilDone_`
or an `NSTimer` with zero interval (identical to the existing SIGTERM pattern).

**Primary recommendation:** Use `NSVisualEffectView` as the window's `contentView` with
`.hudWindow` material and `.behindWindow` blending mode; run the Unix socket server in a daemon
thread using standard `socket` + `threading`; dispatch UI updates to the main thread via `NSTimer`.

---

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `pyobjc-framework-Cocoa` | already in project | NSWindow, NSView, NSApplication, NSTimer, NSColor, NSBezierPath | Required for all AppKit work |
| `pyobjc-framework-Quartz` | already in project | CALayer, CAShapeLayer for layer-backed views | Needed for corner radius, waveform layer drawing |
| Python `socket` stdlib | 3.12 | Unix domain socket client (sender in main.py) | No extra dependency |
| Python `threading` stdlib | 3.12 | Background socket server thread in HUD process | Standard GUI + network pattern |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `asyncio` stdlib | 3.12 | Alternative to raw threading for socket server | Use if already familiar; adds complexity for this use case |
| `json` stdlib | 3.12 | Serialize/deserialize IPC messages | Newline-delimited JSON already defined in ipc.py |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Raw `socket` + thread | `asyncio.start_unix_server` in daemon thread | asyncio is cleaner for multiple concurrent clients but overkill for a single-client HUD; raw socket is simpler to debug |
| `NSTimer` dispatch | `performSelectorOnMainThread_withObject_waitUntilDone_` | Both work; NSTimer pattern is already proven in the existing SIGTERM handler |
| Custom `NSView` drawing | `AppKit.NSProgressIndicator` | NSProgressIndicator can't do waveform bars; custom drawing required |

**Installation:** No new packages needed. All required PyObjC frameworks are already declared in `pyproject.toml`.

---

## Architecture Patterns

### Recommended File Structure (within existing vox/hud/)

```
vox/hud/
├── overlay.py     # REPLACE entirely — full HUD NSApplication process
└── ipc.py         # IMPLEMENT both sides:
                   #   HUDServer class (used in overlay.py)
                   #   HUDClient class (used in main.py and mcp/server.py)
```

`main.py` needs a thin `HUDClient` that connects on startup and sends JSON messages at state
transitions. The HUD process runs `HUDServer` listening for connections.

---

### Pattern 1: Frosted Glass Pill Window

**What:** `NSVisualEffectView` set as the `contentView` of a borderless `NSWindow`. The visual
effect view is given `.hudWindow` material (integer value `23` in Objective-C enum) and
`.behindWindow` blending mode. Window background must be `clearColor` and `setOpaque_(False)`.

**When to use:** Always — this is the base window structure for the HUD.

**Swift reference pattern (confirmed, Apple docs):**
```swift
let visualEffect = NSVisualEffectView()
visualEffect.material = .hudWindow       // enum case 23
visualEffect.blendingMode = .behindWindow
visualEffect.state = .active
window.backgroundColor = .clear
window.isOpaque = false
window.contentView = visualEffect
```

**PyObjC translation:**
```python
# Source: Apple NSVisualEffectView docs + PyObjC bridge conventions
from AppKit import (
    NSVisualEffectView,
    NSVisualEffectMaterialHUDWindow,    # = 23
    NSVisualEffectBlendingModeBehindWindow,  # = 0
    NSVisualEffectStateActive,          # = 1
)

ve = NSVisualEffectView.alloc().initWithFrame_(content_rect)
ve.setMaterial_(NSVisualEffectMaterialHUDWindow)
ve.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
ve.setState_(NSVisualEffectStateActive)
w.setBackgroundColor_(NSColor.clearColor())
w.setOpaque_(False)
w.setContentView_(ve)
```

**IMPORTANT:** The integer constants for `NSVisualEffectMaterial` may need to be verified at
runtime since AppKit does not always export them by name in all PyObjC versions. Fall back to
`ve.setMaterial_(23)` if the named constant is missing.

---

### Pattern 2: Pill Shape via Layer Corner Radius

**What:** Enable layer backing on the content view, then set `layer.cornerRadius` to half the
window height. The window clips to its layer using `masksToBounds`.

**When to use:** For the pill shape of the idle/compact state.

```python
# Source: Apple Core Animation docs + PyObjC bridge conventions
ve.setWantsLayer_(True)
ve.layer().setCornerRadius_(pill_height / 2)
ve.layer().setMasksToBounds_(True)
```

For the pill's animated expand/contract: wrap `setFrame_` calls inside `NSAnimationContext`:

```python
# Source: Apple NSAnimationContext docs
from AppKit import NSAnimationContext

NSAnimationContext.beginGrouping()
NSAnimationContext.currentContext().setDuration_(0.2)
w.animator().setFrame_display_(new_frame, True)
NSAnimationContext.endGrouping()
```

**Pitfall:** `animator()` proxy on `NSWindow` only animates `frame`, `alphaValue`, and a few
other properties. For corner radius changes during expansion, animate separately on the layer
via `CABasicAnimation` or just set the new cornerRadius after the window frame settles.

---

### Pattern 3: Collection Behavior for All Spaces + Fullscreen

**What:** Combining the right `NSWindowCollectionBehavior` flags ensures the HUD appears above
fullscreen apps on every Space.

**Confirmed flags (multiple sources, HIGH confidence):**
```python
# Source: technetexperts.com PyObjC example + Apple developer forum thread
from AppKit import (
    NSWindowCollectionBehaviorCanJoinAllSpaces,     # 1 << 0 = 1
    NSWindowCollectionBehaviorFullScreenAuxiliary,  # 1 << 8 = 256
    NSWindowCollectionBehaviorStationary,           # 1 << 4 = 16
    NSWindowCollectionBehaviorIgnoresCycle,         # 1 << 6 = 64
)
w.setCollectionBehavior_(
    NSWindowCollectionBehaviorCanJoinAllSpaces |
    NSWindowCollectionBehaviorFullScreenAuxiliary |
    NSWindowCollectionBehaviorStationary |
    NSWindowCollectionBehaviorIgnoresCycle
)
```

The existing overlay.py uses `(1 << 0) | (1 << 8)` (canJoinAllSpaces + fullScreenAuxiliary) and
that works. `Stationary` and `IgnoresCycle` are additions that reduce visual jump on Space switch
and prevent CMD+` window cycling.

---

### Pattern 4: Click-Through Except TTS Controls

**What:** The window uses `setIgnoresMouseEvents_(True)` for the non-interactive state. When TTS
controls become visible, the window switches to `setIgnoresMouseEvents_(False)` and the custom
content view overrides `hitTest_` to return `None` (click-through) for background areas and the
actual button subviews for the control strip.

```python
class HUDContentView(NSView):
    def hitTest_(self, point):
        # Delegate to super — only buttons in the TTS strip region respond
        hit = objc.super(HUDContentView, self).hitTest_(point)
        if hit is self:
            return None   # Background is click-through
        return hit        # Subviews (buttons) respond normally
```

This avoids the binary on/off of `ignoresMouseEvents` for the window. When TTS state ends, remove
the button subviews and re-enable `setIgnoresMouseEvents_(True)`.

---

### Pattern 5: Unix Socket IPC — Background Thread + Main Thread Dispatch

**What:** The HUD process runs a socket server on a background daemon thread. Received messages
are dispatched to the main thread (which owns AppKit) via `NSTimer`.

```python
# Socket server (daemon thread, runs independently of AppKit)
import socket, threading, json

class HUDServer:
    def __init__(self, path, on_message):
        self._path = path
        self._on_message = on_message  # called on background thread

    def start(self):
        t = threading.Thread(target=self._serve, daemon=True)
        t.start()

    def _serve(self):
        import os
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
            srv.bind(self._path)
            srv.listen(1)
            while True:
                conn, _ = srv.accept()
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        buf = b""
        with conn:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                        self._on_message(msg)
                    except json.JSONDecodeError:
                        pass
```

**Dispatching to main thread from the socket callback:**
```python
# Source: existing overlay.py SIGTERM pattern — proven approach
from Foundation import NSObject
from AppKit import NSApplication
import AppKit

class _Dispatcher(NSObject):
    def applyMessage_(self, msg):
        # Called on main thread — safe to update NSView here
        update_hud(msg)

_dispatcher = _Dispatcher.alloc().init()

def on_message(msg):
    # Running on background socket thread — dispatch to main thread
    _dispatcher.performSelectorOnMainThread_withObject_waitUntilDone_(
        "applyMessage:", msg, False
    )
```

**Sender side (main.py / mcp/server.py):**
```python
class HUDClient:
    def __init__(self, path):
        self._path = path
        self._sock = None

    def connect(self):
        try:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.connect(self._path)
        except (FileNotFoundError, ConnectionRefusedError):
            self._sock = None  # HUD not running — degrade silently

    def send(self, msg: dict):
        if self._sock is None:
            return
        try:
            self._sock.sendall((json.dumps(msg) + "\n").encode())
        except (BrokenPipeError, OSError):
            self._sock = None  # HUD died — stop trying
```

---

### Pattern 6: Custom Waveform View (NSView drawRect_)

**What:** A subview inside the HUD draws volume bars in `drawRect_`. The main loop sends
`{"type": "audio_level", "level": 0.0-1.0}` messages; on receipt the view stores the level and
calls `setNeedsDisplay_(True)`.

```python
class WaveformView(NSView):
    _level = 0.0
    _num_bars = 5

    def setLevel_(self, level):
        self._level = level
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        bar_count = self._num_bars
        bar_width = rect.size.width / (bar_count * 2 - 1)
        for i in range(bar_count):
            # Vary bar height: center bar tallest, outer bars shorter
            # Apply _level as overall scale
            ...
        NSColor.redColor().setFill()
        NSBezierPath.fillRect_(bar_rect)
```

For smooth animation, `drawRect_` can be called up to 60fps by a timer that polls the latest
level, rather than calling `setNeedsDisplay_` on every incoming socket message.

---

### Pattern 7: Live Transcription Text

**What:** An `NSTextField` (non-editable, transparent background) as a subview. On
`{"type": "transcript", "text": "..."}` messages, call `setStringValue_` on main thread.

```python
from AppKit import NSTextField, NSTextAlignmentCenter

label = NSTextField.alloc().initWithFrame_(text_rect)
label.setEditable_(False)
label.setSelectable_(False)
label.setDrawsBackground_(False)
label.setBezeled_(False)
label.setTextColor_(NSColor.whiteColor())
label.setAlignment_(NSTextAlignmentCenter)
label.setStringValue_("")
```

Word-by-word appearance is achieved by the sender — `vox/audio/stt.py` would send partial
transcription tokens as they arrive. The HUD just updates the label on each message.

---

### Anti-Patterns to Avoid

- **Calling AppKit from background thread:** Any `setNeedsDisplay_`, `setStringValue_`, frame
  change, or visual update called from the socket thread will cause crashes or silent corruption.
  Always dispatch to main thread.
- **Using `asyncio.start_unix_server` on the main thread:** AppKit's `NSApplication.run()` owns
  the main run loop and is not compatible with asyncio's event loop on the same thread without
  the complex `NSRunLoopSelector` hack. Run socket server on a daemon thread instead.
- **`setIgnoresMouseEvents_(True)` on the whole window when TTS controls are visible:** This
  makes all buttons unclickable. Use per-view `hitTest_` override instead.
- **NSAnimationContext with very fast durations (< 0.1s) on frame changes:** Can cause visual
  tearing on Retina displays. Use 0.15–0.25s.
- **Hardcoding window position for multi-monitor:** Use `NSScreen.mainScreen()` (as the existing
  overlay does) which tracks the currently active display. Do not cache the screen frame at
  startup.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Frosted glass effect | Manual blur with CoreImage | `NSVisualEffectView` + `.hudWindow` material | System compositing; correct vibrancy adaptation for light/dark mode automatically |
| Smooth corner radius | Custom CGPath masking | `layer.cornerRadius` + `masksToBounds` on a layer-backed view | CALayer handles antialiasing and GPU compositing |
| Cross-thread UI dispatch | Custom queue/semaphore | `performSelectorOnMainThread_withObject_waitUntilDone_` | The AppKit-correct pattern; same as existing SIGTERM handler |
| Window animation | Manual NSTimer-driven frame updates | `NSAnimationContext` + `window.animator()` | System easing curves; no manual interpolation |

**Key insight:** AppKit's compositor handles all the hard visual work (blur sampling, vibrancy
color adaptation, GPU compositing). Fighting it with custom draws causes visual artifacts.

---

## Common Pitfalls

### Pitfall 1: NSVisualEffectView Material Constant Not Found

**What goes wrong:** `from AppKit import NSVisualEffectMaterialHUDWindow` raises `ImportError`.

**Why it happens:** PyObjC wraps the enum values but they may not be exported as module-level
names in older PyObjC versions. The Swift enum `.hudWindow` corresponds to raw value `23`.

**How to avoid:** Import defensively:
```python
try:
    from AppKit import NSVisualEffectMaterialHUDWindow
    HUD_MATERIAL = NSVisualEffectMaterialHUDWindow
except ImportError:
    HUD_MATERIAL = 23  # Raw enum value, stable since macOS 10.11
```

**Warning signs:** ImportError at startup; test by importing in a REPL first.

---

### Pitfall 2: Window Disappears Behind Fullscreen Apps

**What goes wrong:** HUD shows on normal windows but vanishes when the focused app goes fullscreen.

**Why it happens:** Missing `NSWindowCollectionBehaviorFullScreenAuxiliary` flag, or window level
set too low. `NSStatusWindowLevel` alone is not enough.

**How to avoid:** Set both `NSWindowCollectionBehaviorCanJoinAllSpaces` and
`NSWindowCollectionBehaviorFullScreenAuxiliary` in `setCollectionBehavior_`. Use
`NSStatusWindowLevel + 1` (same as existing overlay.py).

**Warning signs:** HUD visible on desktop; disappears when any app enters fullscreen.

---

### Pitfall 3: AppKit Calls From Socket Thread Cause Crashes

**What goes wrong:** Intermittent crashes (`NSInternalInconsistencyException`) or views not
updating because a background socket thread is calling AppKit APIs directly.

**Why it happens:** AppKit is not thread-safe. All UI mutations must happen on the main thread.

**How to avoid:** Always use `performSelectorOnMainThread_withObject_waitUntilDone_` or a
zero-delay NSTimer to dispatch state from socket callbacks.

**Warning signs:** Occasional crash with `NSInternalInconsistencyException` or `EXC_BAD_ACCESS`
in a stack trace that includes socket recv.

---

### Pitfall 4: Socket File Left Over from Previous Crash

**What goes wrong:** `HUDServer._serve()` fails with `OSError: [Errno 98] Address already in use`
on restart.

**Why it happens:** `/tmp/vox-hud.sock` was not cleaned up when the HUD was killed with SIGKILL.

**How to avoid:** Always `os.unlink(path)` before `bind()`, catching `FileNotFoundError`. The
existing `_kill_orphan_indicators()` pattern in `main.py` can also pre-clean the socket.

**Warning signs:** HUD fails to start on second launch; `ls /tmp/vox-hud.sock` shows a stale file.

---

### Pitfall 5: Pill Width/Height Hardcoded for One Screen DPI

**What goes wrong:** Pill appears too small on Retina displays or too large on lower-res external
monitors.

**Why it happens:** macOS uses point coordinates (not pixels); hardcoded sizes in points are
fine. But if the developer accidentally uses pixel values from a screenshot, they will be 2x too
large.

**How to avoid:** Use NSScreen point dimensions (what AppKit already works in). The existing
overlay.py correctly uses `screen.size.width` in points.

---

### Pitfall 6: `ignoresMouseEvents` Blocks TTS Control Clicks

**What goes wrong:** Pause/skip/stop buttons are visible but unclickable.

**Why it happens:** `window.setIgnoresMouseEvents_(True)` blocks all mouse events for the entire
window — it does not respect per-subview settings.

**How to avoid:** Set `ignoresMouseEvents_(False)` when TTS controls are visible. Use `hitTest_`
override in the content view to return `None` for non-button areas. See Pattern 4.

---

## Code Examples

Verified patterns from official / confirmed sources:

### Frosted Glass Window Setup (PyObjC, confirmed from Apple docs + bridge conventions)
```python
# Source: Apple NSVisualEffectView docs + technetexperts.com PyObjC overlay example
from AppKit import (
    NSApplication, NSWindow, NSWindowStyleMaskBorderless,
    NSBackingStoreBuffered, NSScreen, NSColor, NSStatusWindowLevel,
    NSVisualEffectView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowCollectionBehaviorIgnoresCycle,
)

try:
    from AppKit import NSVisualEffectMaterialHUDWindow as HUD_MATERIAL
except ImportError:
    HUD_MATERIAL = 23

PILL_W, PILL_H = 160, 36

screen = NSScreen.mainScreen().frame()
x = screen.origin.x + (screen.size.width - PILL_W) / 2
y = screen.origin.y + screen.size.height - PILL_H - 12

w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    ((x, y), (PILL_W, PILL_H)),
    NSWindowStyleMaskBorderless,
    NSBackingStoreBuffered,
    False,
)
w.setLevel_(NSStatusWindowLevel + 1)
w.setOpaque_(False)
w.setBackgroundColor_(NSColor.clearColor())
w.setIgnoresMouseEvents_(True)
w.setCollectionBehavior_(
    NSWindowCollectionBehaviorCanJoinAllSpaces |
    NSWindowCollectionBehaviorFullScreenAuxiliary |
    NSWindowCollectionBehaviorStationary |
    NSWindowCollectionBehaviorIgnoresCycle
)

ve = NSVisualEffectView.alloc().initWithFrame_(((0, 0), (PILL_W, PILL_H)))
ve.setMaterial_(HUD_MATERIAL)
ve.setBlendingMode_(0)   # NSVisualEffectBlendingModeBehindWindow
ve.setState_(1)           # NSVisualEffectStateActive
ve.setWantsLayer_(True)
ve.layer().setCornerRadius_(PILL_H / 2)
ve.layer().setMasksToBounds_(True)
w.setContentView_(ve)
w.orderFrontRegardless()
```

### Dispatching Socket Message to Main Thread
```python
# Source: existing overlay.py SIGTERM pattern — proven in codebase
from Foundation import NSObject
from AppKit import NSTimer

class HUDDispatcher(NSObject):
    def applyState_(self, state_str):
        # Safe: running on main AppKit thread
        _apply_hud_state(state_str)

_dispatcher = HUDDispatcher.alloc().init()

def dispatch_state(state: str):
    """Call from any thread to safely update HUD on main thread."""
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.0, _dispatcher, "applyState:", state, False
    )
```

### Window Frame Animation (Pill Expand/Contract)
```python
# Source: Apple NSAnimationContext docs
from AppKit import NSAnimationContext

def animate_pill(window, new_frame):
    NSAnimationContext.beginGrouping()
    NSAnimationContext.currentContext().setDuration_(0.2)
    window.animator().setFrame_display_(new_frame, True)
    NSAnimationContext.endGrouping()
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `NSBlendingModeCompositeSourceOver` manual blur | `NSVisualEffectView` compositor | macOS 10.10 (2014) | System handles vibrancy; much simpler code |
| Fixed integer `setCollectionBehavior_` | Named enum constants | PyObjC 9.x | Readability; but raw integers still work as fallback |
| `NSWindow.setIgnoresMouseEvents_` for entire window | Per-subview `hitTest_` override | Longstanding pattern, but not widely documented | Enables mixed click-through in a single window |
| Polling flags (`/tmp/vox-recording`) | Unix socket IPC | Phase 5 is introducing this | Lower latency; richer message types; bidirectional |

**Deprecated/outdated:**
- File flag polling (`/tmp/vox-recording`, `/tmp/vox-tts-playing`) for HUD state: replaced by
  socket IPC in Phase 5, though the existing flags remain for echo suppression in `main.py`.
- `NSBlurFilter` / `CIFilter` manual blur: unnecessary now that `NSVisualEffectView` is standard.

---

## Integration Points with Existing Code

### What changes in `vox/main.py`
The existing `_show_recording_indicator()` function launches/kills the overlay subprocess and has
no IPC. In Phase 5 this stays structurally the same (subprocess launch), but `main.py` also
needs to instantiate a `HUDClient` and call `client.send({...})` at each state transition:

| Event in main.py | IPC message to send |
|-----------------|---------------------|
| `start_recording()` called | `{"type": "state", "state": "listening"}` + periodic `audio_level` |
| `stop_recording()` → `_send_local()` starts | `{"type": "state", "state": "processing"}` |
| `type_text()` / `inject_text()` completes | `{"type": "state", "state": "idle"}` |

### What changes in `vox/audio/tts.py`
The TTS worker needs to emit:
- `{"type": "tts_start", "text": "..."}` when dequeuing a new item
- `{"type": "queue_update", "count": N}` after any enqueue/dequeue
- `{"type": "tts_end"}` in the `finally` block of `_tts_worker`

### What changes in `vox/hud/overlay.py`
Complete replacement. The new file:
1. Starts `HUDServer` on a daemon thread before `app.run()`
2. Builds the frosted-glass pill window
3. Keeps the SIGTERM/SIGINT handler (unchanged pattern)
4. Implements state machine: idle / listening / processing / speaking

### What changes in `vox/hud/ipc.py`
Add `HUDServer` and `HUDClient` classes. Keep `SOCKET_PATH` constant.

---

## Open Questions

1. **TTS progress bar implementation**
   - What we know: `tts_start` carries the full text; Kokoro generates chunks; each chunk is
     a fixed audio segment.
   - What's unclear: The current `_tts_worker` does not emit per-chunk progress. Does the HUD
     show character position, chunk count, or just a pulsing indicator?
   - Recommendation: Implement as pulsing "speaking" animation initially; real progress tracking
     requires instrumenting the Kokoro pipeline loop (medium effort, can be deferred).

2. **Audio level sampling rate for waveform**
   - What we know: `main.py` already reads audio chunks in the wake word loop.
   - What's unclear: Should audio level messages be sent every chunk (~15ms at 16kHz/256 samples)
     or throttled? Sending every chunk = ~66 messages/sec over a Unix socket.
   - Recommendation: Throttle to ~20fps (50ms) in `main.py` before sending. Measure overhead
     empirically during development.

3. **HUD client reconnect**
   - What we know: `HUDClient.send()` silently drops on `BrokenPipeError`.
   - What's unclear: Should `main.py` attempt reconnection if the HUD process crashes?
   - Recommendation: Add a periodic reconnect attempt (e.g., every 5 seconds) with the same
     silent-fail behavior if the HUD is not running.

4. **Multi-monitor: which screen shows the HUD?**
   - What we know: `NSScreen.mainScreen()` follows the screen with the menu bar (the "active"
     display).
   - What's unclear: User may prefer the HUD on a specific screen.
   - Recommendation: Use `mainScreen()` for v1. Config option can be added later.

5. **macOS 26 Liquid Glass (Tahoe) compatibility**
   - What we know: macOS 26 introduces "Liquid Glass" effects; a CPython issue (139404) notes
     display issues with the new SDK.
   - What's unclear: Does `NSVisualEffectView` with `.hudWindow` material still work correctly
     under macOS 26?
   - Recommendation: Implement against current macOS 15 behavior. Test on macOS 26 beta when
     available; fall back to a solid dark pill if visual effects are broken.

---

## Sources

### Primary (HIGH confidence)
- Apple Developer Docs — `NSVisualEffectView.Material.hudWindow`: confirmed material enum value and usage
- Apple Developer Docs — `NSWindow.CollectionBehavior` (fullScreenAuxiliary, canJoinAllSpaces): behavior flags confirmed
- Apple Developer Docs — `NSAnimationContext`: animation pattern confirmed
- Existing `vox/hud/overlay.py` codebase — collection behavior flags `(1<<0)|(1<<8)` proven working

### Secondary (MEDIUM confidence)
- technetexperts.com — PyObjC overlay with collection behavior: complete working Python code with all four flags; independently corroborated by Apple forum thread
- gist/pommdau NSVisualEffectView.md — 14 material constants including `hudWindow`, Swift but translatable
- gist/onmyway133 blog issue 610 — NSVisualEffectView as contentView pattern with autoresizing mask
- gist/dlech NSRunLoop+asyncio — confirms threading approach; daemon thread with asyncio is valid alternative

### Tertiary (LOW confidence — confirm before implementing)
- PyObjC named constant `NSVisualEffectMaterialHUDWindow` availability: searched but not directly confirmed in PyObjC source; use integer fallback `23`
- `performSelectorOnMainThread_withObject_waitUntilDone_` with dict argument: works in Objective-C; Python `dict` bridging to `NSDictionary` should be automatic but warrants a quick test

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — no new dependencies; existing PyObjC already in project
- Architecture (window/layer patterns): MEDIUM-HIGH — Swift patterns confirmed; PyObjC bridge translation inferred from conventions but not exhaustively tested
- IPC threading: HIGH — asyncio-in-thread is standard Python; NSTimer dispatch pattern proven in existing codebase
- Pitfalls: HIGH — all derived from AppKit threading rules + existing code patterns

**Research date:** 2026-03-27
**Valid until:** 2026-06-27 (stable AppKit APIs; reassess if macOS 26 ships with breaking changes)
