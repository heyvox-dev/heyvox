"""
HUD overlay process for Vox voice layer.

Implements a frosted-glass pill window that floats at the top-center of the
main screen, communicating voice state visually.

State machine:
- idle:       compact gray pill (48x32), click-through, no content
- listening:  expanded red pill (320x32), waveform amplitude bars
- processing: expanded amber pill (320x32), "Transcribing..." label
- speaking:   expanded green pill (320x32), text snippet + Skip/Stop buttons

IPC: HUDServer receives JSON messages over /tmp/vox-hud.sock on a daemon
thread and dispatches state changes to the main AppKit thread via
performSelectorOnMainThread_withObject_waitUntilDone_.

Requirements: HUD-01 through HUD-08
"""

import signal
import sys
import threading


# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

PILL_W_IDLE = 48
PILL_H = 32
PILL_W_ACTIVE = 320
PILL_MARGIN_TOP = 12
ANIM_DURATION = 0.2

# State → (r, g, b, a) overlay color (semi-transparent so frosted glass shows)
STATE_COLORS = {
    "idle":       (0.5, 0.5, 0.5, 0.6),
    "listening":  (1.0, 0.2, 0.2, 0.8),
    "processing": (1.0, 0.7, 0.0, 0.8),
    "speaking":   (0.2, 0.8, 0.3, 0.8),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _centered_x(screen_frame, pill_w):
    """Return the x origin that centers pill_w on the given screen frame."""
    return screen_frame.origin.x + (screen_frame.size.width - pill_w) / 2


def _pill_y(screen_frame):
    """Return the y origin for the pill (PILL_MARGIN_TOP below the top edge)."""
    return screen_frame.origin.y + screen_frame.size.height - PILL_H - PILL_MARGIN_TOP


# ---------------------------------------------------------------------------
# Custom NSView subclasses (defined inside main() to ensure AppKit is loaded)
# ---------------------------------------------------------------------------

def _make_waveform_view_class():
    from AppKit import NSView, NSColor, NSBezierPath

    class WaveformView(NSView):
        """Draws N amplitude bars whose height scales with the audio level.

        The center bar is tallest (level * rect.height), outer bars are
        progressively shorter using scale factors [0.4, 0.6, 1.0, 0.6, 0.4].
        """
        _level = 0.0
        _num_bars = 5
        _scale_factors = [0.4, 0.6, 1.0, 0.6, 0.4]

        def setLevel_(self, level):
            self._level = max(0.0, min(1.0, level))
            self.setNeedsDisplay_(True)

        def drawRect_(self, rect):
            n = self._num_bars
            total_w = rect.size.width
            # Each bar takes 1 unit, each gap takes 1 unit; n bars → 2n-1 units
            unit = total_w / (n * 2 - 1)
            bar_w = unit

            NSColor.whiteColor().colorWithAlphaComponent_(0.9).setFill()
            for i in range(n):
                scale = self._scale_factors[i]
                bar_h = max(3.0, rect.size.height * self._level * scale)
                x = rect.origin.x + i * unit * 2
                y = rect.origin.y + (rect.size.height - bar_h) / 2
                bar_rect = ((x, y), (bar_w, bar_h))
                NSBezierPath.fillRect_(bar_rect)

    return WaveformView


def _make_content_view_class():
    """Create HUDContentView — click-through background, buttons respond."""
    from AppKit import NSView
    import objc

    class HUDContentView(NSView):
        """Content view with selective hit-testing.

        Clicking on the background passes through to windows below.
        Clicking on button subviews works normally (TTS controls).

        Requirement: HUD-04
        """

        def hitTest_(self, point):
            hit = objc.super(HUDContentView, self).hitTest_(point)
            if hit is self:
                return None   # Background is click-through
            return hit        # Subviews (buttons) respond normally

    return HUDContentView


# ---------------------------------------------------------------------------
# State application
# ---------------------------------------------------------------------------

def _apply_state(
    state_str,
    window,
    content_view,
    waveform_view,
    transcript_label,
    tts_controls,
    color_overlay,
    tts_text=None,
):
    """Apply HUD visual state on the main thread.

    Requirements: HUD-01 (pill), HUD-02 (waveform), HUD-03 (transcript),
                  HUD-04 (TTS controls), HUD-05 (colors)
    """
    from AppKit import NSAnimationContext, NSColor, NSScreen

    r, g, b, a = STATE_COLORS.get(state_str, STATE_COLORS["idle"])
    color = NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)
    color_overlay.setBackgroundColor_(color)
    color_overlay.setNeedsDisplay_(True)

    screen = NSScreen.mainScreen().frame()
    is_active = state_str in ("listening", "processing", "speaking")
    new_pill_w = PILL_W_ACTIVE if is_active else PILL_W_IDLE

    x = _centered_x(screen, new_pill_w)
    y = _pill_y(screen)

    # Animate window frame
    NSAnimationContext.beginGrouping()
    NSAnimationContext.currentContext().setDuration_(ANIM_DURATION)
    window.animator().setFrame_display_(((x, y), (new_pill_w, PILL_H)), True)
    NSAnimationContext.endGrouping()

    # Update subview frames to match new pill width
    content_view.setFrame_(((0, 0), (new_pill_w, PILL_H)))
    color_overlay.setFrame_(((0, 0), (new_pill_w, PILL_H)))

    # Waveform (visible only when listening)
    wf_visible = state_str == "listening"
    waveform_view.setHidden_(not wf_visible)

    # Transcript label
    label_visible = state_str in ("processing", "speaking")
    transcript_label.setHidden_(not label_visible)
    if state_str == "processing":
        transcript_label.setStringValue_("Transcribing...")
    elif state_str == "speaking" and tts_text:
        snippet = tts_text[:40] + "..." if len(tts_text) > 40 else tts_text
        transcript_label.setStringValue_(snippet)

    # TTS controls
    skip_btn, stop_btn = tts_controls
    tts_visible = state_str == "speaking"
    skip_btn.setHidden_(not tts_visible)
    stop_btn.setHidden_(not tts_visible)

    # Mouse events: allow clicks only during speaking (for TTS buttons)
    window.setIgnoresMouseEvents_(state_str != "speaking")


