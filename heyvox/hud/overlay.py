"""
HUD overlay process for Vox voice layer.

Implements a frosted-glass pill window positioned at the top-right of the
main screen (avoiding the macOS notch/camera area), communicating voice
state visually.

State machine:
- idle:       compact gray pill (12x12), click-through, no content
- listening:  expanded red pill (200x28), waveform amplitude bars
- processing: expanded amber pill (200x28), "Transcribing..." label
- speaking:   expanded green pill (200x28), text snippet + Skip/Stop buttons

IPC: HUDServer receives JSON messages over /tmp/heyvox-hud.sock on a daemon
thread and dispatches state changes to the main AppKit thread via
performSelectorOnMainThread_withObject_waitUntilDone_.

Requirements: HUD-01 through HUD-08
"""

import json
import os
import signal
import sys


# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

PILL_W = 100
PILL_H = 28
PILL_MARGIN_TOP = 8
PILL_MARGIN_RIGHT = 16  # Default distance from right edge of screen
ANIM_DURATION = 0.2
POSITION_FILE = "/tmp/heyvox-hud-position.json"  # Persists user-dragged position
_MENU_BAR_ONLY = False  # Set by main() — when True, only show menu bar icon, no pill

# State → (r, g, b, a) overlay color (semi-transparent so frosted glass shows)
STATE_COLORS = {
    "idle":       (0.35, 0.35, 0.40, 0.65),  # Subtle gray
    "listening":  (1.0, 0.2, 0.2, 0.8),
    "processing": (1.0, 0.7, 0.0, 0.8),
    "speaking":   (0.2, 0.8, 0.3, 0.8),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_position(screen_frame, pill_w, pill_h):
    """Return default (x, y) — top-right of screen, avoiding notch."""
    x = screen_frame.origin.x + screen_frame.size.width - pill_w - PILL_MARGIN_RIGHT
    y = screen_frame.origin.y + screen_frame.size.height - pill_h - PILL_MARGIN_TOP
    return x, y


def _load_position():
    """Load user-dragged position from disk. Returns (x, y) or None."""
    try:
        with open(POSITION_FILE) as f:
            data = json.load(f)
        return float(data["x"]), float(data["y"])
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return None


def _save_position(x, y):
    """Persist user-dragged position to disk."""
    try:
        with open(POSITION_FILE, "w") as f:
            json.dump({"x": x, "y": y}, f)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Custom NSView subclasses (defined inside main() to ensure AppKit is loaded)
# ---------------------------------------------------------------------------

def _make_comm_badge_view_class():
    """Create an NSView subclass that draws the TNG Starfleet communicator badge."""
    from AppKit import NSView, NSColor, NSBezierPath, NSFont, NSFontAttributeName, \
        NSForegroundColorAttributeName, NSParagraphStyleAttributeName
    from Foundation import NSMakeRect, NSDictionary, NSAttributedString
    import AppKit

    class CommBadgeView(NSView):
        def drawRect_(self, rect):
            w = rect.size.width
            h = rect.size.height
            cx = w / 2 - 10  # shift badge left to make room for "Vox"
            cy = h / 2

            # -- Background oval --
            oval_w = 26
            oval_h = 22
            oval_rect = NSMakeRect(cx - oval_w/2, cy - oval_h/2, oval_w, oval_h)
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.45, 0.35, 0.05, 0.5).set()
            NSBezierPath.bezierPathWithOvalInRect_(oval_rect).fill()

            # -- "V" chevron (our brand mark) --
            v = NSBezierPath.bezierPath()
            v.setLineWidth_(2.5)
            # Left arm of V
            v.moveToPoint_((cx - 7, cy + 8))
            v.lineToPoint_((cx, cy - 5))
            # Right arm of V
            v.lineToPoint_((cx + 7, cy + 8))
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.95, 0.78, 0.15, 1.0).set()
            v.stroke()

            # -- Small sound wave arcs (right side of V) --
            for i, radius in enumerate([4, 7]):
                arc = NSBezierPath.bezierPath()
                arc.setLineWidth_(1.5)
                # Quarter arc from ~30° to ~-30° (rightward sound waves)
                arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                    (cx + 3, cy + 1), radius, 40, -40, True
                )
                alpha = 0.7 - i * 0.25
                NSColor.colorWithCalibratedRed_green_blue_alpha_(0.95, 0.78, 0.15, alpha).set()
                arc.stroke()

            # -- "ox" text to complete "Vox" --
            font = NSFont.boldSystemFontOfSize_(12)
            style = AppKit.NSMutableParagraphStyle.alloc().init()
            style.setAlignment_(AppKit.NSTextAlignmentLeft)
            attrs = NSDictionary.dictionaryWithObjects_forKeys_(
                [font, NSColor.whiteColor(), style],
                [NSFontAttributeName, NSForegroundColorAttributeName,
                 NSParagraphStyleAttributeName],
            )
            text = NSAttributedString.alloc().initWithString_attributes_("ox", attrs)
            text.drawAtPoint_((cx + oval_w/2 + 2, cy - 8))

    return CommBadgeView


def _make_waveform_view_class():
    from AppKit import NSView, NSColor, NSBezierPath

    _HISTORY_SIZE = 64  # ~3.2 seconds at 20fps

    class WaveformView(NSView):
        """Scrolling waveform — mirrored amplitude history, like Voice Memos.

        Keeps a ring buffer of recent audio levels. New samples push in from
        the right, old ones scroll left. Drawn as a mirrored filled area
        around the vertical center, with a subtle gradient fade on older
        samples.
        """
        _level = 0.0
        _history = None  # Lazily initialized list of floats
        _smoothed = 0.0  # Exponentially smoothed current level

        def setLevel_(self, level):
            level = max(0.0, min(1.0, level))
            # Exponential smoothing: fast attack (0.6), slow release (0.15)
            alpha = 0.6 if level > self._smoothed else 0.15
            self._smoothed = alpha * level + (1.0 - alpha) * self._smoothed
            if self._history is None:
                self._history = [0.0] * _HISTORY_SIZE
            self._history.append(self._smoothed)
            if len(self._history) > _HISTORY_SIZE:
                self._history.pop(0)
            self.setNeedsDisplay_(True)

        def drawRect_(self, rect):
            if self._history is None:
                return

            history = self._history
            n = len(history)
            if n == 0:
                return

            w = rect.size.width
            h = rect.size.height
            ox = rect.origin.x
            oy = rect.origin.y
            cy = oy + h / 2.0  # vertical center
            step = w / max(1, n - 1)
            min_amp = h * 0.04  # minimum visible amplitude

            # Draw mirrored filled waveform
            top_path = NSBezierPath.bezierPath()
            bot_path = NSBezierPath.bezierPath()
            top_path.moveToPoint_((ox, cy))
            bot_path.moveToPoint_((ox, cy))

            for i, val in enumerate(history):
                x = ox + i * step
                amp = max(min_amp, val * (h / 2.0) * 0.9)
                top_path.lineToPoint_((x, cy + amp))
                bot_path.lineToPoint_((x, cy - amp))

            # Close paths back to center
            top_path.lineToPoint_((ox + (n - 1) * step, cy))
            top_path.lineToPoint_((ox, cy))
            bot_path.lineToPoint_((ox + (n - 1) * step, cy))
            bot_path.lineToPoint_((ox, cy))

            # Fill with white, higher opacity on recent samples
            NSColor.whiteColor().colorWithAlphaComponent_(0.85).setFill()
            top_path.fill()
            bot_path.fill()

            # Thin center line
            NSColor.whiteColor().colorWithAlphaComponent_(0.3).setStroke()
            center_line = NSBezierPath.bezierPath()
            center_line.moveToPoint_((ox, cy))
            center_line.lineToPoint_((ox + w, cy))
            center_line.setLineWidth_(0.5)
            center_line.stroke()

    return WaveformView


def _make_content_view_class():
    """Create HUDContentView — draggable background, buttons respond."""
    from AppKit import NSView
    import objc

    class HUDContentView(NSView):
        """Content view that supports dragging and click-to-menu.

        Dragging the background moves the window and saves the position.
        Clicking without drag shows the transcript dropdown menu.
        Clicking on button subviews works normally (TTS controls).

        Requirement: HUD-04
        """
        _drag_origin = None
        _drag_started = False
        _menu_callback = None  # Set from main() — callable(event)

        def mouseDown_(self, event):
            self._drag_origin = event.locationInWindow()
            self._drag_started = False

        def mouseDragged_(self, event):
            if self._drag_origin is None:
                return
            self._drag_started = True
            win = self.window()
            if win is None:
                return
            screen_loc = event.locationInWindow()
            frame = win.frame()
            dx = screen_loc.x - self._drag_origin.x
            dy = screen_loc.y - self._drag_origin.y
            new_x = frame.origin.x + dx
            new_y = frame.origin.y + dy
            win.setFrameOrigin_((new_x, new_y))

        def mouseUp_(self, event):
            if not self._drag_started and self._menu_callback:
                self._menu_callback(event)
            elif self._drag_started:
                win = self.window()
                if win:
                    _save_position(win.frame().origin.x, win.frame().origin.y)
            self._drag_origin = None
            self._drag_started = False

        def hitTest_(self, point):
            hit = objc.super(HUDContentView, self).hitTest_(point)
            if hit is self:
                return self   # Background captures mouse for dragging
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
    idle_label=None,
    tts_text=None,
    status_item=None,
    update_status_menu=None,
):
    """Apply HUD visual state on the main thread.

    Requirements: HUD-01 (pill), HUD-02 (waveform), HUD-03 (transcript),
                  HUD-04 (TTS controls), HUD-05 (colors)
    """
    from AppKit import NSAnimationContext, NSColor, NSScreen

    # Update menu bar status icon + label
    _STATUS_LABELS = {
        "idle":       ("\U0001f399", ""),                # 🎙 (icon only)
        "listening":  ("\U0001f534", " Recording..."),   # 🔴 Recording...
        "processing": ("\U0001f7e1", " Transcribing..."),# 🟡 Transcribing...
        "speaking":   ("\U0001f7e2", " Speaking..."),    # 🟢 Speaking...
    }
    if status_item is not None:
        icon, label = _STATUS_LABELS.get(state_str, _STATUS_LABELS["idle"])
        status_item.button().setTitle_(f"{icon}{label}")
        # Refresh menu on state change (updates transcript list, mute state)
        if update_status_menu is not None:
            update_status_menu()

    r, g, b, a = STATE_COLORS.get(state_str, STATE_COLORS["idle"])
    color = NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)
    color_overlay.setBackgroundColor_(color)
    color_overlay.setNeedsDisplay_(True)

    screen = NSScreen.mainScreen().frame()
    is_active = state_str in ("listening", "processing", "speaking")

    # In menu-bar-only mode, the pill is never shown.
    # Otherwise, show pill always — idle uses compact size, active uses active size.
    if _MENU_BAR_ONLY:
        window.orderOut_(None)
    else:
        saved = _load_position()
        if saved:
            x, y = saved
        else:
            x, y = _default_position(screen, PILL_W, PILL_H)

        if not window.isVisible():
            window.orderFrontRegardless()

        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(ANIM_DURATION)
        window.animator().setFrame_display_(((x, y), (PILL_W, PILL_H)), True)
        NSAnimationContext.endGrouping()

        content_view.setFrame_(((0, 0), (PILL_W, PILL_H)))
        color_overlay.setFrame_(((0, 0), (PILL_W, PILL_H)))

        ve = window.contentView()
        if ve and ve.layer():
            ve.layer().setCornerRadius_(PILL_H / 2)
        if color_overlay.layer():
            color_overlay.layer().setCornerRadius_(PILL_H / 2)

    # Show/hide idle label
    if idle_label is not None:
        idle_label.setHidden_(is_active)
        # Show temporary status text (e.g. "Sent to Conductor") then revert
        if not is_active and tts_text:
            idle_label.setStringValue_(tts_text)
            # Schedule revert to default label after 3 seconds
            from Foundation import NSTimer
            def _revert_label(timer):
                idle_label.setStringValue_("\U0001f399 HeyVox")
            NSTimer.scheduledTimerWithTimeInterval_repeats_block_(3.0, False, _revert_label)
        elif not is_active:
            idle_label.setStringValue_("\U0001f399 HeyVox")

    if not is_active:
        # Idle: show label, hide active elements
        waveform_view.setHidden_(True)
        transcript_label.setHidden_(True)
        skip_btn, stop_btn = tts_controls
        skip_btn.setHidden_(True)
        stop_btn.setHidden_(True)
        return

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

    # Clickable in all states
    window.setIgnoresMouseEvents_(False)