# ---------------------------------------------------------------------------
# NSObject dispatcher for thread-safe UI updates
# ---------------------------------------------------------------------------

def _make_dispatcher_class(window, content_view, waveform_view, transcript_label, tts_controls, color_overlay):
    """Build a _Dispatcher NSObject that applies incoming IPC messages."""
    from Foundation import NSObject

    class _Dispatcher(NSObject):
        """Receives messages from the HUD socket server on the main thread.

        Called via performSelectorOnMainThread_withObject_waitUntilDone_.
        All AppKit mutations happen here, safely on the main thread.

        Requirement: HUD-08
        """

        def applyMessage_(self, msg_dict):
            msg_type = msg_dict.get("type", "")

            if msg_type == "state":
                state = msg_dict.get("state", "idle")
                _apply_state(
                    state, window, content_view,
                    waveform_view, transcript_label, tts_controls, color_overlay,
                )

            elif msg_type == "audio_level":
                level = msg_dict.get("level", 0.0)
                waveform_view.setLevel_(level)

            elif msg_type == "transcript":
                text = msg_dict.get("text", "")
                transcript_label.setStringValue_(text)
                transcript_label.setHidden_(False)

            elif msg_type == "tts_start":
                text = msg_dict.get("text", "")
                _apply_state(
                    "speaking", window, content_view,
                    waveform_view, transcript_label, tts_controls, color_overlay,
                    tts_text=text,
                )

            elif msg_type == "tts_end":
                _apply_state(
                    "idle", window, content_view,
                    waveform_view, transcript_label, tts_controls, color_overlay,
                )

            elif msg_type == "queue_update":
                pass  # v1: ignore; future: show badge count

            elif msg_type == "error":
                print(f"[HUD] Error from client: {msg_dict.get('message', '')}", file=sys.stderr)

    return _Dispatcher


# ---------------------------------------------------------------------------
# TTS button action handler
# ---------------------------------------------------------------------------

def _make_tts_action_class():
    from Foundation import NSObject

    class _TTSActionHandler(NSObject):
        """Writes TTS control commands to the command file."""

        def skipTTS_(self, sender):
            _write_tts_cmd("skip")

        def stopTTS_(self, sender):
            _write_tts_cmd("stop")

    return _TTSActionHandler