# ---------------------------------------------------------------------------
# NSObject dispatcher for thread-safe UI updates
# ---------------------------------------------------------------------------

def _make_dispatcher_class(window, content_view, waveform_view, transcript_label, tts_controls, color_overlay, idle_label=None, status_item=None, update_status_menu=None):
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
                text = msg_dict.get("text")
                _apply_state(
                    state, window, content_view,
                    waveform_view, transcript_label, tts_controls, color_overlay,
                    idle_label=idle_label, tts_text=text,
                    status_item=status_item,
                    update_status_menu=update_status_menu,
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
                    idle_label=idle_label, tts_text=text,
                    status_item=status_item, update_status_menu=update_status_menu,
                )

            elif msg_type == "tts_end":
                _apply_state(
                    "idle", window, content_view,
                    waveform_view, transcript_label, tts_controls, color_overlay,
                    idle_label=idle_label,
                    status_item=status_item, update_status_menu=update_status_menu,
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
    """Write a TTS command to the command file (same IPC as CLI heyvox skip/stop)."""
    try:
        # Import inside handler to avoid top-level vox import failure
        # when running overlay.py standalone without full vox package.
        from heyvox.constants import TTS_CMD_FILE
        cmd_path = TTS_CMD_FILE
    except ImportError:
        cmd_path = "/tmp/heyvox-tts-cmd"
    try:
        tmp_path = cmd_path + ".tmp"
        with open(tmp_path, "w") as f:
            f.write(cmd)
        os.rename(tmp_path, cmd_path)
    except OSError as e:
        print(f"[HUD] Failed to write TTS command '{cmd}': {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Transcript dropdown menu
# ---------------------------------------------------------------------------

def _make_menu_action_class():
    """NSObject handler for transcript menu item actions."""
    from Foundation import NSObject
    from AppKit import NSPasteboard, NSPasteboardTypeString

    _TTS_MUTE_FLAGS = ["/tmp/claude-tts-mute", "/tmp/herald-mute"]

    class _MenuActionHandler(NSObject):
        def copyTranscript_(self, sender):
            text = sender.representedObject()
            if text:
                pb = NSPasteboard.generalPasteboard()
                pb.clearContents()
                pb.setString_forType_(text, NSPasteboardTypeString)

        def toggleMute_(self, sender):
            from heyvox.audio.tts import is_muted, set_muted
            currently_muted = is_muted()
            set_muted(not currently_muted)
            sender.setState_(0 if currently_muted else 1)

        def setVerbosity_(self, sender):
            """Set verbosity to the level stored in the menu item's representedObject.

            Delegates entirely to set_verbosity() which handles file flags,
            in-memory state, and cross-process sync.
            """
            try:
                level = sender.representedObject()
                if level:
                    from heyvox.audio.tts import set_verbosity
                    set_verbosity(level)
                    # Persist to config so it survives restarts
                    from heyvox.config import update_config
                    update_config(**{"tts.verbosity": level})
            except Exception:
                pass

        def setTTSStyle_(self, sender):
            """Set TTS style. Persists to config for cross-session consistency."""
            try:
                style = sender.representedObject()
                if style:
                    from heyvox.audio.tts import set_tts_style
                    set_tts_style(style)
            except Exception:
                pass

        def switchMic_(self, sender):
            """Write mic switch request file for main.py to pick up (atomic)."""
            device_name = sender.representedObject()
            if device_name:
                try:
                    from heyvox.constants import MIC_SWITCH_REQUEST_FILE
                    tmp_path = MIC_SWITCH_REQUEST_FILE + ".tmp"
                    with open(tmp_path, "w") as f:
                        f.write(device_name)
                    os.rename(tmp_path, MIC_SWITCH_REQUEST_FILE)
                except Exception:
                    pass

        def switchOutput_(self, sender):
            """Switch macOS system default output device via CoreAudio."""
            device_id = sender.representedObject()
            if device_id is not None:
                try:
                    from heyvox.audio.output import set_default_output_device
                    set_default_output_device(device_id)
                except Exception:
                    pass

        def openLog_(self, sender):
            import subprocess
            try:
                subprocess.run(["open", "-a", "Console", "/tmp/heyvox.log"])
            except Exception:
                pass

        def openConfig_(self, sender):
            import subprocess
            cfg = os.path.expanduser("~/.config/heyvox/config.yaml")
            if os.path.exists(cfg):
                try:
                    subprocess.run(["open", cfg])
                except Exception:
                    pass

        def openHelp_(self, sender):
            import webbrowser
            webbrowser.open("https://heyvox.dev")

        def toggleOverlay_(self, sender):
            """Toggle the floating pill overlay on/off at runtime. Persists to config."""
            global _MENU_BAR_ONLY
            _MENU_BAR_ONLY = not _MENU_BAR_ONLY
            if _MENU_BAR_ONLY:
                sender.setState_(0)  # NSOffState — no checkmark
                # Hide the pill window
                from AppKit import NSApplication
                for w in NSApplication.sharedApplication().windows():
                    if hasattr(w, 'isMainWindow') and not w.isMainWindow():
                        w.orderOut_(None)
            else:
                sender.setState_(1)  # NSOnState — checkmark
                # Immediately show the pill window
                from AppKit import NSApplication
                for w in NSApplication.sharedApplication().windows():
                    if hasattr(w, 'isMainWindow') and not w.isMainWindow():
                        w.orderFrontRegardless()
            # Persist to config so it survives restarts
            try:
                from heyvox.config import update_config
                update_config(hud_menu_bar_only=_MENU_BAR_ONLY)
            except Exception:
                pass

        def restartHeyVox_(self, sender):
            """Kill heyvox.main, relaunch it (which spawns a new overlay), then quit this overlay."""
            import subprocess
            import sys
            import time
            import os
            import signal as _sig

            # Send SIGTERM to main process and wait for clean shutdown
            # (atexit handler releases PID lock). Avoid pkill which can
            # also kill the newly spawned process.
            pid_file = "/tmp/heyvox.pid"
            old_pid = 0
            try:
                with open(pid_file) as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, _sig.SIGTERM)
            except (FileNotFoundError, ValueError, ProcessLookupError):
                # No PID file or process already dead — try pkill as fallback
                subprocess.run(["pkill", "-f", "heyvox.main"], capture_output=True)

            # Wait for process to exit and release PID lock
            if old_pid:
                for _ in range(20):  # up to 2 seconds
                    time.sleep(0.1)
                    try:
                        os.kill(old_pid, 0)  # Check if still alive
                    except ProcessLookupError:
                        break  # Process exited
                else:
                    # Force kill if still alive after 2s
                    try:
                        os.kill(old_pid, _sig.SIGKILL)
                    except ProcessLookupError:
                        pass
                    time.sleep(0.3)

            # Clean up stale PID file in case atexit didn't run
            try:
                os.unlink(pid_file)
            except FileNotFoundError:
                pass

            # Relaunch main process — it will spawn its own overlay.
            # Log stderr so startup failures are diagnosable.
            restart_log = open("/tmp/heyvox-restart.log", "w")
            proc = subprocess.Popen(
                [sys.executable, "-m", "heyvox.main"],
                stdout=subprocess.DEVNULL, stderr=restart_log,
                start_new_session=True,  # Detach so our exit doesn't kill it
            )

            # Wait briefly and verify the new process is alive before quitting
            time.sleep(0.5)
            if proc.poll() is not None:
                # New process already exited — don't quit overlay so user
                # still has the menu bar icon and can see something went wrong.
                restart_log.close()
                return

            # Quit this overlay — the new main process launches a fresh one
            from AppKit import NSApplication
            NSApplication.sharedApplication().terminate_(None)

        def quitHeyVox_(self, sender):
            """Send SIGTERM to parent heyvox.main process, then quit overlay."""
            import os
            import signal as _sig
            try:
                with open("/tmp/heyvox.pid") as f:
                    pid = int(f.read().strip())
                os.kill(pid, _sig.SIGTERM)
            except (FileNotFoundError, ValueError, ProcessLookupError):
                import subprocess
                subprocess.run(["pkill", "-f", "heyvox.main"], capture_output=True)
            from AppKit import NSApplication
            NSApplication.sharedApplication().terminate_(None)

    return _MenuActionHandler


def _build_transcript_menu(handler):
    """Build a compact NSMenu for the HeyVox menu bar icon.

    Layout (Option B — minimal + settings gear):
    1. Status summary line (mic · verbosity · queue)
    2. Recent transcripts (last 3, click to copy)
    3. Mute TTS toggle
    4. Settings submenu (Verbosity, Microphone, Overlay, Voice Cmds, Status)
    5. Restart / Quit

    Args:
        handler: An instance of _MenuActionHandler for action targets.
    """
    from AppKit import NSMenu, NSMenuItem, NSFont, NSAttributedString, NSColor
    from AppKit import NSFontAttributeName, NSForegroundColorAttributeName
    from Foundation import NSDictionary
    import glob as _glob

    menu = NSMenu.alloc().init()
    menu.setAutoenablesItems_(False)
    menu.setMinimumWidth_(200)

    _font = NSFont.systemFontOfSize_(13)
    _font_small = NSFont.systemFontOfSize_(12)
    _font_bold = NSFont.boldSystemFontOfSize_(12)
    _dimmed = NSColor.secondaryLabelColor()

    def _styled(item, title=None):
        t = title if title else item.title()
        attrs = NSDictionary.dictionaryWithObject_forKey_(_font, "NSFont")
        item.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(t, attrs))
        return item

    def _dimmed_item(title):
        attrs = NSDictionary.dictionaryWithObjects_forKeys_(
            [_font_small, _dimmed],
            [NSFontAttributeName, NSForegroundColorAttributeName],
        )
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        item.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(title, attrs))
        item.setEnabled_(False)
        return item

    # -- Gather state --
    queue_count = len(_glob.glob("/tmp/herald-queue/*.wav"))
    hold_count = len(_glob.glob("/tmp/herald-hold/*.wav"))
    try:
        from heyvox.audio.tts import is_muted as _tts_is_muted
        _is_muted = _tts_is_muted()
    except Exception:
        _is_muted = os.path.exists("/tmp/claude-tts-mute") or os.path.exists("/tmp/herald-mute")

    try:
        from heyvox.audio.tts import get_verbosity
        current_verbosity = get_verbosity()
    except Exception:
        current_verbosity = "full"

    _active_mic = ""
    try:
        from heyvox.constants import ACTIVE_MIC_FILE
        with open(ACTIVE_MIC_FILE) as _mf:
            _active_mic = _mf.read().strip()
    except Exception:
        pass

    # Friendly mic name for display
    def _friendly_mic(name):
        if not name:
            return "None"
        n = name
        # "MacBook Pro Microphone" → "Built-in"
        if "macbook" in n.lower() and "microphone" in n.lower():
            return "Built-in"
        # Strip generic suffixes
        for suffix in [" Gaming Headset", " Wireless Gaming Headset", " Microphone",
                       " USB Audio", " Audio Device"]:
            if n.endswith(suffix):
                n = n[:-len(suffix)]
                break
        return n.strip()

    _mic_short = _friendly_mic(_active_mic)

    # ── Section 1: Microphone (top-level with switch submenu) ──
    try:
        import pyaudio as _pa_mod
        _scan = _pa_mod.PyAudio()
        _input_devices = []
        for _di in range(_scan.get_device_count()):
            try:
                _d = _scan.get_device_info_by_index(_di)
                if _d['maxInputChannels'] > 0:
                    _input_devices.append(_d['name'])
            except Exception:
                pass
        _scan.terminate()
    except Exception:
        _input_devices = []

    if not _input_devices and _active_mic:
        _input_devices = [_active_mic]

    mic_parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"\U0001f399 Mic: {_mic_short}", None, "",
    )
    _styled(mic_parent)
    mic_sub = NSMenu.alloc().init()
    mic_sub.setAutoenablesItems_(False)

    for _dev_name in _input_devices:
        _is_active = _dev_name == _active_mic
        _mic_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            _friendly_mic(_dev_name), "switchMic:", "",
        )
        _mic_item.setTarget_(handler)
        _mic_item.setRepresentedObject_(_dev_name)
        _mic_item.setEnabled_(not _is_active)
        if _is_active:
            _mic_item.setState_(1)
        _styled(_mic_item)
        mic_sub.addItem_(_mic_item)

    if not _input_devices:
        _no_mic = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "No devices found", None, "",
        )
        _no_mic.setEnabled_(False)
        _styled(_no_mic)
        mic_sub.addItem_(_no_mic)

    mic_parent.setSubmenu_(mic_sub)
    menu.addItem_(mic_parent)

    # ── Section 1b: Speaker / Output device (system default switch) ──
    try:
        from heyvox.audio.output import list_output_devices, friendly_output_name
        _output_devices = list_output_devices()
    except Exception:
        _output_devices = []

    if _output_devices:
        _active_output = next((d for d in _output_devices if d.is_default), None)
        _output_short = friendly_output_name(_active_output.name) if _active_output else "System Default"

        output_parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"\U0001f508 Output: {_output_short}", None, "",
        )
        _styled(output_parent)
        output_sub = NSMenu.alloc().init()
        output_sub.setAutoenablesItems_(False)

        for _out_dev in _output_devices:
            _out_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                friendly_output_name(_out_dev.name), "switchOutput:", "",
            )
            _out_item.setTarget_(handler)
            _out_item.setRepresentedObject_(_out_dev.device_id)
            _out_item.setEnabled_(not _out_dev.is_default)
            if _out_dev.is_default:
                _out_item.setState_(1)
            _styled(_out_item)
            output_sub.addItem_(_out_item)

        output_parent.setSubmenu_(output_sub)
        menu.addItem_(output_parent)

    # ── Section 2: Speech (verbosity + style in one submenu) ──
    from heyvox.audio.tts import get_tts_style
    current_style = get_tts_style()
    _TTS_LABELS = {
        "full": "Speak All", "short": "First Sentence", "skip": "Mute",
    }
    _STYLE_LABELS = {
        "detailed": "Detailed", "concise": "Concise",
        "technical": "Technical", "casual": "Casual",
    }
    voice_parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"\U0001f4ac Speech: {_TTS_LABELS.get(current_verbosity, 'Speak All')} \u00b7 {_STYLE_LABELS.get(current_style, 'Detailed')}", None, "",
    )
    _styled(voice_parent)
    voice_sub = NSMenu.alloc().init()
    voice_sub.setAutoenablesItems_(False)

    # -- Output mode --
    header_output = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Output", None, "")
    header_output.setEnabled_(False)
    _styled(header_output)
    voice_sub.addItem_(header_output)
    for level, label in [
        ("full", "Speak All"),
        ("short", "First Sentence"),
        ("skip", "Mute"),
    ]:
        v_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"  {label}", "setVerbosity:", "",
        )
        v_item.setTarget_(handler)
        v_item.setRepresentedObject_(level)
        v_item.setEnabled_(True)
        if level == current_verbosity:
            v_item.setState_(1)
        _styled(v_item)
        voice_sub.addItem_(v_item)

    voice_sub.addItem_(NSMenuItem.separatorItem())

    # -- Style --
    header_style = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Style", None, "")
    header_style.setEnabled_(False)
    _styled(header_style)
    voice_sub.addItem_(header_style)
    for style_key, style_desc in [
        ("detailed", "Detailed"),
        ("concise", "Concise"),
        ("technical", "Technical"),
        ("casual", "Casual"),
    ]:
        s_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"  {style_desc}", "setTTSStyle:", "",
        )
        s_item.setTarget_(handler)
        s_item.setRepresentedObject_(style_key)
        s_item.setEnabled_(True)
        if style_key == current_style:
            s_item.setState_(1)
        _styled(s_item)
        voice_sub.addItem_(s_item)

    voice_parent.setSubmenu_(voice_sub)
    menu.addItem_(voice_parent)

    menu.addItem_(NSMenuItem.separatorItem())

    # ── Section 3: Recent transcripts (last 3, click to copy) ──
    try:
        from heyvox.history import load as _load_history
        entries = _load_history(limit=3)
    except Exception:
        entries = []

    if entries:
        for entry in entries:
            text = entry.get("text", "")
            ts = entry.get("ts", "?")
            time_part = ts[-8:-3] if len(ts) >= 8 else ts
            display = text[:30] + "\u2026" if len(text) > 30 else text
            title = f"  {time_part}  {display}"
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, "copyTranscript:", "",
            )
            item.setTarget_(handler)
            item.setRepresentedObject_(text)
            item.setToolTip_(text)
            item.setEnabled_(True)
            _styled(item)
            menu.addItem_(item)
        menu.addItem_(NSMenuItem.separatorItem())

    # ── Section 3: Settings submenu ──
    settings_parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Settings", None, "",
    )
    _styled(settings_parent)
    settings_sub = NSMenu.alloc().init()
    settings_sub.setAutoenablesItems_(False)

    # 4a: Show Overlay toggle
    overlay_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Show Overlay", "toggleOverlay:", "",
    )
    overlay_item.setTarget_(handler)
    overlay_item.setEnabled_(True)
    if not _MENU_BAR_ONLY:
        overlay_item.setState_(1)
    _styled(overlay_item)
    settings_sub.addItem_(overlay_item)

    settings_sub.addItem_(NSMenuItem.separatorItem())

    # 4d: Voice Commands reference
    cmds_parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Voice Commands", None, "",
    )
    _styled(cmds_parent)
    cmds_sub = NSMenu.alloc().init()
    cmds_sub.setAutoenablesItems_(False)

    def _cmd_item(phrase, desc):
        title = f"  {phrase}  \u2014  {desc}"
        attrs = NSDictionary.dictionaryWithObjects_forKeys_(
            [_font_small, NSColor.labelColor()],
            [NSFontAttributeName, NSForegroundColorAttributeName],
        )
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        item.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(title, attrs))
        item.setEnabled_(True)
        return item

    def _section_header(title):
        attrs = NSDictionary.dictionaryWithObjects_forKeys_(
            [_font_bold, _dimmed],
            [NSFontAttributeName, NSForegroundColorAttributeName],
        )
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        item.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(title, attrs))
        item.setEnabled_(True)
        return item

    cmds_sub.addItem_(_section_header("Playback"))
    cmds_sub.addItem_(_cmd_item('"skip"', "Skip current"))
    cmds_sub.addItem_(_cmd_item('"stop"', "Stop all"))
    cmds_sub.addItem_(_cmd_item('"mute"', "Toggle mute"))
    cmds_sub.addItem_(_cmd_item('"replay"', "Replay last"))
    cmds_sub.addItem_(NSMenuItem.separatorItem())
    cmds_sub.addItem_(_section_header("TTS Playback"))
    cmds_sub.addItem_(_cmd_item('"be quiet"', "First sentence only"))
    cmds_sub.addItem_(_cmd_item('"speak normally"', "Speak all"))
    cmds_sub.addItem_(_cmd_item('"shut up"', "Mute"))
    cmds_parent.setSubmenu_(cmds_sub)
    settings_sub.addItem_(cmds_parent)

    settings_sub.addItem_(NSMenuItem.separatorItem())

    # 4e: Status (daemons + queue)
    def _pid_alive(pidfile):
        try:
            with open(pidfile) as _f:
                pid = int(_f.read().strip())
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    orch_ok = _pid_alive("/tmp/herald-orchestrator.pid")
    kokoro_ok = os.path.exists("/tmp/kokoro-daemon.sock") and _pid_alive("/tmp/kokoro-daemon.pid")
    hud_ok = os.path.exists("/tmp/heyvox-hud.sock")

    def _status_item(name, ok):
        icon = "\U0001f7e2" if ok else "\U0001f534"
        label = "running" if ok else "stopped"
        title = f"  {icon} {name}: {label}"
        attrs = NSDictionary.dictionaryWithObjects_forKeys_(
            [_font_small, NSColor.labelColor()],
            [NSFontAttributeName, NSForegroundColorAttributeName],
        )
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        item.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(title, attrs))
        item.setEnabled_(True)
        return item

    settings_sub.addItem_(_status_item("Orchestrator", orch_ok))
    settings_sub.addItem_(_status_item("Kokoro TTS", kokoro_ok))
    settings_sub.addItem_(_status_item("HUD", hud_ok))

    if queue_count > 0 or hold_count > 0:
        q_title = f"  Queue: {queue_count} queued, {hold_count} held"
        settings_sub.addItem_(_dimmed_item(q_title))

    settings_sub.addItem_(NSMenuItem.separatorItem())

    # 4f: Help, Log, Config
    help_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Help", "openHelp:", "",
    )
    help_item.setTarget_(handler)
    help_item.setEnabled_(True)
    _styled(help_item)
    settings_sub.addItem_(help_item)

    log_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Open Log", "openLog:", "",
    )
    log_item.setTarget_(handler)
    log_item.setEnabled_(True)
    _styled(log_item)
    settings_sub.addItem_(log_item)

    settings_parent.setSubmenu_(settings_sub)
    menu.addItem_(settings_parent)

    menu.addItem_(NSMenuItem.separatorItem())

    # ── Section 5: Restart / Quit ──
    restart_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Restart", "restartHeyVox:", "",
    )
    restart_item.setTarget_(handler)
    restart_item.setEnabled_(True)
    _styled(restart_item)
    menu.addItem_(restart_item)

    try:
        from heyvox import __version__
        ver = __version__
    except Exception:
        ver = "0.1.0"
    quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"Quit HeyVox v{ver}", "quitHeyVox:", "",
    )
    quit_item.setTarget_(handler)
    quit_item.setEnabled_(True)
    _styled(quit_item)
    menu.addItem_(quit_item)

    return menu


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(menu_bar_only: bool = False):
    """Launch the HUD overlay NSApplication.

    Builds the frosted-glass pill window, starts the HUDServer on a daemon
    thread, installs SIGTERM/SIGINT handlers, and runs the AppKit event loop.

    Args:
        menu_bar_only: If True, only show the menu bar status icon (no floating pill).

    Requirements: HUD-01 through HUD-08
    """
    # ---- AppKit imports (lazy — must be inside main() for standalone use) ----
    from AppKit import (
        NSApplication, NSWindow, NSColor, NSView,
        NSWindowStyleMaskBorderless, NSScreen, NSBackingStoreBuffered,
        NSStatusWindowLevel, NSVisualEffectView,
        NSTextField, NSButton,
        NSTextAlignmentCenter, NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowCollectionBehaviorStationary,
        NSWindowCollectionBehaviorIgnoresCycle,
    )
    from Foundation import NSObject, NSTimer

    try:
        from AppKit import NSVisualEffectMaterialHUDWindow as HUD_MATERIAL
    except ImportError:
        HUD_MATERIAL = 23  # Raw enum value, stable since macOS 10.11

    from heyvox.hud.ipc import HUDServer, DEFAULT_SOCKET_PATH

    global _MENU_BAR_ONLY
    _MENU_BAR_ONLY = menu_bar_only

    # ---- Application setup ----
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(2)  # NSApplicationActivationPolicyProhibited — no dock icon

    # ---- Screen layout ----
    screen = NSScreen.mainScreen().frame()
    saved = _load_position()
    if saved:
        x, y = saved
    else:
        x, y = _default_position(screen, PILL_W, PILL_H)

    # ---- NSWindow (borderless, status level, transparent) ----
    # Starts hidden (idle) — shown on first active state
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((x, y), (PILL_W, PILL_H)),
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
    ve = NSVisualEffectView.alloc().initWithFrame_(((0, 0), (PILL_W, PILL_H)))
    ve.setMaterial_(HUD_MATERIAL)
    ve.setBlendingMode_(0)   # NSVisualEffectBlendingModeBehindWindow
    ve.setState_(1)          # NSVisualEffectStateActive
    ve.setWantsLayer_(True)
    ve.layer().setCornerRadius_(PILL_H / 2)  # pill shape (HUD-06)
    ve.layer().setMasksToBounds_(True)
    window.setContentView_(ve)

    # ---- Color overlay (semi-transparent tint for state colors) ----
    WaveformView = _make_waveform_view_class()
    HUDContentView = _make_content_view_class()

    color_overlay = NSView.alloc().initWithFrame_(((0, 0), (PILL_W, PILL_H)))
    r, g, b, a = STATE_COLORS["idle"]
    color_overlay.setWantsLayer_(True)
    color_overlay.layer().setCornerRadius_(PILL_H / 2)
    color_overlay.layer().setMasksToBounds_(True)
    idle_color = NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 0.3)
    color_overlay.setBackgroundColor_(idle_color)
    ve.addSubview_(color_overlay)

    # ---- HUDContentView (selective hit-testing for mixed click-through) ----
    content_view = HUDContentView.alloc().initWithFrame_(((0, 0), (PILL_W, PILL_H)))
    ve.addSubview_(content_view)

    # ---- Waveform view — HUD-02 ----
    # Sized for active state — hidden when idle
    wf_margin = 4
    wf_w = PILL_W - wf_margin * 2 - 40  # leave room for buttons
    wf_h = PILL_H - 4
    wf_x = wf_margin
    wf_y = (PILL_H - wf_h) / 2
    waveform_view = WaveformView.alloc().initWithFrame_(((wf_x, wf_y), (wf_w, wf_h)))
    waveform_view.setHidden_(True)
    content_view.addSubview_(waveform_view)

    # ---- Transcript label — HUD-03 ----
    label_margin = 4
    label_w = PILL_W - label_margin * 2 - 40
    label_h = PILL_H - 2
    label_y = (PILL_H - label_h) / 2
    transcript_label = NSTextField.alloc().initWithFrame_(
        ((label_margin, label_y), (label_w, label_h))
    )
    transcript_label.setEditable_(False)
    transcript_label.setSelectable_(False)
    transcript_label.setDrawsBackground_(False)
    transcript_label.setBezeled_(False)
    transcript_label.setTextColor_(NSColor.whiteColor())
    transcript_label.setFont_(
        __import__("AppKit", fromlist=["NSFont"]).NSFont.boldSystemFontOfSize_(11)
    )
    transcript_label.setAlignment_(NSTextAlignmentCenter)
    transcript_label.setStringValue_("")
    transcript_label.setHidden_(True)
    content_view.addSubview_(transcript_label)

    # ---- Idle label ("🎙 HeyVox" centered in idle pill) ----
    NSFont = __import__("AppKit", fromlist=["NSFont"]).NSFont
    idle_label_h = 18
    idle_label_y = (PILL_H - idle_label_h) / 2
    idle_label = NSTextField.alloc().initWithFrame_(
        ((0, idle_label_y), (PILL_W, idle_label_h))
    )
    idle_label.setEditable_(False)
    idle_label.setSelectable_(False)
    idle_label.setDrawsBackground_(False)
    idle_label.setBezeled_(False)
    idle_label.setTextColor_(NSColor.whiteColor())
    idle_label.setFont_(NSFont.boldSystemFontOfSize_(11))
    idle_label.setAlignment_(NSTextAlignmentCenter)
    idle_label.cell().setWraps_(False)
    idle_label.cell().setScrollable_(False)
    idle_label.setStringValue_("\U0001f399 HeyVox")
    idle_label.setHidden_(False)
    content_view.addSubview_(idle_label)

    # ---- TTS control buttons — HUD-04 ----
    TTSActionHandler = _make_tts_action_class()
    tts_handler = TTSActionHandler.alloc().init()

    btn_w = 22
    btn_h = 14
    btn_y = (PILL_H - btn_h) / 2
    btn_gap = 1
    btn_margin_right = 4
    stop_x = PILL_W - btn_margin_right - btn_w
    skip_x = stop_x - btn_gap - btn_w

    skip_btn = NSButton.alloc().initWithFrame_(((skip_x, btn_y), (btn_w, btn_h)))
    skip_btn.setTitle_("Skip")
    skip_btn.setBezelStyle_(0)   # NSBezelStyleSmallSquare / bezel-less
    skip_btn.setBordered_(False)
    skip_btn.setFont_(
        __import__("AppKit", fromlist=["NSFont"]).NSFont.systemFontOfSize_(7)
    )
    skip_btn.setTarget_(tts_handler)
    skip_btn.setAction_("skipTTS:")
    skip_btn.setHidden_(True)

    stop_btn = NSButton.alloc().initWithFrame_(((stop_x, btn_y), (btn_w, btn_h)))
    stop_btn.setTitle_("Stop")
    stop_btn.setBezelStyle_(0)
    stop_btn.setBordered_(False)
    stop_btn.setFont_(
        __import__("AppKit", fromlist=["NSFont"]).NSFont.systemFontOfSize_(7)
    )
    stop_btn.setTarget_(tts_handler)
    stop_btn.setAction_("stopTTS:")
    stop_btn.setHidden_(True)

    content_view.addSubview_(skip_btn)
    content_view.addSubview_(stop_btn)

    tts_controls = (skip_btn, stop_btn)

    # ---- Menu bar status item (lives next to Bluetooth/WiFi icons) ----
    from AppKit import NSStatusBar, NSVariableStatusItemLength
    status_bar = NSStatusBar.systemStatusBar()
    status_item = status_bar.statusItemWithLength_(NSVariableStatusItemLength)
    status_button = status_item.button()

    # State icons for menu bar — using Unicode text rendered as the icon
    _STATUS_ICONS = {
        "idle":       "\U0001f399",     # 🎙 mic
        "listening":  "\U0001f534",     # 🔴 red circle
        "processing": "\U0001f7e1",     # 🟡 yellow circle
        "speaking":   "\U0001f7e2",     # 🟢 green circle
    }
    status_button.setTitle_(_STATUS_ICONS["idle"])

    MenuActionHandler = _make_menu_action_class()
    menu_handler = MenuActionHandler.alloc().init()

    def _update_status_menu():
        """Rebuild and assign menu to status item (called on state change)."""
        menu = _build_transcript_menu(menu_handler)
        menu.setDelegate_(_menu_delegate)
        status_item.setMenu_(menu)

    def _rebuild_menu_contents(menu):
        """Rebuild menu items in-place (called by delegate on every open)."""
        menu.removeAllItems()
        fresh = _build_transcript_menu(menu_handler)
        for i in range(fresh.numberOfItems()):
            item = fresh.itemAtIndex_(0)
            fresh.removeItemAtIndex_(0)
            menu.addItem_(item)

    MenuDelegateClass = type("MenuDelegate", (NSObject,), {
        "menuNeedsUpdate_": lambda self, m: _rebuild_menu_contents(m),
    })
    _menu_delegate = MenuDelegateClass.alloc().init()

    _update_status_menu()

    # Also keep pill dropdown for floating window (click on pill during recording)
    def _show_dropdown(event):
        menu = _build_transcript_menu(menu_handler)
        loc_in_view = content_view.convertPoint_fromView_(
            event.locationInWindow(), None,
        )
        menu.popUpMenuPositioningItem_atLocation_inView_(
            None, loc_in_view, content_view,
        )

    content_view._menu_callback = _show_dropdown

    # ---- Dispatcher (thread-safe UI updates) ----
    DispatcherClass = _make_dispatcher_class(
        window, content_view, waveform_view, transcript_label, tts_controls, color_overlay,
        idle_label=idle_label,
        status_item=status_item, update_status_menu=_update_status_menu,
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

    # ---- Show idle state on startup ----
    _apply_state(
        "idle", window, content_view, waveform_view,
        transcript_label, tts_controls, color_overlay,
        idle_label=idle_label,
        status_item=status_item, update_status_menu=_update_status_menu,
    )

    # ---- Clean shutdown: remove status item so menu bar icon disappears ----
    import atexit

    def _cleanup_status_item():
        try:
            NSStatusBar.systemStatusBar().removeStatusItem_(status_item)
        except Exception:
            pass

    atexit.register(_cleanup_status_item)

    # ---- SIGTERM / SIGINT handler ----
    # Two-pronged: (1) immediately remove menu bar icon from signal context,
    # (2) schedule clean shutdown via NSTimer for run loop cleanup.
    class Terminator(NSObject):
        def terminate_(self, timer):
            hud_server.shutdown()
            app.terminate_(None)

    terminator = Terminator.alloc().init()

    def handle_signal(signum, frame):
        _cleanup_status_item()
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.0, terminator, "terminate:", None, False
        )

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # ---- Run loop ----
    app.run()


if __name__ == "__main__":
    import sys as _sys
    main(menu_bar_only="--menu-bar-only" in _sys.argv)