def _write_tts_cmd(cmd: str) -> None:
    """Write a TTS command to the command file (same IPC as CLI vox skip/stop)."""
    try:
        # Import inside handler to avoid top-level vox import failure
        # when running overlay.py standalone without full vox package.
        from vox.constants import TTS_CMD_FILE
        cmd_path = TTS_CMD_FILE
    except ImportError:
        cmd_path = "/tmp/vox-tts-cmd"
    try:
        with open(cmd_path, "w") as f:
            f.write(cmd)
    except OSError as e:
        print(f"[HUD] Failed to write TTS command '{cmd}': {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    """Launch the HUD overlay NSApplication.

    Builds the frosted-glass pill window, starts the HUDServer on a daemon
    thread, installs SIGTERM/SIGINT handlers, and runs the AppKit event loop.

    Requirements: HUD-01 through HUD-08
    """
    # ---- AppKit imports (lazy — must be inside main() for standalone use) ----
    from AppKit import (
        NSApplication, NSWindow, NSColor, NSView,
        NSWindowStyleMaskBorderless, NSScreen, NSBackingStoreBuffered,
        NSStatusWindowLevel, NSVisualEffectView,
        NSTextField, NSButton,
        NSTextAlignmentCenter, NSAnimationContext,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowCollectionBehaviorStationary,
        NSWindowCollectionBehaviorIgnoresCycle,
    )
    from Foundation import NSObject, NSTimer, NSMakeRect

    try:
        from AppKit import NSVisualEffectMaterialHUDWindow as HUD_MATERIAL
    except ImportError:
        HUD_MATERIAL = 23  # Raw enum value, stable since macOS 10.11

    from vox.hud.ipc import HUDServer, DEFAULT_SOCKET_PATH

    # ---- Application setup ----
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(2)  # NSApplicationActivationPolicyProhibited — no dock icon

    # ---- Screen layout ----
    screen = NSScreen.mainScreen().frame()
    x = _centered_x(screen, PILL_W_IDLE)
    y = _pill_y(screen)

    # ---- NSWindow (borderless, status level, transparent) ----
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((x, y), (PILL_W_IDLE, PILL_H)),
        NSWindowStyleMaskBorderless,
        NSBackingStoreBuffered,
        False,
    )
    window.setLevel_(NSStatusWindowLevel + 1)
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    window.setIgnoresMouseEvents_(True)  # Click-through by default (idle state)

    # All Spaces + fullscreen apps (HUD-07)
    window.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces |
        NSWindowCollectionBehaviorFullScreenAuxiliary |
        NSWindowCollectionBehaviorStationary |
        NSWindowCollectionBehaviorIgnoresCycle
    )

    # ---- Frosted glass (NSVisualEffectView as content view) — HUD-06 ----
    ve = NSVisualEffectView.alloc().initWithFrame_(((0, 0), (PILL_W_IDLE, PILL_H)))
    ve.setMaterial_(HUD_MATERIAL)
    ve.setBlendingMode_(0)   # NSVisualEffectBlendingModeBehindWindow
    ve.setState_(1)          # NSVisualEffectStateActive
    ve.setWantsLayer_(True)
    ve.layer().setCornerRadius_(PILL_H / 2)  # cornerRadius = half height → pill shape (HUD-06)
    ve.layer().setMasksToBounds_(True)
    window.setContentView_(ve)

    # ---- Color overlay (semi-transparent tint for state colors) ----
    WaveformView = _make_waveform_view_class()
    HUDContentView = _make_content_view_class()

    color_overlay = NSView.alloc().initWithFrame_(((0, 0), (PILL_W_IDLE, PILL_H)))
    r, g, b, a = STATE_COLORS["idle"]
    color_overlay.setWantsLayer_(True)
    color_overlay.layer().setCornerRadius_(PILL_H / 2)
    color_overlay.layer().setMasksToBounds_(True)
    idle_color = NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 0.3)
    color_overlay.setBackgroundColor_(idle_color)
    ve.addSubview_(color_overlay)

    # ---- HUDContentView (selective hit-testing for mixed click-through) ----
    content_view = HUDContentView.alloc().initWithFrame_(((0, 0), (PILL_W_IDLE, PILL_H)))
    ve.addSubview_(content_view)

    # ---- Waveform view — HUD-02 ----
    wf_margin = 8
    wf_w = PILL_W_ACTIVE - wf_margin * 2 - 80  # leave room for buttons
    wf_h = PILL_H - 8
    wf_x = wf_margin
    wf_y = (PILL_H - wf_h) / 2
    waveform_view = WaveformView.alloc().initWithFrame_(((wf_x, wf_y), (wf_w, wf_h)))
    waveform_view.setHidden_(True)
    content_view.addSubview_(waveform_view)

    # ---- Transcript label — HUD-03 ----
    label_margin = 8
    label_w = PILL_W_ACTIVE - label_margin * 2 - 80
    label_h = PILL_H - 6
    label_y = (PILL_H - label_h) / 2
    transcript_label = NSTextField.alloc().initWithFrame_(
        ((label_margin, label_y), (label_w, label_h))
    )
    transcript_label.setEditable_(False)
    transcript_label.setSelectable_(False)
    transcript_label.setDrawsBackground_(False)
    transcript_label.setBezeled_(False)
    transcript_label.setTextColor_(NSColor.whiteColor())
    transcript_label.setAlignment_(NSTextAlignmentCenter)
    transcript_label.setStringValue_("")
    transcript_label.setHidden_(True)
    content_view.addSubview_(transcript_label)

    # ---- TTS control buttons — HUD-04 ----
    TTSActionHandler = _make_tts_action_class()
    tts_handler = TTSActionHandler.alloc().init()

    btn_w = 36
    btn_h = 20
    btn_y = (PILL_H - btn_h) / 2
    btn_gap = 4
    btn_margin_right = 8
    stop_x = PILL_W_ACTIVE - btn_margin_right - btn_w
    skip_x = stop_x - btn_gap - btn_w

    skip_btn = NSButton.alloc().initWithFrame_(((skip_x, btn_y), (btn_w, btn_h)))
    skip_btn.setTitle_("Skip")
    skip_btn.setBezelStyle_(0)   # NSBezelStyleSmallSquare / bezel-less
    skip_btn.setBordered_(False)
    skip_btn.setFont_(
        __import__("AppKit", fromlist=["NSFont"]).NSFont.systemFontOfSize_(11)
    )
    skip_btn.setTarget_(tts_handler)
    skip_btn.setAction_("skipTTS:")
    skip_btn.setHidden_(True)

    stop_btn = NSButton.alloc().initWithFrame_(((stop_x, btn_y), (btn_w, btn_h)))
    stop_btn.setTitle_("Stop")
    stop_btn.setBezelStyle_(0)
    stop_btn.setBordered_(False)
    stop_btn.setFont_(
        __import__("AppKit", fromlist=["NSFont"]).NSFont.systemFontOfSize_(11)
    )
    stop_btn.setTarget_(tts_handler)
    stop_btn.setAction_("stopTTS:")
    stop_btn.setHidden_(True)

    content_view.addSubview_(skip_btn)
    content_view.addSubview_(stop_btn)

    tts_controls = (skip_btn, stop_btn)

    # ---- Dispatcher (thread-safe UI updates) ----
    DispatcherClass = _make_dispatcher_class(
        window, content_view, waveform_view, transcript_label, tts_controls, color_overlay
    )
    dispatcher = DispatcherClass.alloc().init()

    # ---- HUD IPC server ----
    def on_message(msg: dict) -> None:
        """Called on background socket thread — dispatch to main thread."""
        dispatcher.performSelectorOnMainThread_withObject_waitUntilDone_(
            "applyMessage:", msg, False
        )

    hud_server = HUDServer(path=DEFAULT_SOCKET_PATH, on_message=on_message)
    hud_server.start()

    # ---- Show window ----
    window.orderFrontRegardless()

    # ---- SIGTERM / SIGINT handler (NSTimer pattern — proven in codebase) ----
    class Terminator(NSObject):
        def terminate_(self, timer):
            hud_server.shutdown()
            app.terminate_(None)

    terminator = Terminator.alloc().init()

    def handle_signal(signum, frame):
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.0, terminator, "terminate:", None, False
        )

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # ---- Run loop ----
    app.run()


if __name__ == "__main__":
    main()
